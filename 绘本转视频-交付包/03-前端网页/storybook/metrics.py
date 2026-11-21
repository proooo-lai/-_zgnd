"""Shared acoustic-record checks for HTTP storage and offline evaluation.

These validate record consistency, not authenticity of the physical capture.
"""
import math


ACOUSTIC_EVENTS = {"audio_loopback": "loopback_onset", "actual_audible": "acoustic_onset"}


def finite_number(value):
    return type(value) in (int, float) and math.isfinite(value)


def acoustic_record_error(item, *, require_warmup=False):
    kind = item.get("measurement_kind")
    if not isinstance(kind, str) or kind not in ACOUSTIC_EVENTS:
        return "not_verified_audible_measurement"
    if item.get("measurement_source") != kind or item.get("playback_event") != ACOUSTIC_EVENTS[kind]:
        return "inconsistent_measurement_source"
    if item.get("input_kind") not in ("voice", "mic"):
        return "not_live_voice_input"
    if require_warmup and item.get("warmup_complete") is not True:
        return "warmup_not_confirmed"
    clock = item.get("clock_domain")
    if item.get("same_clock") is not True or not isinstance(clock, str) or not clock.strip():
        return "clock_alignment_not_confirmed"
    start, end = item.get("speech_end_ms"), item.get("first_audible_ms")
    if not finite_number(start) or not finite_number(end) or start < 0 or end < start:
        return "invalid_timestamps"
    latency = item.get("latency_ms")
    if not finite_number(latency) or not 0 <= latency <= 3_600_000 or abs(latency - (end - start)) > 1:
        return "inconsistent_latency"
    return None
