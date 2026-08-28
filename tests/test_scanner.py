import os
import time
from pathlib import Path

import pytest

from frameio_export_watcher.config import StabilityConfig, WatchConfig
from frameio_export_watcher.paths import parse_template
from frameio_export_watcher.scanner import (
    Candidate,
    ExportScanner,
    OpenFileIndex,
    StabilityTracker,
)


def build_tree(root: Path) -> Path:
    export = root / "2026" / "Beierholm" / "Kundecase #0711" / "Projektfiler" / "Eksport"
    export.mkdir(parents=True)
    (root / "2026" / "Beierholm" / "Kundecase #0711" / "Projektfiler" / "Grafik").mkdir()
    (root / "2025" / "Andet" / "Sag 1" / "Projektfiler" / "Eksport").mkdir(parents=True)
    (root / ".@__thumb").mkdir()
    return export


def make_config(root: Path, **overrides) -> WatchConfig:
    defaults = dict(
        root=root,
        export_template=parse_template("{year}/{client}/{case}/Projektfiler/Eksport"),
        stability=StabilityConfig(min_age_seconds=0, checks=1, interval_seconds=0),
    )
    defaults.update(overrides)
    return WatchConfig(**defaults)


def test_finds_every_export_folder_and_its_fields(tmp_path):
    export = build_tree(tmp_path)
    dirs = ExportScanner(make_config(tmp_path)).find_export_dirs()
    paths = {d.path for d in dirs}
    assert export in paths
    assert len(dirs) == 2
    fields = next(d.fields for d in dirs if d.path == export)
    assert fields == {"year": "2026", "client": "Beierholm", "case": "Kundecase #0711"}


def test_hidden_synology_folders_are_not_traversed(tmp_path):
    build_tree(tmp_path)
    hidden = tmp_path / ".@__thumb" / "X" / "Y" / "Projektfiler" / "Eksport"
    hidden.mkdir(parents=True)
    dirs = ExportScanner(make_config(tmp_path)).find_export_dirs()
    assert all(".@__thumb" not in str(d.path) for d in dirs)


def test_scan_skips_temp_files_and_empty_files(tmp_path):
    export = build_tree(tmp_path)
    (export / "spot.mp4").write_bytes(b"x" * 100)
    (export / "spot.mp4.tmp").write_bytes(b"x" * 100)
    (export / ".DS_Store").write_bytes(b"x")
    (export / "empty.mov").write_bytes(b"")
    (export / "renders").mkdir()
    (export / "renders" / "deep.mov").write_bytes(b"x" * 10)

    names = {c.name for c in ExportScanner(make_config(tmp_path)).scan()}
    assert names == {"spot.mp4"}


def test_recursive_mode_includes_subfolders(tmp_path):
    export = build_tree(tmp_path)
    (export / "renders").mkdir()
    (export / "renders" / "deep.mov").write_bytes(b"x" * 10)

    scanner = ExportScanner(make_config(tmp_path, recursive=True))
    assert {c.name for c in scanner.scan()} == {"deep.mov"}


def _candidate(path: Path, size: int, mtime_ns: int) -> Candidate:
    return Candidate(path=path, export_dir=None, size=size, mtime_ns=mtime_ns)  # type: ignore[arg-type]


def test_growing_file_is_not_ready_until_it_stops(tmp_path):
    tracker = StabilityTracker(
        StabilityConfig(min_age_seconds=10, checks=2, interval_seconds=30)
    )
    now = time.time()
    old_mtime = int((now - 60) * 1_000_000_000)
    path = tmp_path / "spot.mp4"

    # Still being written: size keeps changing.
    assert tracker.observe(_candidate(path, 100, old_mtime), now=now) is False
    assert tracker.observe(_candidate(path, 200, old_mtime + 1), now=now + 30) is False
    # Settled, but not yet seen long enough.
    assert tracker.observe(_candidate(path, 200, old_mtime + 1), now=now + 40) is False
    # Same size and mtime, two observations, 30s apart.
    assert tracker.observe(_candidate(path, 200, old_mtime + 1), now=now + 61) is True


def test_a_freshly_written_file_waits_for_min_age(tmp_path):
    tracker = StabilityTracker(
        StabilityConfig(min_age_seconds=60, checks=1, interval_seconds=0)
    )
    now = time.time()
    path = tmp_path / "spot.mp4"
    fresh = _candidate(path, 10, int((now - 5) * 1_000_000_000))
    assert tracker.observe(fresh, now=now) is False
    assert tracker.observe(fresh, now=now + 120) is True


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="needs /proc")
def test_open_file_index_sees_a_file_this_process_holds_open(tmp_path):
    path = tmp_path / "spot.mp4"
    path.write_bytes(b"x" * 10)
    index = OpenFileIndex(cache_seconds=0)

    assert index.holds(path) is False
    with path.open("rb"):
        assert index.holds(path) is True
    assert index.holds(path) is False


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="needs /proc")
def test_open_file_index_matches_a_second_path_to_the_same_file(tmp_path):
    """A hard link stands in for the bind mount: same inode, different path."""
    path = tmp_path / "spot.mp4"
    path.write_bytes(b"x" * 10)
    other = tmp_path / "same_file_other_path.mp4"
    os.link(path, other)
    index = OpenFileIndex(cache_seconds=0)

    with path.open("rb"):
        # Held open under one path, asked about under the other.
        assert index.holds(other) is True


def test_a_file_being_written_is_not_ready_when_the_handle_check_is_on(tmp_path):
    path = tmp_path / "spot.mp4"
    path.write_bytes(b"x" * 10)
    old = int((time.time() - 600) * 1_000_000_000)
    os.utime(path, ns=(old, old))
    tracker = StabilityTracker(
        StabilityConfig(
            min_age_seconds=0,
            checks=1,
            interval_seconds=0,
            use_open_handle_check=True,
        ),
        # No caching, so the second look sees the handle already closed.
        open_files=OpenFileIndex(cache_seconds=0),
    )
    candidate = _candidate(path, 10, old)

    with path.open("rb"):
        assert tracker.observe(candidate) is False
    assert tracker.observe(candidate) is True


def test_synology_bookkeeping_is_never_uploaded(tmp_path):
    """@eaDir and its sidecars appear in every share and are not deliverables."""
    export = build_tree(tmp_path)
    (export / "spot.mp4").write_bytes(b"x" * 100)

    eadir = export / "@eaDir"
    eadir.mkdir()
    (eadir / "spot.mp4@SynoEAStream").write_bytes(b"x" * 394)
    (eadir / "SYNOPHOTO_THUMB_M.jpg").write_bytes(b"x" * 50)
    (export / "spot.mp4@SynoEAStream").write_bytes(b"x" * 394)
    (export / "#recycle").mkdir()
    (export / "#recycle" / "gammel.mov").write_bytes(b"x" * 50)

    scanner = ExportScanner(make_config(tmp_path, recursive=True))
    assert {c.name for c in scanner.scan()} == {"spot.mp4"}


def test_system_noise_is_ignored_even_with_custom_ignore_patterns(tmp_path):
    """A user list replaces the defaults; it must not re-enable @eaDir."""
    export = build_tree(tmp_path)
    (export / "spot.mp4").write_bytes(b"x" * 100)
    (export / "@eaDir").mkdir()
    (export / "@eaDir" / "spot.mp4@SynoEAStream").write_bytes(b"x" * 394)

    scanner = ExportScanner(
        make_config(tmp_path, recursive=True, ignore_patterns=("*.aaf",))
    )
    assert {c.name for c in scanner.scan()} == {"spot.mp4"}


def test_eadir_is_not_mistaken_for_a_year_or_client_folder(tmp_path):
    build_tree(tmp_path)
    stray = tmp_path / "@eaDir" / "Kunde" / "Sag" / "Projektfiler" / "Eksport"
    stray.mkdir(parents=True)
    (stray / "junk.mp4").write_bytes(b"x" * 100)

    dirs = ExportScanner(make_config(tmp_path)).find_export_dirs()
    assert all("@eaDir" not in str(d.path) for d in dirs)


class _StubIndex:
    """An open-handle index with controllable answers."""

    def __init__(self, *, held: bool, usable: bool = True) -> None:
        self._held = held
        self.is_usable = usable

    def holds(self, path) -> bool:
        return self._held


def _tracker(*, held: bool, usable: bool = True, **stability):
    defaults = dict(
        min_age_seconds=0,
        checks=2,
        interval_seconds=15,
        use_open_handle_check=True,
        upload_as_soon_as_closed=True,
    )
    defaults.update(stability)
    return StabilityTracker(
        StabilityConfig(**defaults), open_files=_StubIndex(held=held, usable=usable)
    )


def test_a_closed_file_is_ready_on_the_very_first_sighting(tmp_path):
    tracker = _tracker(held=False)
    now = time.time()
    candidate = _candidate(tmp_path / "spot.mp4", 100, int((now - 30) * 1_000_000_000))

    # No second observation, no 15 second wait.
    assert tracker.observe(candidate, now=now) is True


def test_a_file_still_open_is_never_ready_however_long_it_sits(tmp_path):
    tracker = _tracker(held=True)
    now = time.time()
    candidate = _candidate(tmp_path / "spot.mp4", 100, int((now - 3600) * 1_000_000_000))

    assert tracker.observe(candidate, now=now) is False
    assert tracker.observe(candidate, now=now + 600) is False


def test_the_fast_path_still_respects_min_age(tmp_path):
    tracker = _tracker(held=False, min_age_seconds=30)
    now = time.time()
    fresh = _candidate(tmp_path / "spot.mp4", 100, int((now - 5) * 1_000_000_000))

    assert tracker.observe(fresh, now=now) is False
    assert tracker.observe(fresh, now=now + 60) is True


def test_an_unusable_index_falls_back_to_watching_the_file(tmp_path):
    """Without pid: host, 'nobody has it open' is meaningless -- do not trust it."""
    tracker = _tracker(held=False, usable=False)
    now = time.time()
    candidate = _candidate(tmp_path / "spot.mp4", 100, int((now - 30) * 1_000_000_000))

    assert tracker.observe(candidate, now=now) is False
    assert tracker.observe(candidate, now=now + 20) is True


def test_without_the_fast_path_the_old_rule_applies(tmp_path):
    tracker = _tracker(held=False, upload_as_soon_as_closed=False)
    now = time.time()
    candidate = _candidate(tmp_path / "spot.mp4", 100, int((now - 30) * 1_000_000_000))

    assert tracker.observe(candidate, now=now) is False
    assert tracker.observe(candidate, now=now + 20) is True
