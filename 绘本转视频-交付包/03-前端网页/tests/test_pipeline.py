import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest
from PIL import Image, ImageDraw

from storybook.pipeline import (assert_allowed_input, extract_pdf, merge_visual_page,
                                inspect_video_quality, prepare_video_reference, require_video_quality,
                                synthesize_narration, validate_story, validate_video_quality)
from storybook.providers import ProviderError
from storybook.schema import (contain_transform, normalize_bbox,
                              page_to_video_bbox, video_to_page_point)


def test_forbidden_directory_is_rejected_before_filesystem_resolution(monkeypatch):
    def no_resolve(self, *args, **kwargs):
        raise AssertionError("Must not inspect a forbidden path")
    monkeypatch.setattr(Path, "resolve", no_resolve)
    with pytest.raises(ValueError, match="禁止访问"):
        assert_allowed_input(Path("F:/AI+/参赛文件/测试集/never-open.pdf"))


def test_pdf_extraction_preserves_pages_words_and_official_objects(tmp_path):
    document = pymupdf.open()
    page = document.new_page(width=600, height=400)
    page.insert_text((60, 70), "A red apple.")
    annotation = page.add_rect_annot(pymupdf.Rect(120, 120, 240, 240))
    annotation.set_info(content="apple")
    annotation.update()
    page = document.new_page(width=400, height=600)
    page.insert_text((60, 70), "The end.")
    page.set_rotation(90)
    source = tmp_path / "book.pdf"
    document.save(source)
    document.close()
    pages, metadata = extract_pdf(source, tmp_path / "run", max_pages=10)
    assert metadata["page_count"] == 2
    assert [p["number"] for p in pages] == [1, 2]
    assert "apple" in pages[0]["text"]
    # PDF annotation Rect includes its one-point visible border.
    assert pages[0]["objects"][0]["bbox"] == pytest.approx([0.2, 0.3, 0.2, 0.3], abs=.006)
    assert pages[1]["image_width"] > pages[1]["image_height"]
    assert all(normalize_bbox(w["bbox"]) for p in pages for w in p["words"])


def test_vlm_removes_watermark_and_does_not_trust_text_layer_as_story():
    page = {"number": 1, "native_text": "CONFIDENTIAL watermark", "text": "CONFIDENTIAL watermark",
            "description": "", "objects": [], "words": [{"text": "CONFIDENTIAL", "bbox": [.1, .1, .1, .1]}]}
    result = {"text": "A cat is sleeping.", "description": "A black cat on a chair.",
              "kind": "story", "objects": [{"label": "cat", "bbox": [.2, .3, .3, .2]}],
              "words": [{"text": "cat", "bbox": [.15, .8, .1, .04]}]}
    merged = merge_visual_page(page, result)
    assert merged["native_text"] == "CONFIDENTIAL watermark"
    assert merged["text"] == "A cat is sleeping."
    assert merged["words"][0]["text"] == "cat"
    assert merged["objects"][0]["label"] == "cat"


def test_story_rejects_missing_duplicate_and_reordered_pages():
    pages = [{"number": 1}, {"number": 3}]
    with pytest.raises(ValueError, match="逐页覆盖"):
        validate_story({"scenes": [{"page_number": 1, "narration": "Hello"}]}, pages)
    with pytest.raises(ValueError, match="顺序"):
        validate_story({"scenes": [{"page_number": 3, "narration": "A"},
                                   {"page_number": 1, "narration": "B"}]}, pages)


def test_touch_transform_accounts_for_letterbox_and_is_reversible():
    transform = contain_transform(500, 900, 1280, 720)
    assert video_to_page_point(.01, .5, transform) is None
    box = [.1, .2, .3, .4]
    video_box = page_to_video_bbox(box, transform)
    assert video_to_page_point(video_box[0], video_box[1], transform) == pytest.approx((.1, .2))
    assert video_to_page_point(video_box[0] + video_box[2], video_box[1] + video_box[3], transform) == pytest.approx((.4, .6))


@pytest.mark.parametrize("box", [[0, 0, -1, .1], [float("nan"), 0, .1, .1], [1.2, 0, .1, .1]])
def test_invalid_model_boxes_are_not_clickable(box):
    assert normalize_bbox(box) is None


@pytest.mark.parametrize("dimensions", [(600, 1000), (1800, 600), (800, 800)])
def test_cloud_reference_keeps_all_four_corners_without_crop_or_stretch(tmp_path, dimensions):
    width, height = dimensions
    source = tmp_path / "page.png"
    image = Image.new("RGB", dimensions, "white")
    draw = ImageDraw.Draw(image)
    marker = min(width, height) // 8
    colours = [(255, 0, 0), (0, 170, 0), (0, 0, 255), (200, 0, 180)]
    for (left, top), colour in zip([(0, 0), (width-marker, 0), (0, height-marker), (width-marker, height-marker)], colours):
        draw.rectangle((left, top, left+marker-1, top+marker-1), fill=colour)
    image.save(source)
    original = source.read_bytes()
    destination = tmp_path / "references" / "reference-0001.png"
    result = prepare_video_reference(source, destination, 1280, 720)
    with Image.open(destination) as reference:
        assert reference.size == (1280, 720)
        left, top, fitted_width, fitted_height = result["content_rect"]
        assert abs(fitted_width / width - fitted_height / height) <= 1 / min(width, height)
        assert left >= 0 and top >= 0 and left + fitted_width <= 1280 and top + fitted_height <= 720
        dx = max(1, int(marker * fitted_width / width / 2))
        dy = max(1, int(marker * fitted_height / height / 2))
        sample_points = [(left+dx, top+dy), (left+fitted_width-dx-1, top+dy),
                         (left+dx, top+fitted_height-dy-1), (left+fitted_width-dx-1, top+fitted_height-dy-1)]
        for point, colour in zip(sample_points, colours):
            assert reference.getpixel(point) == colour
        assert reference.getpixel((0, 0)) == (244, 241, 233)
    assert source.read_bytes() == original
    assert result["not_valid_for_generated_frame_grounding"] is True


def test_reference_transparency_composites_on_paper_background(tmp_path):
    source = tmp_path / "transparent.png"
    Image.new("RGBA", (1280, 720), (0, 0, 0, 0)).save(source)
    destination = tmp_path / "reference.png"
    prepare_video_reference(source, destination, 1280, 720)
    with Image.open(destination) as reference:
        assert reference.getpixel((640, 360)) == (244, 241, 233)


def test_narration_prefers_dashscope_stream_and_does_not_retry_failures(tmp_path):
    class Speech:
        def mode(self, name):
            return "dashscope"
        async def tts(self, *args):
            raise AssertionError("Do not send a second paid file-based request")
        async def tts_stream(self, text, language, output, on_pcm):
            assert on_pcm is None
            raise ProviderError("stream connection interrupted")
    with pytest.raises(ProviderError, match="stream connection interrupted"):
        asyncio.run(synthesize_narration(Speech(), "测试文本", "zh", tmp_path / "speech.wav"))


def test_narration_supports_minimal_provider_without_mode_or_stream(tmp_path):
    class Speech:
        async def tts(self, text, language, output):
            assert text == "hello" and language == "en"
            return output
    destination = tmp_path / "speech.wav"
    assert asyncio.run(synthesize_narration(Speech(), "hello", "en", destination)) == destination


@pytest.mark.parametrize("result", [
    {"consistent": "false", "issues": [], "uncertain": False},
    {"consistent": True, "issues": []},
    {"consistent": True, "issues": [{"severity": "warning", "description": "change"}], "uncertain": False},
    {"consistent": True, "issues": [{"severity": "major", "description": ""}], "uncertain": False},
    {"consistent": True, "issues": [{"severity": {}, "description": "change"}], "uncertain": False},
])
def test_video_quality_does_not_accept_malformed_model_results(result):
    with pytest.raises(ValueError, match="格式无效"):
        validate_video_quality(result)


@pytest.mark.parametrize("result,decision,rejected", [
    ({"consistent": True, "issues": [], "uncertain": False}, "sampled_consistent", False),
    ({"consistent": True, "issues": [], "uncertain": True}, "needs_review", False),
    ({"consistent": False, "issues": [], "uncertain": True}, "rejected", True),
    ({"consistent": True, "issues": [{"severity": "major", "description": "Six apples appeared on the previously empty tree."}], "uncertain": False}, "rejected", True),
])
def test_video_quality_samples_real_frames_and_never_regenerates(tmp_path, monkeypatch, result, decision, rejected):
    calls, sampled_seconds = [], []
    source = tmp_path / "page.png"
    Image.new("RGB", (400, 500), "white").save(source)
    clip = tmp_path / "generated.mp4"
    clip.write_bytes(b"original generated bytes must remain intact")

    async def extract(arguments, timeout):
        assert arguments[arguments.index("-i") + 1] == str(clip)
        sampled_seconds.append(float(arguments[arguments.index("-ss") + 1]))
        Image.new("RGB", (1280, 720), "green").save(Path(arguments[-1]))
        return b""

    class Visual:
        async def vision(self, prompt, images):
            calls.append((prompt, images))
            assert images[0] == source
            assert len(images) == 4 and all(path.is_file() for path in images)
            assert "凭空新增" in prompt and "眨眼" in prompt
            return result

        async def generate_clip(self, *args):
            raise AssertionError("Quality rejection must not submit another paid video")

    monkeypatch.setattr("storybook.pipeline.run_process", extract)
    quality = asyncio.run(inspect_video_quality(source, clip, 5.0, Visual(),
        SimpleNamespace(ffmpeg="ffmpeg", fps=24), tmp_path / "book", 0))
    assert len(calls) == 1
    assert sampled_seconds == pytest.approx([0.5, 2.5, 4.92])
    assert quality["decision"] == decision
    assert quality["scope"] == "three_sampled_frames_requires_human_full_video_review"
    audit = json.loads((tmp_path / "book" / "agents" / "video-quality-0001.json").read_text(encoding="utf-8"))
    assert audit["decision"] == decision
    assert len(audit["sample_paths"]) == 3
    assert clip.read_bytes() == b"original generated bytes must remain intact"
    if rejected:
        with pytest.raises(ValueError, match="未自动重试"):
            require_video_quality(quality, 1)
    else:
        require_video_quality(quality, 1)
