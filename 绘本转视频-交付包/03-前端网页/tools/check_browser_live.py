"""Run exactly one text and one authored WAV question through the actual UI.

This does not read competition test data or print credentials. Timing is a
request-to-WebAudio scheduling proxy, never end-of-speech audible latency.
"""
from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / "artifacts"
QUESTION = SCRATCH / "live_validation" / "question-zh-16k.wav"
SCREENSHOT = SCRATCH / "browser" / "live-pcm.png"


def available_books():
    """书架里的绘本 id；连不上服务时由调用方给出可读的提示。"""
    with urllib.request.urlopen("http://127.0.0.1:8765/api/books", timeout=5) as response:
        payload = json.loads(response.read().decode("utf-8"))
    books = payload.get("books") if isinstance(payload, dict) else payload
    return [book.get("id") for book in (books or []) if isinstance(book, dict) and book.get("id")]


def main(book_id, question):
    if not book_id:
        raise SystemExit("没有可用的绘本：先在页面里导入一本，或用 --book 指定 book id。")
    if not question.is_file():
        raise SystemExit(
            "缺少 16 kHz 提问音频：" + str(question) + "\n"
            "先生成一份：python tools/check_speech_live.py"
            "（会写出 artifacts/live_validation/question-zh-16k.wav），"
            "或用 --question 指定现成的 WAV。"
        )
    SCREENSHOT.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "source": "self_authored_demo_and_question_not_competition_test_data",
        "measurement": "request_to_web_audio_voiced_sample_schedule_proxy",
        "actual_audible_end_to_end_latency": "not_measured",
        "cases": [],
    }
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            channel="chrome", headless=True,
            args=["--autoplay-policy=no-user-gesture-required"],
        )
        page = browser.new_page(viewport={"width": 1440, "height": 1120})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto("http://127.0.0.1:8765", wait_until="networkidle")
        page.evaluate("(id) => loadBook(id)", book_id)
        page.wait_for_function("id => state.book && state.book.id === id", arg=book_id)
        page.evaluate("""() => {
          window.liveUiProbe = {pcm: [], starts: [], requests: [], results: [], htmlPlays: 0};
          const originalPCM = window.enqueuePCM;
          window.enqueuePCM = function(chunk, context, generation) {
            liveUiProbe.pcm.push({stream_id: chunk.stream_id, index: chunk.index,
              turn_id: chunk.turn_id, sample_rate: chunk.sample_rate, received: performance.now()});
            return originalPCM(chunk, context, generation);
          };
          const originalStart = AudioBufferSourceNode.prototype.start;
          AudioBufferSourceNode.prototype.start = function(at) {
            liveUiProbe.starts.push({at, duration: this.buffer.duration,
              samples: this.buffer.length, now: performance.now()});
            return originalStart.call(this, at);
          };
          const originalPlay = HTMLMediaElement.prototype.play;
          HTMLMediaElement.prototype.play = function() {
            if (this.id === 'response-audio') liveUiProbe.htmlPlays++;
            return originalPlay.call(this);
          };
          const originalFetch = window.fetch;
          window.fetch = function(url, options) {
            if (String(url).endsWith('/ask/stream') || String(url).endsWith('/voice/stream'))
              liveUiProbe.requests.push({url: String(url), started: performance.now()});
            return originalFetch.call(this, url, options);
          };
          const originalAccept = window.accept;
          window.accept = function(result, context, skipAudio) {
            liveUiProbe.results.push({turn_id: result.turn_id, answer: result.answer,
              transcript: result.transcript || null, audio_url: result.audio_url || null,
              skipAudio, time: performance.now()});
            return originalAccept(result, context, skipAudio);
          };
        }""")

        def snapshot():
            return page.evaluate("""() => ({
              ...liveUiProbe, metrics: state.metrics,
              voiceAttempts: state.voiceAttempts,
              sources: state.pcm ? state.pcm.sources.size : 0,
              pending: state.pcm ? state.pcm.pending.length : 0,
              browserStatus: document.getElementById('interaction-status').textContent
            })""")

        def collect(kind, previous):
            page.wait_for_function(
                "n => liveUiProbe.results.length > n && !state.busy",
                arg=previous["results"], timeout=150000,
            )
            page.wait_for_function(
                "n => state.metrics.length > n",
                arg=previous["metrics"], timeout=30000,
            )
            page.wait_for_function(
                "() => !state.pcm || (!state.pcm.sources.size && !state.pcm.pending.length)",
                timeout=90000,
            )
            data = snapshot()
            result = data["results"][-1]
            new_metrics = data["metrics"][previous["metrics"]:]
            new_pcm = data["pcm"][previous["pcm"]:]
            request = data["requests"][-1]
            first_metric = new_metrics[0]
            case = {
                "input": kind,
                "answer": result["answer"],
                "transcript": result["transcript"],
                "pcm_chunks": len(new_pcm),
                "pcm_stream_count": len({c["stream_id"] for c in new_pcm}),
                "first_audio_metric_count": len(new_metrics),
                "playback_event": first_metric["playback_event"],
                "request_to_first_voiced_sample_proxy_ms": round(
                    first_metric["first_audible_ms"] - request["started"], 2
                ),
                "ui_send_to_first_voiced_sample_proxy_ms": first_metric["latency_ms"],
                "html_audio_automatic_plays": data["htmlPlays"] - previous["htmlPlays"],
                "full_audio_available": bool(result["audio_url"]),
                "final_skipped_duplicate_play": result["skipAudio"],
                "mic_compliance_denominator": data["voiceAttempts"],
            }
            assert new_pcm, "No actual PCM event received"
            assert len(new_metrics) == 1, "Duplicate or missing first-audio metric"
            assert first_metric["playback_event"] == "web_audio_pcm_start"
            assert case["html_audio_automatic_plays"] == 0, "URL replay duplicated PCM"
            assert result["skipAudio"] is True
            assert data["voiceAttempts"] == 0, "Non-microphone input entered mic compliance"
            report["cases"].append(case)
            print(json.dumps(case, ensure_ascii=False), flush=True)
            return data

        before = {"results": 0, "metrics": 0, "pcm": 0, "htmlPlays": 0}
        page.locator("#question").fill("How many apples can you see?")
        page.locator("#send-question").click()
        first = collect("text_cross_language", before)

        before = {
            "results": len(first["results"]), "metrics": len(first["metrics"]),
            "pcm": len(first["pcm"]), "htmlPlays": first["htmlPlays"],
        }
        page.locator("#query-audio").set_input_files(str(question))
        page.locator("#send-audio").click()
        second = collect("authored_16khz_wav_upload", before)

        history = page.evaluate("""async () => {
          const response = await fetch(bookPath('/history') + '?session_id=' + encodeURIComponent(state.session));
          const data = await response.json();
          return {count: (data.turns || []).length,
            turns: (data.turns || []).map(t => ({turn_id: t.turn_id || t.id,
              has_audio_url: Boolean(t.audio_url), input_kind: t.input_kind}))};
        }""")
        page.get_by_role("tab", name="交互实测", exact=True).click()
        page.screenshot(path=str(SCREENSHOT), full_page=True)
        report.update(
            history=history, browser_errors=errors, screenshot=str(SCREENSHOT),
            final_pcm_sources=second["sources"], final_pcm_pending=second["pending"],
            passed=not errors,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        browser.close()


def parse_args():
    parser = argparse.ArgumentParser(description="实时链路 UI 抽查：一句文字提问 + 一段自录 16 kHz 提问音频")
    parser.add_argument("--book", default=None, help="要打开的绘本 id；省略时取书架第一本")
    parser.add_argument("--question", default=QUESTION, type=Path, help="16 kHz 单声道提问 WAV")
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    target = arguments.book
    if not target:
        try:
            catalog = available_books()
        except Exception as error:
            raise SystemExit("连不上本地服务 http://127.0.0.1:8765 （" + type(error).__name__ + "），先双击 一键启动.vbs") from None
        target = catalog[0] if catalog else None
    main(target, Path(arguments.question))
