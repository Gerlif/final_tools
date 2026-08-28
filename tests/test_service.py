"""End-to-end: disk -> mapping -> upload -> state, with a fake Frame.io."""

from pathlib import Path

from frameio_export_watcher.config import (
    AppConfig,
    AuthConfig,
    FrameioConfig,
    StabilityConfig,
    UploadConfig,
    WatchConfig,
)
from frameio_export_watcher.paths import parse_template
from frameio_export_watcher.resolver import DestinationResolver
from frameio_export_watcher.service import WatcherService
from frameio_export_watcher.state import (
    STATUS_BASELINE,
    STATUS_NO_MATCH,
    STATUS_UPLOADED,
    StateStore,
)
from frameio_export_watcher.uploader import Uploader

from fakes import FakeFrameio, FakeSession

FIELD_TEMPLATE = "{year}/{client}/{case}/Projektfiler/Eksport"


def make_config(tmp_path: Path, **overrides) -> AppConfig:
    config = AppConfig(
        watch=WatchConfig(
            root=tmp_path / "share",
            export_template=parse_template(FIELD_TEMPLATE),
            poll_interval_seconds=1,
            stability=StabilityConfig(min_age_seconds=0, checks=1, interval_seconds=0),
        ),
        frameio=FrameioConfig(
            project_template=parse_template("{year}"),
            folder_template=parse_template("{client}/{case}"),
            account_id="acc-1",
        ),
        upload=UploadConfig(
            status_poll_seconds=0,
            max_concurrent_files=1,
            retry_backoff_seconds=0,
            chunk_attempts=1,
        ),
        auth=AuthConfig(mode="legacy", legacy_token="t"),
        state_db=tmp_path / "state" / "state.db",
        heartbeat_file=tmp_path / "state" / "heartbeat",
    )
    for key, value in overrides.items():
        object.__setattr__(config, key, value)
    return config


def build(tmp_path, *, api=None, config=None):
    api = api or FakeFrameio()
    config = config or make_config(tmp_path)
    session = FakeSession()
    state = StateStore(config.state_db)
    resolver = DestinationResolver(api, config.frameio)
    uploader = Uploader(
        api,
        config.upload,
        session=session,
        version_stack_on_duplicate=config.frameio.version_stack_on_duplicate,
    )
    service = WatcherService(config, api, state, uploader, resolver)
    return api, session, state, service, config


def make_export(root: Path, year="2026", client="Beierholm", case="Kundecase #0711") -> Path:
    export = root / year / client / case / "Projektfiler" / "Eksport"
    export.mkdir(parents=True, exist_ok=True)
    return export


def mirror_on_frameio(api: FakeFrameio, year="2026", client="Beierholm", case="Kundecase #0711"):
    project = api.add_project(year)
    client_folder = api.add_folder(project.root_folder_id, client)
    return api.add_folder(client_folder.id, case)


def test_a_finished_export_lands_in_the_matching_frameio_folder(tmp_path):
    api, session, state, service, config = build(tmp_path)
    target = mirror_on_frameio(api)
    export = make_export(config.watch.root)
    (export / "spot_v1.mp4").write_bytes(b"a" * 40)

    stats = service.run_cycle(wait=True)

    assert (stats.uploaded, stats.failed, stats.skipped_no_match) == (1, 0, 0)
    assert session.puts, "nothing was PUT to the presigned URLs"
    assert any(
        call[0] == "create_local_upload" and call[1] == target.id for call in api.calls
    )
    assert state.get(str(export / "spot_v1.mp4")).status == STATUS_UPLOADED


def test_nothing_is_uploaded_without_a_matching_frameio_folder(tmp_path):
    api, session, state, service, config = build(tmp_path)
    api.add_project("2026")  # the client and case folders are missing
    export = make_export(config.watch.root)
    (export / "spot.mp4").write_bytes(b"a" * 10)

    stats = service.run_cycle(wait=True)

    assert (stats.uploaded, stats.skipped_no_match) == (0, 1)
    assert not session.puts
    assert state.get(str(export / "spot.mp4")).status == STATUS_NO_MATCH


def test_a_file_is_never_uploaded_twice(tmp_path):
    api, session, state, service, config = build(tmp_path)
    mirror_on_frameio(api)
    export = make_export(config.watch.root)
    (export / "spot.mp4").write_bytes(b"a" * 10)

    service.run_cycle(wait=True)
    second = service.run_cycle(wait=True)

    assert second.queued == 0
    assert len([c for c in api.calls if c[0] == "create_local_upload"]) == 1


def test_a_re_export_is_uploaded_as_a_new_version(tmp_path):
    api, session, state, service, config = build(tmp_path)
    mirror_on_frameio(api)
    export = make_export(config.watch.root)
    path = export / "spot.mp4"
    path.write_bytes(b"a" * 10)
    service.run_cycle(wait=True)

    # Same filename, new render.
    path.write_bytes(b"b" * 20)
    stats = service.run_cycle(wait=True)

    assert stats.uploaded == 1
    assert [c for c in api.calls if c[0] == "version_stack"]


def test_dry_run_resolves_but_uploads_nothing(tmp_path):
    config = make_config(tmp_path, dry_run=True)
    api, session, state, service, _ = build(tmp_path, config=config)
    mirror_on_frameio(api)
    export = make_export(config.watch.root)
    (export / "spot.mp4").write_bytes(b"a" * 10)

    stats = service.run_cycle(wait=True)

    assert stats.uploaded == 0
    assert not session.puts
    assert state.get(str(export / "spot.mp4")) is None


def test_only_files_under_a_matching_export_path_are_touched(tmp_path):
    api, session, state, service, config = build(tmp_path)
    mirror_on_frameio(api)
    other = config.watch.root / "2026" / "Beierholm" / "Kundecase #0711" / "Projektfiler" / "Grafik"
    other.mkdir(parents=True)
    (other / "logo.png").write_bytes(b"a" * 10)

    stats = service.run_cycle(wait=True)

    assert stats.seen == 0
    assert not session.puts


def test_a_failing_upload_is_recorded_and_retried(tmp_path):
    api, session, state, service, config = build(tmp_path)
    mirror_on_frameio(api)
    export = make_export(config.watch.root)
    path = export / "spot.mp4"
    path.write_bytes(b"a" * 10)

    session.status_code = 500
    first = service.run_cycle(wait=True)
    assert first.failed == 1
    assert state.get(str(path)).attempts == 1

    session.status_code = 200
    second = service.run_cycle(wait=True)
    assert second.uploaded == 1
    assert state.get(str(path)).status == STATUS_UPLOADED


def test_heartbeat_is_written(tmp_path):
    api, session, state, service, config = build(tmp_path)
    service._heartbeat()
    assert config.heartbeat_file.exists()


def test_baseline_marks_existing_files_so_the_backlog_is_never_uploaded(tmp_path):
    api, session, state, service, config = build(tmp_path)
    mirror_on_frameio(api)
    export = make_export(config.watch.root)
    for name in ("gammel_1.mp4", "gammel_2.mp4", "gammel_3.mp4"):
        (export / name).write_bytes(b"a" * 10)

    assert service.baseline() == 3

    stats = service.run_cycle(wait=True)
    assert (stats.queued, stats.uploaded) == (0, 0)
    assert not session.puts
    assert state.counts() == {STATUS_BASELINE: 3}


def test_files_arriving_after_a_baseline_are_uploaded(tmp_path):
    api, session, state, service, config = build(tmp_path)
    mirror_on_frameio(api)
    export = make_export(config.watch.root)
    (export / "gammel.mp4").write_bytes(b"a" * 10)
    service.baseline()

    (export / "ny.mp4").write_bytes(b"b" * 10)
    stats = service.run_cycle(wait=True)

    assert stats.uploaded == 1
    assert state.get(str(export / "ny.mp4")).status == STATUS_UPLOADED
    assert state.get(str(export / "gammel.mp4")).status == STATUS_BASELINE


def test_baseline_leaves_an_earlier_upload_alone(tmp_path):
    api, session, state, service, config = build(tmp_path)
    mirror_on_frameio(api)
    export = make_export(config.watch.root)
    path = export / "spot.mp4"
    path.write_bytes(b"a" * 10)
    service.run_cycle(wait=True)

    assert service.baseline() == 0
    assert state.get(str(path)).status == STATUS_UPLOADED


def test_baseline_dry_run_records_nothing(tmp_path):
    api, session, state, service, config = build(tmp_path)
    mirror_on_frameio(api)
    export = make_export(config.watch.root)
    (export / "gammel.mp4").write_bytes(b"a" * 10)

    assert service.baseline(dry_run=True) == 1
    assert state.counts() == {}


def test_a_baselined_file_can_be_released_again(tmp_path):
    api, session, state, service, config = build(tmp_path)
    mirror_on_frameio(api)
    export = make_export(config.watch.root)
    path = export / "gammel.mp4"
    path.write_bytes(b"a" * 10)
    service.baseline()

    # This is what `retry --status baseline` does.
    state.forget(str(path))

    assert service.run_cycle(wait=True).uploaded == 1


def test_a_subfolder_is_recreated_on_frameio_and_the_file_lands_in_it(tmp_path):
    api, session, state, service, config = build(tmp_path)
    case_folder = mirror_on_frameio(api)
    export = make_export(config.watch.root)
    (export / "Hero").mkdir()
    (export / "Hero" / "Fil.mp4").write_bytes(b"a" * 10)

    object.__setattr__(config.watch, "recursive", True)
    stats = service.run_cycle(wait=True)

    assert stats.uploaded == 1
    created = [c for c in api.calls if c[0] == "create_folder"]
    assert created == [("create_folder", case_folder.id, "Hero")]

    hero = next(c for c in api.children[case_folder.id] if c.name == "Hero")
    uploads = [c for c in api.calls if c[0] == "create_local_upload"]
    assert uploads == [("create_local_upload", hero.id, "Fil.mp4", 10)]


def test_an_existing_subfolder_is_reused_not_duplicated(tmp_path):
    api, session, state, service, config = build(tmp_path)
    case_folder = mirror_on_frameio(api)
    api.add_folder(case_folder.id, "Hero")
    export = make_export(config.watch.root)
    (export / "Hero").mkdir()
    (export / "Hero" / "Fil.mp4").write_bytes(b"a" * 10)

    object.__setattr__(config.watch, "recursive", True)
    service.run_cycle(wait=True)

    assert not [c for c in api.calls if c[0] == "create_folder"]
    assert len([f for f in api.children[case_folder.id] if f.name == "Hero"]) == 1


def test_nested_subfolders_are_mirrored_in_order(tmp_path):
    api, session, state, service, config = build(tmp_path)
    case_folder = mirror_on_frameio(api)
    export = make_export(config.watch.root)
    deep = export / "Hero" / "16x9"
    deep.mkdir(parents=True)
    (deep / "Fil.mp4").write_bytes(b"a" * 10)

    object.__setattr__(config.watch, "recursive", True)
    service.run_cycle(wait=True)

    created = [c for c in api.calls if c[0] == "create_folder"]
    assert [c[2] for c in created] == ["Hero", "16x9"]
    assert created[0][1] == case_folder.id
    hero = next(c for c in api.children[case_folder.id] if c.name == "Hero")
    assert created[1][1] == hero.id


def test_subfolders_are_flattened_when_creation_is_switched_off(tmp_path):
    api, session, state, service, config = build(tmp_path)
    case_folder = mirror_on_frameio(api)
    export = make_export(config.watch.root)
    (export / "Hero").mkdir()
    (export / "Hero" / "Fil.mp4").write_bytes(b"a" * 10)

    object.__setattr__(config.watch, "recursive", True)
    object.__setattr__(config.frameio, "create_subfolders", False)
    service.run_cycle(wait=True)

    assert not [c for c in api.calls if c[0] == "create_folder"]
    uploads = [c for c in api.calls if c[0] == "create_local_upload"]
    assert uploads == [("create_local_upload", case_folder.id, "Fil.mp4", 10)]


def test_a_missing_case_folder_is_still_never_created(tmp_path):
    api, session, state, service, config = build(tmp_path)
    api.add_project("2026")  # client and case folders absent on Frame.io
    export = make_export(config.watch.root)
    (export / "Hero").mkdir()
    (export / "Hero" / "Fil.mp4").write_bytes(b"a" * 10)

    object.__setattr__(config.watch, "recursive", True)
    stats = service.run_cycle(wait=True)

    assert (stats.uploaded, stats.skipped_no_match) == (0, 1)
    assert not [c for c in api.calls if c[0] == "create_folder"]


def test_dry_run_reports_the_subfolder_without_creating_it(tmp_path):
    config = make_config(tmp_path, dry_run=True)
    api, session, state, service, _ = build(tmp_path, config=config)
    mirror_on_frameio(api)
    export = make_export(config.watch.root)
    (export / "Hero").mkdir()
    (export / "Hero" / "Fil.mp4").write_bytes(b"a" * 10)

    object.__setattr__(config.watch, "recursive", True)
    service.run_cycle(wait=True)

    assert not [c for c in api.calls if c[0] == "create_folder"]
    assert not session.puts
