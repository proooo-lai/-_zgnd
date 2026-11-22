"""Decode a real original video through text and voice HTTP entry points."""
import asyncio
import json
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, ImageFont, ImageStat
import pytest

from storybook.server import create_app
from storybook.video import run_process
from tests.test_interaction import ScriptedProvider, make_book, encoded_frame
from tests.test_server import settings_for, wav_bytes


def seed_video(app):
    book = make_book(app.state.store, generated=True)
    book['duration'] = 6
    book['scenes'] = [{**book['scenes'][0], 'start': 0, 'end': 6}]
    app.state.store.save(book)
    folder = app.state.store.directory(book['id'])
    blank = Image.new('RGB', (640, 360), 'white')
    blank.save(folder/'blank.png')
    written = blank.copy()
    ImageDraw.Draw(written).text((160, 130), 'apple', fill='black', font=ImageFont.load_default(size=64))
    written.save(folder/'written.png')
    manifest = folder/'frames.txt'
    manifest.write_text("file 'blank.png'\nduration 2\nfile 'written.png'\nduration 2\nfile 'blank.png'\nduration 2\nfile 'blank.png'\n")
    asyncio.run(run_process([app.state.settings.ffmpeg, '-y', '-hide_banner', '-loglevel', 'error',
        '-f', 'concat', '-safe', '0', '-i', str(manifest), '-t', '6', '-vf', 'fps=12,tpad=stop_mode=clone:stop_duration=1', '-c:v', 'libx264',
        '-pix_fmt', 'yuv420p', str(folder/'book.mp4')]))
    return book


class PixelProvider(ScriptedProvider):
    """The external model boundary; actual frame extraction remains real."""
    def __init__(self):
        super().__init__(enabled=('vision', 'asr'))

    @staticmethod
    def has_ink(path):
        with Image.open(path).convert('RGB') as im:
            assert im.size == (640, 360) and min(im.getpixel((0, 0))) > 240
            return ImageStat.Stat(im.crop((160, 130, 384, 216)).convert('L')).mean[0] < 240

    async def vision(self, prompt, images):
        if 'VIDEO_TEXT_LOCATE_V1' in prompt:
            readable = [self.has_ink(image) for image in images]
            assert readable == [False, True, False]
            return {'uncertain': False, 'matches': [{'frame_index': 1, 'text': 'apple',
                'bbox_1000': [250, 350, 600, 600], 'confidence': .97}]}
        assert 'VIDEO_TEXT_REVIEW_V1' in prompt and self.has_ink(images[0])
        return {'visible': True, 'uncertain': False, 'text': 'apple',
                'bbox_1000': [250, 350, 600, 600], 'confidence': .97}

    async def asr(self, audio):
        assert Path(audio).is_file()
        return '高亮“apple”'


@pytest.mark.parametrize('voice', [False, True])
def test_http_search_and_voice_seek_to_text_in_middle_not_opening_or_uploaded_frame(tmp_path, voice):
    app = create_app(settings_for(tmp_path), PixelProvider())
    seed_video(app)
    with TestClient(app) as client:
        if voice:
            response = client.post('/api/books/synthetic/voice', data={
                'session_id': 'test', 'time': '0', 'frame': encoded_frame('red')},
                files={'audio': ('query.wav', wav_bytes(), 'audio/wav')})
        else:
            response = client.post('/api/books/synthetic/ask', json={
                'question': '查找“apple”', 'session_id': 'test', 'time': 0, 'frame': encoded_frame('red')})
        assert response.status_code == 200, response.text
        result = response.json()
        assert not result['uncertain'] and result['seek_time'] == 3
        assert result['boxes'][0]['presentation'] == ('highlight' if voice else 'outline')
        location = result['text_location']
        assert location['frame_size'] == [640, 360] and location['sampled_frames'] == 3
        assert client.get(location['frame_url']).status_code == 200
        turn = app.state.store.history('synthetic', 'test')[-1]
        assert turn['text_location']['frame_sha256'] == location['frame_sha256']
        if voice:
            assert turn['transcript'] == '高亮“apple”' and turn['input_kind'] == 'voice'
        record = app.state.store.directory('synthetic')/'turns'/turn['turn_id']/'text-location/search.json'
        assert json.loads(record.read_text())['status'] == 'found'
