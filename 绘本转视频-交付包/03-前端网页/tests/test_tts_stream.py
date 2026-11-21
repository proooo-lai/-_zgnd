import asyncio
import base64
import json
import wave

import httpx
import pytest

from storybook.providers import Provider, ProviderError, split_tts_text
from storybook.settings import Settings


class Bytes(httpx.AsyncByteStream):
    def __init__(self, value, pause=False):
        self.value, self.pause, self.closed = value, pause, False

    async def __aiter__(self):
        for offset in range(0, len(self.value), 7):
            yield self.value[offset:offset + 7]
        if self.pause:
            await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


def packet(data, final=False, *, multiline=False):
    value = {"output": {"audio": {"data": base64.b64encode(data).decode()},
                        "finish_reason": "stop" if final else None}}
    serialized = json.dumps(value, indent=2 if multiline else None)
    return ("\n".join("data: " + line for line in serialized.splitlines()) + "\n\n").encode()


async def make_provider(tmp_path, responder):
    config = {"kind": "dashscope", "model": "qwen3-tts-flash", "voice": "Cherry",
              "base_url": "https://models.example.test/api/v1", "api_key": "secret-never-output"}
    settings = Settings(data_dir=tmp_path / "data", config_file=tmp_path / "none.json", providers={"tts": config})
    provider = Provider(settings)
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(transport=httpx.MockTransport(responder))
    return provider


def test_split_tts_preserves_text_and_prefers_sentences():
    text = "这是第一句。" * 120 + "A long English sentence. " * 30
    parts = split_tts_text(text)
    assert "".join(parts) == text
    assert all(0 < len(part) <= 550 for part in parts)
    assert parts[0].endswith("。")
    assert max(map(len, split_tts_text("字" * 1300))) <= 550


def test_stream_pcm_writes_exact_wave_and_accepts_multiline_sse(tmp_path):
    async def run():
        seen, callbacks = [], []
        pcm1, pcm2 = b"\x00\x00\x11\x22", b"\x33\x44\x55\x66"
        wire = b": heartbeat\n\n" + packet(pcm1, multiline=True) + packet(pcm2) + packet(b"", final=True)
        stream = Bytes(wire)

        def respond(request):
            seen.append(request)
            return httpx.Response(200, headers={"content-type": "text/event-stream; charset=utf-8"}, stream=stream)

        provider = await make_provider(tmp_path, respond)
        async def on_pcm(payload):
            callbacks.append(payload)
        try:
            path = await provider.tts_stream("你好。", "zh", tmp_path / "answer.wav", on_pcm)
            assert len(seen) == 1
            assert seen[0].headers["X-DashScope-SSE"] == "enable"
            assert "stream" not in json.loads(seen[0].content)
            assert [chunk["index"] for chunk in callbacks] == [0, 1]
            assert all(chunk["sample_rate"] == 24000 for chunk in callbacks)
            with wave.open(str(path), "rb") as wav:
                assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (24000, 1, 2)
                assert wav.readframes(wav.getnframes()) == pcm1 + pcm2
            assert stream.closed
            assert not list(tmp_path.glob("*.partial"))
        finally:
            await provider.close()
    asyncio.run(run())


@pytest.mark.parametrize("wire", [
    b'data: {"output":{"audio":{"data":"!notbase64!"}}}\n\n',
    packet(b"\x00"),
    packet(b"\x00\x00"),  # missing explicit stop
    packet(b"", final=True),  # stop without any samples
    b'data: ["not-an-object"]\n\n',
    b'data: {broken}\n\n',
    b'event: error\ndata: {"code":"InvalidApiKey","message":"secret-never-output"}\n\n',
])
def test_invalid_stream_never_leaves_completed_audio_or_retries(tmp_path, wire):
    async def run():
        calls = []
        def respond(request):
            calls.append(request)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Bytes(wire))
        provider = await make_provider(tmp_path, respond)
        try:
            with pytest.raises(ProviderError) as caught:
                await provider.tts_stream("你好。", "zh", tmp_path / "answer.wav", None)
            assert "secret-never-output" not in str(caught.value)
            assert len(calls) == 1
            assert not (tmp_path / "answer.wav").exists()
            assert not list(tmp_path.glob("*.partial"))
        finally:
            await provider.close()
    asyncio.run(run())


def test_cancel_stream_cleans_partial_and_preserves_prior_output(tmp_path):
    async def run():
        stream = Bytes(packet(b"\x11\x22"), pause=True)
        provider = await make_provider(tmp_path, lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=stream))
        output = tmp_path / "answer.wav"
        output.write_bytes(b"previous-complete-result")
        received = asyncio.Event()
        async def on_pcm(payload):
            received.set()
        try:
            task = asyncio.create_task(provider.tts_stream("你好。", "zh", output, on_pcm))
            await asyncio.wait_for(received.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert output.read_bytes() == b"previous-complete-result"
            assert stream.closed
            assert not list(tmp_path.glob("*.partial"))
        finally:
            await provider.close()
    asyncio.run(run())


def test_long_text_segments_keep_global_index_and_all_pcm(tmp_path):
    async def run():
        sent, chunks = [], []
        def respond(request):
            text = json.loads(request.content)["input"]["text"]
            sent.append(text)
            pcm = len(sent).to_bytes(2, "little")
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  stream=Bytes(packet(pcm) + packet(b"", final=True)))
        provider = await make_provider(tmp_path, respond)
        async def on_pcm(payload):
            chunks.append(payload)
        text = "A sentence to read. " * 80
        try:
            output = await provider.tts_stream(text, "en", tmp_path / "answer.wav", on_pcm)
            assert "".join(sent) == text
            assert len(sent) > 1 and all(len(s) <= 550 for s in sent)
            assert [chunk["index"] for chunk in chunks] == list(range(len(sent)))
            with wave.open(str(output), "rb") as wav:
                assert wav.readframes(wav.getnframes()) == b"".join(i.to_bytes(2, "little") for i in range(1, len(sent) + 1))
        finally:
            await provider.close()
    asyncio.run(run())


def test_non_dashscope_delegates_without_pcm_callback(tmp_path, monkeypatch):
    async def run():
        provider = await make_provider(tmp_path, lambda request: pytest.fail("unexpected request"))
        provider.settings.providers["tts"]["kind"] = "openai"
        called = []
        async def ordinary(text, language, out, voice=None):
            called.append((text, language, out))
            return out
        async def on_pcm(payload):
            pytest.fail("regular provider must not emit fake PCM")
        monkeypatch.setattr(provider, "tts", ordinary)
        try:
            result = await provider.tts_stream("Hello.", "en", tmp_path / "answer.wav", on_pcm)
            assert result == tmp_path / "answer.wav" and len(called) == 1
        finally:
            await provider.close()
    asyncio.run(run())
