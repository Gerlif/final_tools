import pytest

from frameio_export_watcher.config import UploadConfig
from frameio_export_watcher.resolver import Destination
from frameio_export_watcher.uploader import UploadError, Uploader

from fakes import FakeFrameio, FakeSession

CONFIG = UploadConfig(status_poll_seconds=0, chunk_attempts=2)


def make_uploader(api, session, **kwargs):
    return Uploader(api, CONFIG, session=session, **kwargs)


def destination_for(api):
    project = api.add_project("2026")
    folder = api.add_folder(project.root_folder_id, "Kundecase #0711")
    return Destination(
        account_id="acc-1",
        project_id=project.id,
        project_name=project.name,
        folder_id=folder.id,
        folder_path=("Kundecase #0711",),
    )


def test_uploads_every_chunk_with_the_headers_s3_requires(tmp_path):
    api = FakeFrameio(chunk_size=8)
    session = FakeSession()
    destination = destination_for(api)
    path = tmp_path / "spot.mp4"
    payload = bytes(range(20))
    path.write_bytes(payload)

    result = make_uploader(api, session).upload(path, "spot.mp4", destination)

    assert len(session.puts) == 3
    assert b"".join(body for _, body, _ in session.puts) == payload
    for _, _, headers in session.puts:
        assert headers["Content-Type"] == "video/mp4"
        assert headers["x-amz-acl"] == "private"
    assert [headers["Content-Length"] for _, _, headers in session.puts] == ["8", "8", "4"]
    assert result.versioned_onto is None


def test_same_name_is_stacked_as_a_new_version(tmp_path):
    api = FakeFrameio()
    session = FakeSession()
    destination = destination_for(api)
    existing = api.add_file(destination.folder_id, "spot.mp4")
    path = tmp_path / "spot.mp4"
    path.write_bytes(b"x" * 4)

    result = make_uploader(api, session).upload(path, "spot.mp4", destination)

    stacked = [call for call in api.calls if call[0] == "version_stack"]
    assert stacked and stacked[0][2] == (existing.id, result.file_id)
    assert result.versioned_onto is not None


def test_new_version_joins_an_existing_stack(tmp_path):
    api = FakeFrameio()
    session = FakeSession()
    destination = destination_for(api)
    stack = api.add_file(destination.folder_id, "spot.mp4", asset_type="version_stack")
    path = tmp_path / "spot.mp4"
    path.write_bytes(b"x" * 4)

    result = make_uploader(api, session).upload(path, "spot.mp4", destination)

    assert ("move", result.file_id, stack.id) in api.calls
    assert not [call for call in api.calls if call[0] == "version_stack"]


def test_version_stacking_can_be_switched_off(tmp_path):
    api = FakeFrameio()
    session = FakeSession()
    destination = destination_for(api)
    api.add_file(destination.folder_id, "spot.mp4")
    path = tmp_path / "spot.mp4"
    path.write_bytes(b"x" * 4)

    make_uploader(api, session, version_stack_on_duplicate=False).upload(
        path, "spot.mp4", destination
    )
    assert not [call for call in api.calls if call[0] in {"version_stack", "move"}]


def test_a_rejected_chunk_removes_the_placeholder(tmp_path):
    api = FakeFrameio()
    session = FakeSession(status_code=403)
    destination = destination_for(api)
    path = tmp_path / "spot.mp4"
    path.write_bytes(b"x" * 4)

    with pytest.raises(UploadError):
        make_uploader(api, session).upload(path, "spot.mp4", destination)

    assert [call for call in api.calls if call[0] == "delete"]


def test_a_failed_upload_status_is_an_error(tmp_path):
    api = FakeFrameio()
    session = FakeSession()
    destination = destination_for(api)
    path = tmp_path / "spot.mp4"
    path.write_bytes(b"x" * 4)

    original = api.create_local_upload

    def failing(*args, **kwargs):
        target = original(*args, **kwargs)
        api.fail_status_for.add(target.file_id)
        return target

    api.create_local_upload = failing
    with pytest.raises(UploadError):
        make_uploader(api, session).upload(path, "spot.mp4", destination)
