"""Synthetic fixtures test conversion fidelity, never represent real interactions."""

from pathlib import Path
import importlib.util
import json

import pytest


SPEC = importlib.util.spec_from_file_location("storybook_recording", Path(__file__).resolve().parents[1] / "tools" / "recording.py")
recording = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recording)


def test_protected_directory_rejected_before_resolution(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("filesystem access before rejecting forbidden path")
    monkeypatch.setattr(Path, "resolve", forbidden)
    with pytest.raises(recording.RecordingError, match="测试集"):
        recording.checked_path("F:/AI+/参赛文件/测试集/recording.webm")


def test_conversion_never_cuts_drops_or_resizes_timeline(tmp_path):
    command = recording.conversion_command("ffmpeg", tmp_path / "source.webm", tmp_path / "target.mp4", {"width": 1280, "height": 720})
    for forbidden in ("-ss", "-to", "-t", "-shortest", "-r", "-vf", "-af", "-filter_complex"):
        assert forbidden not in command
    assert "0:a?" in command
    assert command[command.index("-fps_mode:v") + 1] == "passthrough"


def test_odd_dimensions_are_preserved_without_padding(tmp_path):
    command = recording.conversion_command("ffmpeg", tmp_path / "s.webm", tmp_path / "t.mp4", {"width": 1279, "height": 719})
    assert "yuv444p" in command
    assert "-vf" not in command


@pytest.fixture
def synthetic_webm(tmp_path):
    encoder = recording.find_binary("ffmpeg")
    source = tmp_path / "synthetic_converter_fixture.webm"
    recording.run_command([encoder, "-hide_banner", "-v", "error", "-nostdin", "-f", "lavfi", "-i",
                           "testsrc2=size=320x180:rate=12:duration=1.25", "-f", "lavfi", "-i",
                           "sine=frequency=440:sample_rate=48000:duration=1.25", "-c:v", "libvpx-vp9",
                           "-deadline", "realtime", "-c:a", "libopus", str(source)])
    return source


def test_real_conversion_preserves_frames_audio_original_and_manifest(synthetic_webm):
    source_hash = recording.sha256(synthetic_webm)
    destination = synthetic_webm.with_suffix(".mp4")
    result = recording.convert_recording(synthetic_webm, destination)
    assert recording.sha256(synthetic_webm) == source_hash
    assert result["input_probe"]["video_frames"] == result["output_probe"]["video_frames"] == 15
    assert result["output_probe"]["audio_tracks"] == 1
    assert result["output_probe"]["video_codec"] == "h264"
    assert result["output_probe"]["audio_codecs"] == ["aac"]
    manifest = json.loads(destination.with_suffix(".recording.json").read_text(encoding="utf-8"))
    assert manifest["source_sha256"] == source_hash
    assert manifest["output_sha256"] == recording.sha256(destination)
    assert manifest["editing"]["cuts"] is False
    with pytest.raises(recording.RecordingError, match="已存在"):
        recording.convert_recording(synthetic_webm, destination)


def test_overlength_recording_is_rejected_not_trimmed(monkeypatch, tmp_path):
    source = tmp_path / "own_recording.webm"
    source.write_bytes(b"synthetic")
    monkeypatch.setattr(recording, "find_binary", lambda *args: "unused")
    monkeypatch.setattr(recording, "probe", lambda *args: {"duration_seconds": 301})
    with pytest.raises(recording.RecordingError, match="不会裁剪"):
        recording.convert_recording(source, source.with_suffix(".mp4"))
    assert not source.with_suffix(".mp4").exists()


def test_silent_recording_rejected_instead_of_fabricating_audio(monkeypatch, tmp_path):
    source = tmp_path / "own_silent_recording.webm"
    source.write_bytes(b"synthetic")
    monkeypatch.setattr(recording, "find_binary", lambda *args: "unused")
    monkeypatch.setattr(recording, "probe", lambda *args: {"duration_seconds": 10, "audio_tracks": 0})
    with pytest.raises(recording.RecordingError, match="没有音轨"):
        recording.convert_recording(source, source.with_suffix(".mp4"))


def test_recording_can_use_the_existing_imageio_encoder(monkeypatch, tmp_path):
    monkeypatch.setattr(recording, "ROOT", tmp_path)
    monkeypatch.setattr(recording.shutil, "which", lambda name: None)
    encoder = recording.find_binary("ffmpeg")
    assert Path(encoder).is_file()
    result = recording.run_command([encoder, "-version"])
    assert "ffmpeg version" in result.stdout
