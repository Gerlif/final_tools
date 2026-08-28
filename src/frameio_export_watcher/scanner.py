"""Finding export folders on disk and deciding when a file is done being written."""

from __future__ import annotations

import fnmatch
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .config import SYSTEM_IGNORE_PATTERNS, StabilityConfig, WatchConfig
from .paths import Segment, normalize

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExportDir:
    """An ``.../Projektfiler/Eksport`` directory and the fields extracted from its path."""

    path: Path
    fields: dict[str, str]

    @property
    def key(self) -> str:
        return "/".join(self.fields[name] for name in sorted(self.fields))


@dataclass(frozen=True)
class Candidate:
    """A file inside an export directory that may need uploading."""

    path: Path
    export_dir: ExportDir
    size: int
    mtime_ns: int

    @property
    def name(self) -> str:
        return normalize(self.path.name)

    @property
    def subpath(self) -> tuple[str, ...]:
        """The folders between the export folder and this file, if any."""
        try:
            relative = self.path.parent.relative_to(self.export_dir.path)
        except (AttributeError, ValueError):
            return ()
        return tuple(normalize(part) for part in relative.parts)


class ExportScanner:
    """Walks the watch root down the template, level by level.

    Only the directories the template can possibly match are visited, so a
    deep production archive is not walked in full on every poll.
    """

    def __init__(self, config: WatchConfig) -> None:
        self._config = config

    def find_export_dirs(self) -> list[ExportDir]:
        root = self._config.root
        if not root.is_dir():
            log.error("watch root %s does not exist or is not a directory", root)
            return []

        found: list[ExportDir] = [ExportDir(path=root, fields={})]
        for segment in self._config.export_template.segments:
            found = [
                child
                for current in found
                for child in self._expand(current, segment)
            ]
            if not found:
                break
        return found

    def _expand(self, current: ExportDir, segment: Segment) -> Iterator[ExportDir]:
        if segment.is_literal:
            # A literal segment is a direct lookup; no directory listing needed.
            child = current.path / segment.raw
            if child.is_dir():
                yield ExportDir(path=child, fields=current.fields)
            return

        try:
            entries = sorted(os.scandir(current.path), key=lambda e: e.name)
        except OSError as exc:
            log.warning("cannot list %s: %s%s", current.path, exc, _access_hint(exc))
            return

        for entry in entries:
            if self._is_ignored(entry.name):
                continue
            try:
                if not entry.is_dir():
                    continue
            except OSError:
                continue
            matched = segment.match(entry.name)
            if matched is None:
                continue
            yield ExportDir(path=Path(entry.path), fields={**current.fields, **matched})

    def scan(self) -> Iterator[Candidate]:
        """Yield every file in every export directory that is not ignored."""
        for export_dir in self.find_export_dirs():
            yield from self._scan_dir(export_dir, export_dir.path)

    def _scan_dir(self, export_dir: ExportDir, directory: Path) -> Iterator[Candidate]:
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError as exc:
            log.warning("cannot list %s: %s%s", directory, exc, _access_hint(exc))
            return

        for entry in entries:
            if self._is_ignored(entry.name):
                continue
            try:
                if entry.is_dir():
                    if self._config.recursive:
                        yield from self._scan_dir(export_dir, Path(entry.path))
                    continue
                if not entry.is_file():
                    continue
                stat = entry.stat()
            except OSError as exc:
                log.debug("skipping %s: %s", entry.path, exc)
                continue

            if stat.st_size < self._config.min_file_size_bytes:
                continue
            yield Candidate(
                path=Path(entry.path),
                export_dir=export_dir,
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
            )

    def _is_ignored(self, name: str) -> bool:
        """True for temp files, and for NAS/OS bookkeeping in any case."""
        return any(
            fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(name.lower(), pattern.lower())
            for pattern in (*SYSTEM_IGNORE_PATTERNS, *self._config.ignore_patterns)
        )


class StabilityTracker:
    """Decides whether a file has stopped changing.

    A file counts as finished when its size and mtime have been identical
    across ``checks`` observations spread over at least ``interval_seconds``,
    and the file is at least ``min_age_seconds`` old. This survives slow SMB
    copies and applications that write in bursts, which inotify alone does not.
    """

    def __init__(
        self, config: StabilityConfig, open_files: "OpenFileIndex | None" = None
    ) -> None:
        self._config = config
        self._seen: dict[str, tuple[int, int, int, float]] = {}
        self._open_files = open_files or OpenFileIndex()

    def observe(self, candidate: Candidate, now: float | None = None) -> bool:
        """Record an observation; return True when the file looks complete."""
        now = time.time() if now is None else now
        key = str(candidate.path)
        previous = self._seen.get(key)

        if previous and previous[0] == candidate.size and previous[1] == candidate.mtime_ns:
            count, first_seen = previous[2] + 1, previous[3]
        else:
            count, first_seen = 1, now
        self._seen[key] = (candidate.size, candidate.mtime_ns, count, first_seen)

        age = now - (candidate.mtime_ns / 1_000_000_000)
        if age < self._config.min_age_seconds:
            return False
        if count < self._config.checks:
            return False
        if now - first_seen < self._config.interval_seconds:
            return False
        if self._config.use_open_handle_check and self._open_files.holds(candidate.path):
            log.debug("%s still has an open file handle", candidate.path)
            return False
        return True

    def forget(self, path: Path) -> None:
        self._seen.pop(str(path), None)

    def prune(self, keep: set[str]) -> None:
        """Drop observations for files that no longer exist."""
        for key in list(self._seen):
            if key not in keep:
                del self._seen[key]


def _access_hint(exc: OSError) -> str:
    """Name the user we run as, so a denied mount points at PUID/PGID."""
    if not isinstance(exc, PermissionError):
        return ""
    return (
        f" -- this container runs as uid={os.getuid()} gid={os.getgid()}, "
        "which cannot read that path; set PUID/PGID in docker-compose.yml to a "
        "user that can, then rebuild with --build"
    )


class OpenFileIndex:
    """Which files any visible process currently holds open.

    Matching is by (device, inode), not by path: the watcher sees the share as
    /data/... while the Samba process on the NAS has the very same file open as
    /volume1/..., so the two paths never compare equal. The inode does.

    One walk of /proc answers for every file in a scan, so the snapshot is
    cached for a couple of seconds rather than rebuilt per file.
    """

    def __init__(self, cache_seconds: float = 2.0) -> None:
        self._cache_seconds = cache_seconds
        self._expires = 0.0
        self._open: set[tuple[int, int]] = set()
        self._warned = False

    def holds(self, path: Path) -> bool:
        try:
            stat = os.stat(path)
        except OSError:
            return False
        return (stat.st_dev, stat.st_ino) in self._snapshot()

    def _snapshot(self) -> set[tuple[int, int]]:
        now = time.monotonic()
        if now < self._expires:
            return self._open
        self._open, processes = _open_inodes()
        self._expires = now + self._cache_seconds
        if processes <= 2 and not self._warned:
            self._warned = True
            log.warning(
                "the open-handle check can only see %d process(es); without "
                "`pid: host` in docker-compose.yml it cannot see the NAS's file "
                "server and will never report a file as still being written",
                processes,
            )
        return self._open


def _open_inodes() -> tuple[set[tuple[int, int]], int]:
    """Every (device, inode) held open by a visible process, and the count."""
    open_inodes: set[tuple[int, int]] = set()
    processes = 0

    proc = Path("/proc")
    if not proc.is_dir():
        return open_inodes, 0

    for pid_dir in proc.iterdir():
        if not pid_dir.name.isdigit():
            continue
        processes += 1
        try:
            descriptors = list((pid_dir / "fd").iterdir())
        except OSError:
            # The process exited, or is not ours to inspect.
            continue
        for descriptor in descriptors:
            try:
                stat = os.stat(descriptor)
            except OSError:
                continue
            open_inodes.add((stat.st_dev, stat.st_ino))
    return open_inodes, processes
