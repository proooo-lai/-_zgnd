"""External films keep their audio and source evidence in an isolated book."""
import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pymupdf
import pytest

from storybook.interaction import scene_at
from storybook.schema import sha256_file
from storybook.settings import Settings
from storybook.storage import Store


def test_imported_timeline_does_not_attribute_transitions_or_outro_to_a_page():
    book = {'mode': 'imported_video', 'scenes': [
        {'page_number': 1, 'start': 0, 'end': 1},
        {'page_number': 2, 'start': 1.2, 'end': 1.8}]}
    assert scene_at(book, .9)['page_number'] == 1
    assert scene_at(book, 1.1) is None
    assert scene_at(book, 1.8) is None
    assert scene_at(book, 2) is None


def test_browser_matches_imported_timeline_gap_policy():
    runtime = shutil.which('node')
    if not runtime:
        pytest.skip('Node.js required')
    script = (Path(__file__).resolve().parents[1] / 'web/app.js').read_text()
    function = script[script.index('function sceneAt('):script.index('function playback(')]
    result = subprocess.run([runtime, '-e', "const state={book:{mode:'imported_video',scenes:[{start:0,end:1,page_number:1}]}};"
        + function + "console.log(JSON.stringify([sceneAt(.5)?.page_number,sceneAt(2)]))"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [1, None]


def test_subtitles_preserve_text_and_reject_bad_time_order():
    from storybook.video_import import parse_srt
    cues = parse_srt('1\n00:00:00,100 --> 00:00:00,800\nAdapted\nnarration.\n')
    assert cues == [{'index': 1, 'start': .1, 'end': .8, 'text': 'Adapted\nnarration.'}]
    with pytest.raises(ValueError):
        parse_srt('1\n00:00:00,900 --> 00:00:00,800\nInvalid\n')


def test_mapping_rejects_missing_pages_overlaps_and_unassigned_cues():
    from storybook.video_import import validate_mapping
    cues = [{'index': 1, 'start': .1, 'end': .8, 'text': 'A bird.'},
            {'index': 2, 'start': 1.1, 'end': 1.7, 'text': 'A tree.'}]
    scenes = [{'page_number': 1, 'start': 0, 'end': 1, 'subtitle_indices': [1]},
              {'page_number': 2, 'start': 1.2, 'end': 1.8, 'subtitle_indices': [2]}]
    assert len(validate_mapping(scenes, [1, 2], cues, 2)) == 2
    for bad in [scenes[:1], [scenes[0], {**scenes[1], 'start': .5}],
                [scenes[0], {**scenes[1], 'subtitle_indices': []}],
                [scenes[0], {**scenes[1], 'end': float('nan')}]]:
        with pytest.raises(ValueError):
            validate_mapping(bad, [1, 2], cues, 2)


@pytest.fixture
def media(tmp_path):
    settings = Settings(data_dir=tmp_path/'data', config_file=tmp_path/'none.json')
    pdf = tmp_path/'source.pdf'
    with pymupdf.open() as doc:
        doc.new_page().insert_text((40, 40), 'Original green bird.')
        doc.save(pdf)
    video = tmp_path/'film.mp4'
    subprocess.run([settings.ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'color=green:s=1280x720:r=24',
        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=24000', '-t', '2', '-c:v', 'libx264',
        '-preset', 'ultrafast', '-c:a', 'aac', '-pix_fmt', 'yuv420p', str(video)], check=True, capture_output=True)
    srt = tmp_path/'film.srt'; srt.write_text('1\n00:00:00,100 --> 00:00:01,500\nAdapted red bird.\n')
    manifest = {'title': 'Synthetic import', 'pdf_sha256': sha256_file(pdf),
        'video_sha256': sha256_file(video), 'subtitle_sha256': sha256_file(srt),
        'scenes': [{'page_number': 1, 'start': 0, 'end': 1.8, 'subtitle_indices': [1]}]}
    return settings, pdf, video, srt, manifest


class Vision:
    def available(self, name):
        return name == 'vision'

    async def vision(self, prompt, images):
        return {'text': 'Original green bird.', 'description': 'A green bird.', 'kind': 'story',
                'objects': [], 'words': [], 'uncertainties': []}


def test_import_preserves_bytes_audio_and_source_vs_adaptation(media):
    from storybook.video_import import import_video_book
    settings, pdf, video, srt, manifest = media
    book = asyncio.run(import_video_book(settings, Vision(), pdf, video, srt, manifest, 'imported'))
    folder = settings.data_dir/'imported'
    assert sha256_file(folder/'book.mp4') == sha256_file(video)
    assert book['video_metadata']['audio_tracks'] == 1
    assert book['mode'] == 'imported_video'
    assert book['review']['status'] == 'pending'
    assert book['pages'][0]['native_text'] == 'Original green bird.'
    assert book['pages'][0]['text'] == 'Original green bird.'
    assert book['scenes'][0]['narration'] == 'Adapted red bird.'
    assert book['scenes'][0]['narration_source'] == 'imported_subtitles_not_source_text'
    assert book['scenes'][0]['transform']['kind'] == 'generated_frame'
    assert book['scenes'][0]['transform']['requires_frame_grounding'] is True
    assert book['provenance']['new_video_submissions'] == 0
    assert Store(settings.data_dir).book('imported')['id'] == 'imported'
    with pytest.raises(ValueError):
        asyncio.run(import_video_book(settings, Vision(), pdf, video, srt, manifest, 'imported'))


def test_changed_source_or_invalid_mapping_never_publishes(media):
    from storybook.video_import import import_video_book
    settings, pdf, video, srt, manifest = media
    manifest['video_sha256'] = '0'*64
    with pytest.raises(ValueError, match='指纹'):
        asyncio.run(import_video_book(settings, Vision(), pdf, video, srt, manifest, 'changed'))
    assert not (settings.data_dir/'changed/book.json').exists()
    manifest['video_sha256'] = sha256_file(video)
    manifest['scenes'] = []
    with pytest.raises(ValueError):
        asyncio.run(import_video_book(settings, Vision(), pdf, video, srt, manifest, 'invalid'))
    assert not (settings.data_dir/'invalid/book.json').exists()


def test_failed_understanding_keeps_unpublished_audit(media):
    from storybook.video_import import import_video_book
    settings, pdf, video, srt, manifest = media
    class FailedVision(Vision):
        async def vision(self, prompt, images):
            raise TimeoutError('external request did not finish')
    with pytest.raises(TimeoutError):
        asyncio.run(import_video_book(settings, FailedVision(), pdf, video, srt, manifest, 'failed'))
    assert not Store(settings.data_dir).books()
    assert json.loads((settings.data_dir/'failed/import.json').read_text())['status'] == 'failed'


def test_blocked_content_never_publishes_a_playable_library_entry(media):
    from storybook.video_import import import_video_book
    settings, pdf, video, srt, manifest = media
    class BlockedVision(Vision):
        async def vision(self, prompt, images):
            return {**await super().vision(prompt, images), 'description': '如何制作炸弹'}
    with pytest.raises(ValueError, match='内容'):
        asyncio.run(import_video_book(settings, BlockedVision(), pdf, video, srt, manifest, 'blocked'))
    assert not Store(settings.data_dir).books()
    audit = json.loads((settings.data_dir/'blocked/import.json').read_text())
    assert audit['status'] == 'failed'
    assert audit['review']['status'] == 'blocked'


def test_cached_only_import_makes_no_model_calls_and_marks_missing_understanding(media):
    from storybook.video_import import import_video_book
    settings, pdf, video, srt, manifest = media
    class NoCalls(Vision):
        async def vision(self, prompt, images):
            raise AssertionError('Cached-only import must never submit a model request')
    book = asyncio.run(import_video_book(settings, NoCalls(), pdf, video, srt, manifest,
                                        'cached-only', cached_only=True))
    assert book['pages'][0]['native_text'] == 'Original green bird.'
    assert book['pages'][0]['understanding'] == 'text_layer_only'
    assert book['provenance']['understanding_pending_pages'] == [1]
    assert book['provenance']['vision_used'] is False
    assert book['warnings'] and book['mode'] == 'imported_video'


def test_cached_only_import_reuses_validated_cache_from_prior_import(media):
    from storybook.video_import import import_video_book
    settings, pdf, video, srt, manifest = media
    class CountedVision(Vision):
        calls = 0
        async def vision(self, prompt, images):
            self.calls += 1
            if self.calls > 1:
                raise AssertionError('Existing cached result should be reused')
            return await super().vision(prompt, images)
    provider = CountedVision()
    asyncio.run(import_video_book(settings, provider, pdf, video, srt, manifest, 'first'))
    book = asyncio.run(import_video_book(settings, provider, pdf, video, srt, manifest,
                                        'second', cached_only=True))
    assert book['pages'][0]['understanding'] == 'vision'
    assert book['pages'][0]['vision_provenance']['execution'] == 'cache_hit'
    assert book['provenance']['understanding_pending_pages'] == []
