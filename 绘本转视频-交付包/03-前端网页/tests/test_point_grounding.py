"""Unambiguous click images and explicit 0..1000 corner coordinates."""
import asyncio
from types import SimpleNamespace

import pytest
from PIL import Image

from storybook.interaction import Interactor
from storybook.storage import Store
from tests.test_interaction import ScriptedProvider, encoded_frame, make_book


def test_click_uses_original_and_visible_point_with_converted_box(tmp_path):
    store = Store(tmp_path); make_book(store, generated=True)
    reply = {'found':True, 'label':'moving bird', 'description':'A blue bird.',
             'bbox_1000':[380,450,580,700], 'confidence':.95}
    provider = ScriptedProvider([reply], enabled=('vision',))
    result = asyncio.run(Interactor(store, provider, SimpleNamespace()).touch(
        'synthetic', .46, .55, 12, 's1', encoded_frame('green')))
    assert result['object']['label'] == 'moving bird'
    assert result['object']['bbox'] == pytest.approx([.38,.45,.2,.25])
    assert result['boxes'][0]['bbox'] == pytest.approx([.38,.45,.2,.25])
    images = provider.calls[0]['images']
    assert len(images) == 2
    with Image.open(images[0]) as original, Image.open(images[1]) as marked:
        assert marked.size == original.size
        xy = round(.46*(original.width-1)), round(.55*(original.height-1))
        assert original.convert('RGB').getpixel(xy)[1] > 100
        pixel = marked.convert('RGB').getpixel(xy)
        assert pixel[0] > 180 and pixel[1] < 70
        assert marked.convert('RGB').getpixel((0,0)) == original.convert('RGB').getpixel((0,0))


@pytest.mark.parametrize('box', [
    [380,450,580], [380,450,580,700,800], [580,450,380,700],
    [-1,0,100,100], [0,0,1001,100], [False,0,100,100],
    [.38,.45,.58,.7], [0,0,0,100], ['380',450,580,700],
])
def test_invalid_model_coordinates_are_rejected_without_guessing_units(tmp_path, box):
    store = Store(tmp_path); make_book(store, generated=True)
    provider = ScriptedProvider([{'found':True, 'label':'bird', 'bbox_1000':box, 'confidence':.95}], enabled=('vision',))
    result = asyncio.run(Interactor(store, provider, SimpleNamespace()).touch(
        'synthetic', .46, .55, 12, 's1', encoded_frame()))
    assert result['uncertain'] is True
    assert result['boxes'] == []


def test_bbox_must_contain_clicked_point_after_corner_conversion(tmp_path):
    store = Store(tmp_path); make_book(store, generated=True)
    provider = ScriptedProvider([{'found':True,'label':'other bird','bbox_1000':[50,50,150,150],'confidence':.95}], enabled=('vision',))
    result = asyncio.run(Interactor(store, provider, SimpleNamespace()).touch(
        'synthetic', .46, .55, 12, 's1', encoded_frame()))
    assert result['uncertain'] is True and result['boxes'] == []


@pytest.mark.parametrize('point', [(0,0),(1,1)])
def test_visible_marker_remains_inside_image_at_edges(tmp_path, point):
    store = Store(tmp_path); make_book(store, generated=True)
    provider = ScriptedProvider([{'found':False}], enabled=('vision',))
    result = asyncio.run(Interactor(store, provider, SimpleNamespace()).touch(
        'synthetic', *point, 12, 's1', encoded_frame('green')))
    assert result['uncertain'] is True
    images = provider.calls[0]['images']
    assert len(images) == 2
    with Image.open(images[1]) as marked:
        pixel = marked.convert('RGB').getpixel((round(point[0]*(marked.width-1)),round(point[1]*(marked.height-1))))
        assert pixel[0] > 180 and pixel[1] < 70
