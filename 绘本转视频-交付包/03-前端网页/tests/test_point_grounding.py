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
             'coordinate_space':'full_frame', 'bbox_1000':[380,450,580,700], 'confidence':.95}
    provider = ScriptedProvider([reply], enabled=('vision',))
    result = asyncio.run(Interactor(store, provider, SimpleNamespace()).touch(
        'synthetic', .46, .55, 12, 's1', encoded_frame('green')))
    assert result['object']['label'] == 'moving bird'
    assert result['object']['bbox'] == pytest.approx([.38,.45,.2,.25])
    assert result['boxes'][0]['bbox'] == pytest.approx([.38,.45,.2,.25])
    images = provider.calls[0]['images']
    assert len(images) == 4
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
    provider = ScriptedProvider([{'found':True, 'label':'bird', 'coordinate_space':'full_frame', 'bbox_1000':box, 'confidence':.95}], enabled=('vision',))
    result = asyncio.run(Interactor(store, provider, SimpleNamespace()).touch(
        'synthetic', .46, .55, 12, 's1', encoded_frame()))
    assert result['uncertain'] is True
    assert result['boxes'] == []


def test_bbox_must_contain_clicked_point_after_corner_conversion(tmp_path):
    store = Store(tmp_path); make_book(store, generated=True)
    provider = ScriptedProvider([{'found':True,'label':'other bird','coordinate_space':'full_frame','bbox_1000':[50,50,150,150],'confidence':.95}], enabled=('vision',))
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
    assert len(images) == 4
    with Image.open(images[1]) as marked:
        pixel = marked.convert('RGB').getpixel((round(point[0]*(marked.width-1)),round(point[1]*(marked.height-1))))
        assert pixel[0] > 180 and pixel[1] < 70


@pytest.mark.parametrize('point', [(0,0), (1,1), (.62,.28), (.01,.95)])
def test_click_crop_records_exact_geometry_and_mapping_without_edge_padding(tmp_path, point):
    from storybook.grounding import ClickObservation, make_click_context
    original = tmp_path / 'original.png'
    Image.new('RGB', (1000,600), 'green').save(original)
    images, transform = make_click_context(original, tmp_path, *point)
    assert len(images) == 4 and images[0] == original
    left, top, width, height = transform['crop_rect']
    assert 0 <= left <= 1000-width and 0 <= top <= 600-height
    assert width == height == 390
    assert transform['frame_size'] == [1000,600]
    # Both unmarked inputs keep original pixels, even at the edge.
    with Image.open(images[2]) as crop:
        assert crop.width >= 640 and crop.getpixel((0,0)) == (0,128,0)
    observation = ClickObservation(found=True,label='synthetic object',bbox_1000=[0,0,1000,1000],
                                   coordinate_space='crop',confidence=.9)
    assert observation.video_box(transform) == pytest.approx([left/1000,top/600,width/1000,height/600])
    assert observation.video_box() is None  # Never silently treat crop coordinates as frame coordinates.


def test_localized_model_box_is_mapped_and_audited_in_original_frame(tmp_path):
    import json
    store = Store(tmp_path); make_book(store, generated=True)
    reply = {'found':True,'label':'small original object','bbox_1000':[400,400,600,600],
             'coordinate_space':'crop','confidence':.95}
    provider = ScriptedProvider([reply], enabled=('vision',))
    result = asyncio.run(Interactor(store, provider, SimpleNamespace()).touch(
        'synthetic',.5,.5,12,'s1',encoded_frame('green')))
    assert not result['uncertain']
    box = result['boxes'][0]['bbox']
    assert box[0] < .5 < box[0]+box[2] and box[1] < .5 < box[1]+box[3]
    audit = json.loads((tmp_path/'synthetic/turns'/result['turn_id']/'click-grounding.json').read_text())
    assert audit['accepted'] is True and audit['model_result'] == reply
    assert audit['transform']['frame_size'] == [160,90]
    assert 'coordinate_space' in provider.calls[0]['prompt']


@pytest.mark.parametrize('space', ['unknown', 1000, None])
def test_unknown_coordinate_spaces_are_never_guessed(tmp_path, space):
    store = Store(tmp_path); make_book(store, generated=True)
    reply = {'found':True,'label':'object','bbox_1000':[0,0,1000,1000],
             'coordinate_space':space,'confidence':.95}
    result = asyncio.run(Interactor(store,ScriptedProvider([reply],enabled=('vision',)),SimpleNamespace()).touch(
        'synthetic',.5,.5,12,'s1',encoded_frame()))
    assert result['uncertain'] and result['boxes'] == []


def test_missing_coordinate_space_is_rejected_instead_of_defaulting_to_full_frame(tmp_path):
    store=Store(tmp_path);make_book(store,generated=True)
    provider=ScriptedProvider([{'found':True,'label':'ambiguous box',
        'bbox_1000':[400,400,600,600],'confidence':.95}],enabled=('vision',))
    result=asyncio.run(Interactor(store,provider,SimpleNamespace()).touch(
        'synthetic',.5,.5,12,'s1',encoded_frame()))
    assert result['uncertain'] and result['boxes']==[]


def test_nonfinite_model_values_are_rejected_without_breaking_private_audit(tmp_path):
    import json
    store=Store(tmp_path);make_book(store,generated=True)
    provider=ScriptedProvider([{'found':True,'label':'untrusted','coordinate_space':'full_frame',
        'bbox_1000':[400,400,600,600],'confidence':float('nan')}],enabled=('vision',))
    result=asyncio.run(Interactor(store,provider,SimpleNamespace()).touch(
        'synthetic',.5,.5,12,'s1',encoded_frame()))
    assert result['uncertain'] and result['boxes']==[]
    audit=json.loads((tmp_path/'synthetic/turns'/result['turn_id']/'click-grounding.json').read_text())
    assert audit['accepted'] is False and audit['model_result']=={'invalid_json_value':True}
