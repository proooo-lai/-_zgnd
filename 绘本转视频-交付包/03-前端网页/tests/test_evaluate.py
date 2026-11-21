"""Meaningful evaluation invariants: no fake accuracy or fake audible latency."""

from pathlib import Path
import importlib.util

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "evaluate.py"
SPEC = importlib.util.spec_from_file_location("story_evaluate", MODULE_PATH)
evaluate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evaluate)


def test_unscored_queries_do_not_become_correct_from_expected_keywords():
    records = [
        {"kind": "qa", "manual_result": True, "ok": True, "http_elapsed_ms": 120},
        {"kind": "qa", "manual_result": False, "ok": True, "http_elapsed_ms": 450},
        {"kind": "touch", "ok": False, "expected": "rabbit", "http_elapsed_ms": 90},
        {"kind": "seek", "ok": True, "manual_result": "true", "http_elapsed_ms": 80},
    ]
    summary = evaluate.compute_summary(records)
    assert summary["accuracy"] == 0.5
    assert summary["scored_queries"] == 2
    assert summary["unscored_queries"] == 2
    assert summary["scoring_coverage"] == 0.5
    assert summary["http_failures"] == 1
    assert summary["per_kind"]["touch"]["accuracy"] is None
    assert summary["audible_end_to_end"]["within_2s_ratio"] is None
    assert summary["http_latency"]["is_audible_end_to_end"] is False


def test_text_equality_is_diagnostic_not_semantic_judgment():
    checks = evaluate.diagnostic_checks(
        {"expected": "A red rabbit", "expected_page": 4},
        {"answer": " A RED rabbit ", "page": 4},
    )
    assert checks["exact_answer_match"] is True
    assert checks["expected_page_match"] is True
    assert evaluate.compute_summary([{"checks": checks}])["accuracy"] is None
    mismatch = evaluate.diagnostic_checks({"expected": "rabbit"}, {"answer": "There is no rabbit."})
    assert mismatch["exact_answer_match"] is False


def test_actual_audio_latency_requires_warmup_and_same_clock():
    common = {"measurement_kind": "audio_loopback", "measurement_source": "audio_loopback",
              "playback_event": "loopback_onset", "input_kind": "mic", "warmup_complete": True,
              "same_clock": True, "clock_domain": "loopback_capture", "speech_end_ms": 1000}
    metrics = {"measurements": [
        {**common, "first_audible_ms": 2999},
        {**common, "first_audible_ms": 3001},
        {**common, "measurement_kind": "browser_playing_event", "first_audible_ms": 1100},
        {**common, "warmup_complete": False, "first_audible_ms": 1100},
        {**common, "same_clock": False, "first_audible_ms": 1100},
        {**common, "first_audible_ms": 999},
    ]}
    for row in metrics["measurements"]:
        row["latency_ms"] = row["first_audible_ms"] - row["speech_end_ms"]
    result = evaluate.summarize_audible_metrics(metrics)
    assert result["verified_samples"] == 2
    assert result["within_2s"] == 1
    assert result["within_2s_ratio"] == 0.5
    assert sum(result["excluded_by_reason"].values()) == 4


def test_no_measurements_is_unknown_not_zero_latency():
    summary = evaluate.compute_summary([])
    assert summary["accuracy"] is None
    assert summary["http_latency"]["p50_ms"] is None
    assert summary["audible_end_to_end"]["status"] == "not_measured"


def test_protected_test_directory_is_rejected_before_reading(monkeypatch):
    def forbidden_resolve(*args, **kwargs):
        raise AssertionError("must reject lexical protected path before filesystem resolution")
    monkeypatch.setattr(Path, "resolve", forbidden_resolve)
    with pytest.raises(ValueError, match="测试集"):
        evaluate.checked_path("F:/AI+/参赛文件/测试集/queries.jsonl")


def test_touch_requires_coordinates_and_time(tmp_path):
    with pytest.raises(ValueError, match="touch"):
        evaluate.query_payload({"kind": "touch", "x": 0.2, "y": 0.4}, "book", tmp_path)
    route, payload = evaluate.query_payload({"kind": "touch", "x": 0.2, "y": 0.4, "time": 5}, "book", tmp_path)
    assert route == "touch"
    assert payload["book_id"] == "book"
    with pytest.raises(ValueError):
        evaluate.query_payload({"kind": "qa", "time": float("nan")}, "book", tmp_path)


def test_utf8_jsonl_requires_objects(tmp_path):
    good = tmp_path / "own_queries.jsonl"
    good.write_text('\ufeff{"question":"这是哪里？"}\n', encoding="utf-8")
    assert evaluate.read_jsonl(good)[0]["question"] == "这是哪里？"
    bad = tmp_path / "invalid.jsonl"
    bad.write_text('[]\n', encoding="utf-8")
    with pytest.raises(ValueError, match="必须是对象"):
        evaluate.read_jsonl(bad)


def test_manual_labels_bind_to_actual_result_ids(tmp_path):
    labels = tmp_path / "review.jsonl"
    labels.write_text('{"id":"q2","manual_result":true}\n', encoding="utf-8")
    records = [{"id": "q1"}, {"id": "q2"}]
    evaluate.apply_manual_labels(records, labels)
    assert records[1]["manual_result"] is True
    assert evaluate.compute_summary(records)["unscored_queries"] == 1
    labels.write_text('{"id":"wrong","manual_result":true}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="不存在"):
        evaluate.apply_manual_labels(records, labels)


def test_live_voice_api_uses_multipart(monkeypatch):
    calls = []
    def capture(url, data, content_type, timeout):
        calls.append((url, data, content_type, timeout))
        return 200, {"answer": "hello"}
    monkeypatch.setattr(evaluate, "request_http", capture)
    result = evaluate.request_voice("http://localhost/api/books/b/voice", {
        "session_id": "s", "time": 8, "audio": "UklGRg==", "mime_type": "audio/wav"
    }, 10)
    assert result[0] == 200
    assert calls[0][2].startswith("multipart/form-data; boundary=")
    assert b'name="audio"; filename="query.wav"' in calls[0][1]
    assert b'RIFF' in calls[0][1]
    assert b'name="session_id"' in calls[0][1]


def test_browser_metrics_remain_proxy_even_when_fast():
    result = evaluate.summarize_audible_metrics({"events": [
        {"input_kind": "voice", "latency_ms": 100, "playback_event": "playing",
         "turn_id": "t", "session_id": "s", "book_id": "b"}
    ]})
    assert result["verified_samples"] == 0
    assert result["status"] == "not_measured"


def test_loopback_requests_bypass_environment_proxy_only(monkeypatch):
    calls = []
    class Response:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return b'{"ok":true}'
    class Opener:
        def open(self, request, timeout):
            calls.append("direct_loopback")
            return Response()
    def build(handler):
        assert handler.proxies == {}
        return Opener()
    def default_open(request, timeout):
        calls.append("default_proxy_policy")
        return Response()
    monkeypatch.setattr(evaluate.urllib.request, "build_opener", build)
    monkeypatch.setattr(evaluate.urllib.request, "urlopen", default_open)
    assert evaluate.request_json("http://127.0.0.1:8765/api/metrics", None, 1)[0] == 200
    assert evaluate.request_json("http://[::1]:8765/api/metrics", None, 1)[0] == 200
    assert evaluate.request_json("https://remote.example/api/metrics", None, 1)[0] == 200
    assert calls == ["direct_loopback", "direct_loopback", "default_proxy_policy"]


def test_explicit_browser_source_cannot_be_relabelled_as_acoustic():
    result = evaluate.summarize_audible_metrics([{
        "measurement_kind": "audio_loopback", "measurement_source": "browser_playback_event",
        "warmup_complete": True, "same_clock": True, "clock_domain": "browser-performance",
        "speech_end_ms": 1000, "first_audible_ms": 1500}])
    assert result["verified_samples"] == 0


@pytest.mark.parametrize("updates", [{"measurement_source": None}, {"playback_event": "http_complete"},
    {"playback_event": None}, {"clock_domain": " "}, {"latency_ms": 900}, {"input_kind": "text"}])
def test_offline_acoustic_records_obey_the_http_contract(updates):
    item = {"measurement_kind": "audio_loopback", "measurement_source": "audio_loopback",
            "playback_event": "loopback_onset", "input_kind": "mic", "latency_ms": 500,
            "warmup_complete": True, "same_clock": True, "clock_domain": "capture-monotonic",
            "speech_end_ms": 1000, "first_audible_ms": 1500, **updates}
    assert evaluate.summarize_audible_metrics([item])["verified_samples"] == 0


@pytest.mark.parametrize("kind", [[], {}, None, 7])
def test_invalid_metric_kind_does_not_crash_summary(kind):
    result = evaluate.summarize_audible_metrics([{"measurement_kind": kind}])
    assert result["verified_samples"] == 0
    assert result["excluded_by_reason"]["not_verified_audible_measurement"] == 1
