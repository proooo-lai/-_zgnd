"""Clearing a session also removes nested verified video frames, only for its turns."""
from storybook.storage import Store


def test_clear_history_removes_nested_frames_without_touching_other_sessions(tmp_path):
    store=Store(tmp_path/'data')
    for session,turn in [('s1','turn1'),('s2','turn2')]:
        store.append('book',session,{'turn_id':turn})
        folder=store.directory('book')/'turns'/turn/'object-location'
        folder.mkdir(parents=True)
        (folder/'frame.png').write_bytes(b'original synthetic fixture')
    external=tmp_path/'outside';external.mkdir();(external/'keep.txt').write_text('keep')
    (store.directory('book')/'turns/turn1/external-link').symlink_to(external,target_is_directory=True)
    store.clear_history('book','s1')
    assert store.history('book','s1')==[]
    assert not (store.directory('book')/'turns/turn1').exists()
    assert (store.directory('book')/'turns/turn2/object-location/frame.png').is_file()
    assert (external/'keep.txt').read_text()=='keep'
    assert store.history('book','s2')==[{'turn_id':'turn2'}]
