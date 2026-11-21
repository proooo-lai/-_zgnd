import pytest

from storybook.video import allocate_durations, check_video_metadata, media_duration


def test_timeline_never_truncates_speech_and_obeys_frame_boundaries():
    audio = [20.1, 3, 35.2]
    durations = allocate_durations([20, 3, 30], audio, 120, 24)
    assert 120 <= sum(durations) < 120.15
    assert all(d >= a + .35 for d, a in zip(durations, audio))
    assert all(abs(d * 24 - round(d * 24)) < .0001 for d in durations)


def test_narration_overflow_is_error_not_silent_truncation():
    with pytest.raises(ValueError, match="不能截断"):
        allocate_durations([1, 1], [95, 95])


def test_actual_video_metadata_is_validated():
    probe = {"format": {"duration": "120.01"}, "streams": [
        {"codec_type": "video", "width": 1280, "height": 720, "avg_frame_rate": "24/1", "codec_name": "h264"},
        {"codec_type": "audio"}]}
    assert check_video_metadata(probe)["audio_tracks"] == 1
    probe["streams"][0]["width"] = 640
    with pytest.raises(ValueError, match="1280"):
        check_video_metadata(probe)
    probe["streams"][0]["width"] = 1280
    probe["format"]["duration"] = "90"
    with pytest.raises(ValueError, match="时长"):
        check_video_metadata(probe)


def test_duration_ignores_unknown_stream_values():
    assert media_duration({"streams": [{"duration": "N/A"}, {"duration": "3.2"}]}) == 3.2
