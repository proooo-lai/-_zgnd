from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from storybook.providers import Provider, ProviderError, dashscope_audio_url
from storybook.settings import Settings, validate_service_url


def settings(tmp_path, **providers):
    return Settings(data_dir=tmp_path / "data", config_file=tmp_path / "config.json", providers=providers)


def config(kind="openai"):
    return {"kind": kind, "base_url": "https://models.example/v1", "model": "test-model", "api_key": "private-key-never-echo"}


def test_dashscope_signed_audio_url_upgrades_tls_without_changing_signature():
    url = "http://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/audio.wav?Signature=a%2Bb%3D&Expires=123"
    assert dashscope_audio_url(url) == "https" + url[4:]
    assert dashscope_audio_url(url.replace(".aliyuncs.com", ".aliyuncs.com.evil.test")) == url.replace(".aliyuncs.com", ".aliyuncs.com.evil.test")
    assert dashscope_audio_url("http://user:password@dashscope-result-bj.oss-cn-beijing.aliyuncs.com/a") == "http://user:password@dashscope-result-bj.oss-cn-beijing.aliyuncs.com/a"


def test_dashscope_tts_downloads_signed_audio_over_https(tmp_path):
    async def scenario():
        calls = []
        wave = b"RIFF" + bytes(4) + b"WAVE" + bytes(40)
        def respond(request):
            calls.append(request)
            if request.method == "POST":
                return httpx.Response(200, json={"output": {"audio": {"url": "http://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/a.wav?Signature=a%2Bb"}}})
            return httpx.Response(200, content=wave)
        provider = Provider(settings(tmp_path, tts=config("dashscope")))
        await provider.client.aclose()
        provider.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            target = await provider.tts("你好", "zh", tmp_path / "speech.wav")
            assert target.read_bytes() == wave
            assert calls[1].url.scheme == "https"
            assert calls[1].url.query == b"Signature=a%2Bb"
            assert "authorization" not in calls[1].headers
        finally:
            await provider.close()
    asyncio.run(scenario())


def test_public_settings_whitelist_and_strict_url(tmp_path):
    value = settings(tmp_path, chat={**config(), "token": "hidden", "secret": "hidden"})
    public = json.dumps(value.public())
    assert "private-key-never-echo" not in public
    assert "hidden" not in public
    assert "api_key" not in public
    for invalid in ("http://localhost.evil.example/v1", "http://127.0.0.1.evil/v1",
                    "https://user:secret@models.example/v1", "https://models.example/v1?key=secret"):
        with pytest.raises(ValueError):
            validate_service_url(invalid)
    assert validate_service_url("http://[::1]:8000/v1/") == "http://[::1]:8000/v1"


def test_atomic_settings_updates_keep_runtime_and_no_partial_mutation(tmp_path):
    file = tmp_path / "config.json"
    file.write_text(json.dumps({"providers": {"chat": config()}, "runtime": {"target_duration": 150, "fps": 30}, "owner": "team"}))
    value = settings(tmp_path)
    value.save_providers({"chat": {"voice": "voice-a"}})
    saved = json.loads(file.read_text(encoding="utf-8"))
    assert saved["runtime"]["target_duration"] == 150
    assert saved["runtime"]["fps"] == 30
    assert saved["owner"] == "team"
    before = dict(value.providers["chat"])
    with pytest.raises(ValueError):
        value.save_providers({"chat": {"model": "should-not-stick"}, "asr": {"base_url": "http://external.example"}})
    assert value.providers["chat"] == before


@pytest.mark.parametrize("body", [None, [], {"choices": []}, {"choices": [{"message": {"content": None}}]}, {"error": "private-key-never-echo"}])
def test_malformed_model_json_has_stable_sanitized_failure(tmp_path, body):
    async def scenario():
        provider = Provider(settings(tmp_path, chat=config()))
        await provider.client.aclose()
        provider.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)))
        try:
            with pytest.raises(ProviderError) as error:
                await provider.chat("question")
            assert "private-key-never-echo" not in str(error.value)
        finally:
            await provider.close()
    asyncio.run(scenario())


def test_post_timeout_is_not_retried_and_task_trace_records_unknown(tmp_path):
    async def scenario():
        calls = []
        def fail(request):
            calls.append(request)
            raise httpx.ReadTimeout("server echoed private-key-never-echo", request=request)
        provider = Provider(settings(tmp_path, video=config("ark")))
        await provider.client.aclose()
        provider.client = httpx.AsyncClient(transport=httpx.MockTransport(fail))
        image = tmp_path / "page.png"
        image.write_bytes(b"fixture")
        output = tmp_path / "clip.mp4"
        try:
            with pytest.raises(ProviderError, match="未自动重试") as error:
                await provider.generate_clip(image, "gentle scene", output, 5)
            assert len(calls) == 1
            assert calls[0].url.path == "/v1/contents/generations/tasks"
            assert "private-key-never-echo" not in str(error.value)
            trace = json.loads(output.with_suffix(".task.json").read_text(encoding="utf-8"))
            assert trace["status"] == "submission_unconfirmed"
        finally:
            await provider.close()
    asyncio.run(scenario())


def test_http_error_and_redirect_never_echo_credentials(tmp_path):
    async def scenario():
        for status in (401, 500, 302):
            calls = []
            def respond(request):
                calls.append(request)
                return httpx.Response(status, text="private-key-never-echo", headers={"location": "https://elsewhere.example"})
            provider = Provider(settings(tmp_path, chat=config()))
            await provider.client.aclose()
            provider.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
            try:
                with pytest.raises(ProviderError) as error:
                    await provider.chat("question")
                assert len(calls) == 1
                assert "private-key-never-echo" not in str(error.value)
            finally:
                await provider.close()
    asyncio.run(scenario())


def test_dashscope_asr_uses_audio_data_and_accepts_text_blocks(tmp_path):
    async def scenario():
        calls = []
        def respond(request):
            calls.append(request)
            return httpx.Response(200, json={"choices": [{"message": {"content": [{"type": "text", "text": "Where is the rabbit?"}]}}]})
        provider = Provider(settings(tmp_path, asr=config("dashscope")))
        await provider.client.aclose()
        provider.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        source = tmp_path / "query.wav"
        source.write_bytes(b"RIFFfixture")
        try:
            assert await provider.asr(source) == "Where is the rabbit?"
            payload = json.loads(calls[0].content)
            assert payload["messages"][0]["content"][0]["input_audio"]["data"].startswith("data:audio/")
        finally:
            await provider.close()
    asyncio.run(scenario())


def test_tts_json_error_is_not_written_as_wav(tmp_path):
    async def scenario():
        provider = Provider(settings(tmp_path, tts=config()))
        await provider.client.aclose()
        provider.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"error": "bad"})))
        target = tmp_path / "speech.wav"
        try:
            with pytest.raises(ProviderError, match="WAV"):
                await provider.tts("hello", "en", target)
            assert not target.exists()
        finally:
            await provider.close()
    asyncio.run(scenario())


def test_download_rejects_https_to_http_redirect_and_keeps_no_partial(tmp_path):
    async def scenario():
        provider = Provider(settings(tmp_path))
        await provider.client.aclose()
        provider.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(302, headers={"location": "http://insecure.example/file"})))
        target = tmp_path / "clip.mp4"
        try:
            with pytest.raises(ProviderError, match="HTTPS"):
                await provider._download("https://files.example/file?signature=private", target)
            assert not target.exists()
            assert not list(tmp_path.glob("*.download"))
        finally:
            await provider.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("status,code,hint", [
    (404, "ModelNotOpen", "火山方舟控制台开通"),
    (401, "InvalidApiKey", "API Key 无效"),
    (401, "AuthenticationError", "鉴权失败"),
    (403, "AccessDenied", "没有调用此模型的权限"),
    (429, "QuotaExceeded", "额度不足"),
])
def test_known_service_error_is_actionable_without_echoing_message(tmp_path, status, code, hint):
    async def scenario():
        calls = []
        def respond(request):
            calls.append(request)
            return httpx.Response(status, json={"error": {"code": code,
                "message": "account 1234567890 private-key-never-echo https://signed.example/?token=hidden"}})
        provider = Provider(settings(tmp_path, chat=config()))
        await provider.client.aclose()
        provider.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            with pytest.raises(ProviderError, match=hint) as captured:
                await provider.chat("hello")
            error = captured.value
            assert error.code == code
            assert error.status_code == status
            assert error.submission_state == "rejected"
            assert len(calls) == 1
            for private in ("1234567890", "private-key-never-echo", "signed.example", "hidden"):
                assert private not in str(error)
        finally:
            await provider.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("status,expected_state", [(404, "rejected"), (500, "submission_unconfirmed")])
def test_video_trace_distinguishes_explicit_rejection_from_server_uncertainty(tmp_path, status, expected_state):
    async def scenario():
        calls = []
        def respond(request):
            calls.append(request)
            return httpx.Response(status, json={"error": {"code": "ModelNotOpen", "message": "private-key-never-echo"}})
        provider = Provider(settings(tmp_path, video=config("ark")))
        await provider.client.aclose()
        provider.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        image = tmp_path / "page.png"
        image.write_bytes(b"fixture")
        output = tmp_path / "clip.mp4"
        try:
            with pytest.raises(ProviderError) as captured:
                await provider.generate_clip(image, "gentle scene", output, 5)
            assert captured.value.submission_state == expected_state
            trace = json.loads(output.with_suffix(".task.json").read_text(encoding="utf-8"))
            assert trace["status"] == expected_state
            assert trace["error_code"] == "ModelNotOpen"
            assert trace["http_status"] == status
            assert trace["task_id"] is None
            assert "private-key-never-echo" not in json.dumps(trace)
            assert len(calls) == 1
        finally:
            await provider.close()
    asyncio.run(scenario())


def test_unknown_error_code_is_not_disclosed(tmp_path):
    async def scenario():
        provider = Provider(settings(tmp_path, chat=config()))
        await provider.client.aclose()
        provider.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
            httpx.Response(400, json={"error": {"code": "private-key-never-echo", "message": "account-secret"}})))
        try:
            with pytest.raises(ProviderError) as captured:
                await provider.chat("hello")
            assert captured.value.code is None
            assert "private-key-never-echo" not in str(captured.value)
            assert "account-secret" not in str(captured.value)
        finally:
            await provider.close()
    asyncio.run(scenario())


def test_success_http_with_known_error_code_has_same_safe_mapping(tmp_path):
    async def scenario():
        provider = Provider(settings(tmp_path, chat=config()))
        await provider.client.aclose()
        provider.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
            httpx.Response(200, json={"code": "InvalidApiKey", "message": "private-key-never-echo"})))
        try:
            with pytest.raises(ProviderError, match="API Key 无效") as captured:
                await provider.chat("hello")
            assert captured.value.code == "InvalidApiKey"
            assert captured.value.submission_state == "rejected"
            assert "private-key-never-echo" not in str(captured.value)
        finally:
            await provider.close()
    asyncio.run(scenario())


def test_streaming_http_failure_uses_known_safe_error_mapping(tmp_path):
    async def scenario():
        provider = Provider(settings(tmp_path, chat=config()))
        await provider.client.aclose()
        provider.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
            httpx.Response(401, json={"error": {"type": "AuthenticationError", "message": "private-key-never-echo"}})))
        try:
            with pytest.raises(ProviderError, match="鉴权失败") as captured:
                await provider._stream_completion(config(), {"stream": True}, lambda value: None)
            assert captured.value.code == "AuthenticationError"
            assert captured.value.status_code == 401
            assert captured.value.submission_state == "rejected"
            assert "private-key-never-echo" not in str(captured.value)
        finally:
            await provider.close()
    asyncio.run(scenario())
