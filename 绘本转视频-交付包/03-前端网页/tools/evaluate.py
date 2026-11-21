"""Run explicitly supplied queries without reading or discovering datasets.

Usage:
    python tools/evaluate.py queries.jsonl --book-id BOOK --base-url http://127.0.0.1:8765
    python tools/evaluate.py --summarize evaluation/results.jsonl

Correctness is scored only from an explicit boolean ``manual_result`` added by
a reviewer. Exact text/page comparisons are diagnostic checks, not a semantic
judge. No answer files are discovered automatically.
"""

from __future__ import annotations

import argparse
import base64
import json
import ipaddress
import math
import mimetypes
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from storybook.metrics import acoustic_record_error


KINDS = ("qa", "touch", "seek")
AUDIBLE_MEASUREMENTS = {"audio_loopback", "actual_audible"}


def checked_path(value: str | Path, *, must_exist: bool = True) -> Path:
    """Reject protected test directories before any file read or existence test."""
    path = Path(value).expanduser()
    # Check lexical segments before resolve() can follow a symlink/junction.
    normalized = str(path).replace("\\", "/")
    if any(part.casefold() == "测试集" for part in normalized.split("/")):
        raise ValueError("拒绝访问测试集目录；请显式提供允许使用的自建评测文件。")
    resolved = path.resolve()
    if any(part.casefold() == "测试集" for part in resolved.parts):
        raise ValueError("评测路径指向受保护的测试集目录。")
    if must_exist and not resolved.is_file():
        raise ValueError(f"评测文件不存在或不是普通文件: {resolved}")
    return resolved


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    file_path = checked_path(path)
    rows = []
    with file_path.open("r", encoding="utf-8-sig") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"JSONL 第 {number} 行无效: {exc.msg}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"JSONL 第 {number} 行必须是对象。")
            rows.append(row)
    return rows


def finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    point = (len(ordered) - 1) * fraction
    low = math.floor(point)
    high = math.ceil(point)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (point - low), 3)


def manual_score(record: dict[str, Any]) -> bool | None:
    """Only JSON booleans count as human correctness labels."""
    result = record.get("manual_result")
    return result if isinstance(result, bool) else None


def summarize_audible_metrics(metrics: Any) -> dict[str, Any]:
    """Strictly separate verified acoustic measurements from playback proxies.

    Accepted record schema: measurement_kind=audio_loopback|actual_audible,
    warmup_complete=true, speech_end_ms, first_audible_ms. Both timestamps must
    use the same clock, documented through clock_domain and same_clock=true.
    Merely naming an HTTP or browser event 'first_audio' does not qualify.
    """
    if isinstance(metrics, dict):
        entries = metrics.get("measurements", metrics.get("records", metrics.get("events", [])))
    else:
        entries = metrics
    if not isinstance(entries, list):
        entries = []
    valid = []
    excluded: Counter[str] = Counter()
    for item in entries:
        if not isinstance(item, dict):
            excluded["invalid_record"] += 1
            continue
        error = acoustic_record_error(item, require_warmup=True)
        if error:
            excluded[error] += 1
            continue
        valid.append(float(item["first_audible_ms"] - item["speech_end_ms"]))
    passed = sum(value <= 2000.0 for value in valid)
    return {
        "source": "/api/metrics; explicit acoustic measurements only",
        "definition": "speech end to actual audible answer onset, after warmup",
        "threshold_ms": 2000,
        "threshold_role": "internal_target_not_confirmed_official_limit",
        "measurement_authenticity": "caller_supplied_acoustic_records_require_capture_evidence",
        "verified_samples": len(valid),
        "within_2s": passed,
        "within_2s_ratio": passed / len(valid) if valid else None,
        "p50_ms": percentile(valid, 0.50),
        "p95_ms": percentile(valid, 0.95),
        "excluded_by_reason": dict(excluded),
        "status": "measured" if valid else "not_measured",
    }


def compute_summary(records: list[dict[str, Any]], metrics: Any = None) -> dict[str, Any]:
    """Count full queries, keeping unreviewed and failed runs visible."""
    scored = [manual_score(row) for row in records if manual_score(row) is not None]
    correct = sum(result is True for result in scored)
    per_kind: dict[str, Any] = {}
    for kind in KINDS:
        subset = [row for row in records if row.get("kind", "qa") == kind]
        labels = [manual_score(row) for row in subset if manual_score(row) is not None]
        passed = sum(value is True for value in labels)
        per_kind[kind] = {
            "total": len(subset),
            "scored": len(labels),
            "correct": passed,
            "incorrect": len(labels) - passed,
            "unscored": len(subset) - len(labels),
            "accuracy": passed / len(labels) if labels else None,
            "http_failures": sum(row.get("ok") is False for row in subset),
        }
    http_values = [float(row["http_elapsed_ms"]) for row in records
                   if finite_number(row.get("http_elapsed_ms")) and row["http_elapsed_ms"] >= 0]
    return {
        "total_queries": len(records),
        "scored_queries": len(scored),
        "correct_queries": correct,
        "incorrect_queries": len(scored) - correct,
        "unscored_queries": len(records) - len(scored),
        "accuracy": correct / len(scored) if scored else None,
        "accuracy_source": "explicit human manual_result booleans; full query correctness",
        "accuracy_denominator": "manually scored queries only; inspect unscored_queries separately",
        "scoring_coverage": len(scored) / len(records) if records else None,
        "http_failures": sum(row.get("ok") is False for row in records),
        "per_kind": per_kind,
        "http_latency": {
            "source": "client monotonic clock around complete HTTP request/response",
            "is_audible_end_to_end": False,
            "samples": len(http_values),
            "mean_ms": round(statistics.mean(http_values), 3) if http_values else None,
            "p50_ms": percentile(http_values, 0.50),
            "p95_ms": percentile(http_values, 0.95),
        },
        "audible_end_to_end": summarize_audible_metrics(metrics),
    }


def normalize_answer(value: str) -> str:
    return " ".join(value.casefold().split())


def diagnostic_checks(query: dict[str, Any], response: Any) -> dict[str, Any]:
    checks: dict[str, Any] = {"scoring_role": "diagnostic_only_not_semantic_correctness"}
    if not isinstance(response, dict):
        return checks
    answer = response.get("answer", response.get("text"))
    acceptable = query.get("acceptable_answers", [])
    if isinstance(acceptable, str):
        acceptable = [acceptable]
    if not isinstance(acceptable, list):
        acceptable = []
    acceptable = [value for value in acceptable if isinstance(value, str)]
    if isinstance(query.get("expected"), str):
        acceptable = [query["expected"], *acceptable]
    if acceptable and isinstance(answer, str):
        checks["exact_answer_match"] = normalize_answer(answer) in {
            normalize_answer(candidate) for candidate in acceptable
        }
    if "expected_page" in query:
        page = response.get("page", response.get("page_number"))
        checks["expected_page_match"] = page == query["expected_page"] if page is not None else None
    return checks


def request_http(url: str, data: bytes | None, content_type: str, timeout: float) -> tuple[int, Any]:
    request = urllib.request.Request(url, data=data, headers={"Content-Type": content_type})
    hostname = urllib.parse.urlsplit(url).hostname or ""
    try:
        is_loopback = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        is_loopback = hostname.casefold() == "localhost"
    # Local evaluation should never go through the machine's HTTP proxy. Keep
    # normal proxy behavior for explicitly configured remote evaluation hosts.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({})).open if is_loopback else urllib.request.urlopen
    try:
        with opener(request, timeout=timeout) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read(), exc.code
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        body = {"error": "non-JSON HTTP response", "body_preview": raw[:300].decode("utf-8", "replace")}
    return status, body


def request_json(url: str, payload: dict[str, Any] | None, timeout: float) -> tuple[int, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    return request_http(url, data, "application/json", timeout)


def request_voice(url: str, payload: dict[str, Any], timeout: float) -> tuple[int, Any]:
    """Match the live API's multipart audio upload, not a mock JSON ASR API."""
    boundary = "story-evaluation-" + uuid.uuid4().hex
    parts: list[bytes] = []
    for name in ("session_id", "time"):
        if name in payload:
            parts.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                          + str(payload[name]) + "\r\n").encode("utf-8"))
    parts.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"audio\"; filename=\"query.wav\"\r\n"
                  f"Content-Type: {payload['mime_type']}\r\n\r\n").encode("ascii"))
    parts.append(base64.b64decode(payload["audio"]))
    parts.append(f"\r\n--{boundary}--\r\n".encode("ascii"))
    return request_http(url, b"".join(parts), f"multipart/form-data; boundary={boundary}", timeout)


def query_payload(query: dict[str, Any], book_id: str, base_dir: Path) -> tuple[str, dict[str, Any]]:
    kind = query.get("kind", "qa")
    if kind not in KINDS:
        raise ValueError(f"不支持的 kind: {kind!r}")
    question = query.get("question", "")
    if not isinstance(question, str):
        raise ValueError("question 必须是字符串。")
    payload: dict[str, Any] = {"book_id": book_id, "question": question}
    if query.get("session_id"):
        payload["session_id"] = str(query["session_id"])
    for name in ("time", "x", "y"):
        if name in query:
            if not finite_number(query[name]) or query[name] < 0:
                raise ValueError(f"{name} 必须是有限非负数。")
            payload[name] = query[name]
    if kind == "touch" and any(name not in payload for name in ("x", "y", "time")):
        raise ValueError("touch 查询必须提供 x、y、time。")
    if query.get("audio"):
        audio_path = Path(str(query["audio"]))
        if not audio_path.is_absolute():
            audio_path = base_dir / audio_path
        audio_path = checked_path(audio_path)
        if audio_path.stat().st_size > 30 * 1024 * 1024:
            raise ValueError("单个查询音频不能超过 30 MB。")
        payload["audio"] = base64.b64encode(audio_path.read_bytes()).decode("ascii")
        payload["mime_type"] = mimetypes.guess_type(audio_path.name)[0] or "audio/wav"
        return "voice", payload
    return ("touch" if kind == "touch" else "ask"), payload


def run_query(query: dict[str, Any], index: int, args: argparse.Namespace, base_dir: Path) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": query.get("id", index),
        "kind": query.get("kind", "qa"),
        "question": query.get("question", ""),
        "source": "explicit user-provided JSONL",
        "manual_result": query.get("manual_result") if isinstance(query.get("manual_result"), bool) else None,
    }
    for key in ("expected", "acceptable_answers", "expected_page", "audio", "x", "y", "time"):
        if key in query:
            record[key] = query[key]
    started: float | None = None
    try:
        route, payload = query_payload(query, args.book_id, base_dir)
        payload.setdefault("session_id", args.session_id)
        template = getattr(args, route + "_path")
        route_path = template.format(book_id=urllib.parse.quote(args.book_id, safe=""))
        url = args.base_url.rstrip("/") + "/" + route_path.lstrip("/")
        started = time.perf_counter()
        status, response = (request_voice(url, payload, args.timeout) if route == "voice"
                            else request_json(url, payload, args.timeout))
        record.update({
            "http_status": status,
            "ok": 200 <= status < 300 and not (isinstance(response, dict) and response.get("error")),
            "response": response,
            "checks": diagnostic_checks(query, response),
        })
    except (ValueError, OSError, urllib.error.URLError, TimeoutError) as exc:
        record.update({"ok": False, "error": str(exc), "error_type": type(exc).__name__})
    record["http_elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3) if started is not None else None
    record["latency_source"] = "HTTP full response including transport serialization; not audible latency"
    record["score_status"] = "manually_scored" if manual_score(record) is not None else "unscored"
    return record


def apply_manual_labels(records: list[dict[str, Any]], labels_path: str | Path) -> None:
    """Import post-review labels by ID without using question keyword heuristics."""
    labels = read_jsonl(labels_path)
    indexed: dict[str, bool] = {}
    for label in labels:
        if "id" not in label or not isinstance(label.get("manual_result"), bool):
            raise ValueError("人工标签行必须包含 id 和布尔值 manual_result。")
        key = str(label["id"])
        if key in indexed:
            raise ValueError(f"人工标签 id 重复: {key}")
        indexed[key] = label["manual_result"]
    record_ids = [str(record.get("id")) for record in records]
    if len(set(record_ids)) != len(record_ids):
        raise ValueError("评测结果 id 不唯一，无法安全合并人工标签。")
    unknown = set(indexed) - set(record_ids)
    if unknown:
        raise ValueError("人工标签包含本次评测中不存在的 id: " + ", ".join(sorted(unknown)))
    for record in records:
        key = str(record.get("id"))
        if key in indexed:
            record["manual_result"] = indexed[key]
            record["manual_result_source"] = "explicit reviewer labels JSONL"
            record["score_status"] = "manually_scored"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("queries", nargs="?", help="显式给定的自建 JSONL；不会自动查找数据")
    parser.add_argument("--summarize", help="汇总人工填写 manual_result 后的结果 JSONL，不发送请求")
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--book-id")
    parser.add_argument("--session-id", default="evaluation-" + uuid.uuid4().hex)
    parser.add_argument("--labels", help="显式给定人工复核 JSONL，每行 {id,manual_result:true|false}")
    parser.add_argument("--metrics-file", help="汇总时使用显式给定的已保存 metrics JSON；不自动寻找文件")
    parser.add_argument("--output-dir", default="evaluation")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--ask-path", default="/api/books/{book_id}/ask")
    parser.add_argument("--touch-path", default="/api/books/{book_id}/touch")
    parser.add_argument("--voice-path", default="/api/books/{book_id}/voice")
    parser.add_argument("--metrics-path", default="/api/metrics")
    args = parser.parse_args(argv)
    if args.summarize and args.queries:
        parser.error("--summarize 与 queries 不能同时使用。")
    if not args.summarize and (not args.queries or not args.book_id):
        parser.error("运行评测需要 queries 和 --book-id。")
    if not finite_number(args.timeout) or args.timeout <= 0:
        parser.error("--timeout 必须大于 0。")
    try:
        if args.summarize:
            records = read_jsonl(args.summarize)
            if args.labels:
                apply_manual_labels(records, args.labels)
            metrics = None
            if args.metrics_file:
                metrics = json.loads(checked_path(args.metrics_file).read_text(encoding="utf-8-sig"))
                if isinstance(metrics, dict) and "http_status" in metrics:
                    metrics = metrics.get("data") if metrics["http_status"] == 200 else None
            summary = compute_summary(records, metrics)
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0
        query_path = checked_path(args.queries)
        queries = read_jsonl(query_path)
        if args.labels:
            parser.error("--labels 用于 --summarize：请先运行，再对实际回答进行人工复核。")
        output_dir = checked_path(args.output_dir, must_exist=False)
        output_dir.mkdir(parents=True, exist_ok=True)
        records = []
        with (output_dir / "results.jsonl").open("w", encoding="utf-8") as stream:
            for index, query in enumerate(queries, 1):
                record = run_query(query, index, args, query_path.parent)
                records.append(record)
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                stream.flush()
                print(f"[{index}/{len(queries)}] {record['kind']} HTTP={record.get('http_status', 'error')} {record['score_status']}")
        try:
            metrics_status, metrics = request_json(args.base_url.rstrip("/") + "/" + args.metrics_path.lstrip("/"), None, args.timeout)
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            metrics_status, metrics = None, {"error": str(exc)}
        if isinstance(metrics, dict) and isinstance(metrics.get("events"), list):
            metrics = {**metrics, "events": [event for event in metrics["events"]
                       if isinstance(event, dict) and event.get("book_id") == args.book_id
                       and event.get("session_id") == args.session_id]}
        (output_dir / "metrics_raw.json").write_text(json.dumps({"http_status": metrics_status, "data": metrics}, ensure_ascii=False, indent=2), encoding="utf-8")
        summary = compute_summary(records, metrics if metrics_status == 200 else None)
        summary["evaluation_session_id"] = args.session_id
        (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if all(record.get("ok") for record in records) else 1
    except (ValueError, OSError) as exc:
        print(f"评测失败: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
