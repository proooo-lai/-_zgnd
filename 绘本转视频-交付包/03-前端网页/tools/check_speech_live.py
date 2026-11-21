"""Small live ASR/TTS check using two authored, non-dataset questions.

Keys are read through Settings and never printed or written to the report.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storybook.providers import Provider, ProviderError
from storybook.settings import Settings
from storybook.video import media_duration, probe_media, run_process


async def main():
    settings = Settings()
    provider = Provider(settings)
    # 进度产物写在工程内、已被 .gitignore 忽略的 artifacts/，不再写到工程外的 work/
    output = ROOT / "artifacts" / "live_validation"
    output.mkdir(parents=True, exist_ok=True)
    report = {"test_source": "two_self_authored_questions_not_competition_queries",
              "models": {name: {key: provider.config(name).get(key, "") for key in ("kind", "model", "voice")}
                         for name in ("tts", "asr")},
              "measurement": "API_processing_and_download_not_end_to_end_audible_latency",
              "cases": []}
    try:
        for language, question in (("zh", "小鸟看见了什么？"), ("en", "What color is the apple?")):
            case = {"language": language, "question": question}
            stage = "tts"
            started = time.perf_counter()
            try:
                wav = await provider.tts(question, language, output / f"question-{language}.wav")
                case["tts_ms"] = round((time.perf_counter() - started) * 1000)
                original = await probe_media(wav, settings.ffprobe)
                stream = next(s for s in original["streams"] if s.get("codec_type") == "audio")
                case["tts_audio"] = {key: stream.get(key) for key in ("codec_name", "sample_rate", "channels", "bits_per_sample")}
                case["tts_audio"]["duration"] = media_duration(original)
                stage = "normalize_audio"
                normalized = output / f"question-{language}-16k.wav"
                await run_process([settings.ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(wav),
                                   "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(normalized)], timeout=30)
                metadata = await probe_media(normalized, settings.ffprobe)
                stream = next(s for s in metadata["streams"] if s.get("codec_type") == "audio")
                case["asr_input"] = {key: stream.get(key) for key in ("codec_name", "sample_rate", "channels", "bits_per_sample")}
                stage = "asr"
                started = time.perf_counter()
                case["transcript"] = await provider.asr(normalized)
                case["asr_ms"] = round((time.perf_counter() - started) * 1000)
                case["status"] = "completed"
            except Exception as error:
                case.update(status="failed", failed_stage=stage,
                            error=str(error) if isinstance(error, ProviderError) else type(error).__name__)
            report["cases"].append(case)
            (output / "speech.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(case, ensure_ascii=False), flush=True)
    finally:
        await provider.close()


if __name__ == "__main__":
    asyncio.run(main())
