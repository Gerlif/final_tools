import time
from pathlib import Path

from frameio_export_watcher.config import StabilityConfig, WatchConfig
from frameio_export_watcher.paths import parse_template
from frameio_export_watcher.scanner import Candidate, ExportScanner, StabilityTracker


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
