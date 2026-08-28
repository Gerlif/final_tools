"""The watch loop: scan, decide, upload, remember."""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .config import AppConfig
from .frameio import FrameioClient
from .resolver import Destination, DestinationResolver, NoMatch, ResolveError
from .scanner import Candidate, ExportScanner, StabilityTracker
from .state import (
    STATUS_BASELINE,
    STATUS_FAILED,
    STATUS_GIVEN_UP,
    STATUS_NO_MATCH,
    STATUS_UPLOADED,
    StateStore,
)
from .uploader import UploadError, Uploader

log = logging.getLogger(__name__)

_TERMINAL_STATUSES = frozenset(
    {STATUS_UPLOADED, STATUS_NO_MATCH, STATUS_GIVEN_UP, STATUS_BASELINE}
)


@dataclass
class CycleStats:
    """Counters for one scan. Upload workers update them, so guard with a lock."""

    seen: int = 0
    queued: int = 0
    uploaded: int = 0
    skipped_no_match: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def count(self, field_name: str, error: str | None = None) -> None:
        with self._lock:
            setattr(self, field_name, getattr(self, field_name) + 1)
            if error:
                self.errors.append(error)


class WatcherService:
    """Ties the scanner, resolver, uploader and state store together."""

    def __init__(
        self,
        config: AppConfig,
        client: FrameioClient,
        state: StateStore,
        uploader: Uploader,
        resolver: DestinationResolver,
    ) -> None:
        self._config = config
        self._client = client
        self._state = state
        self._uploader = uploader
        self._resolver = resolver
        self._scanner = ExportScanner(config.watch)
        self._stability = StabilityTracker(config.watch.stability)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._in_flight: set[str] = set()
        self._executor = ThreadPoolExecutor(
            max_workers=config.upload.max_concurrent_files,
            thread_name_prefix="upload",
        )

    # -- lifecycle --------------------------------------------------------

    def stop(self) -> None:
        log.info("stop requested; finishing in-flight uploads")
        self._stop.set()

    def run_forever(self) -> None:
        log.info(
            "watching %s for %s",
            self._config.watch.root,
            self._config.watch.export_template.raw,
        )
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                stats = self.run_cycle()
                log.info(
                    "scan done: %d file(s) seen, %d queued, %d in flight",
                    stats.seen,
                    stats.queued,
                    len(self._in_flight),
                )
            except ResolveError as exc:
                log.error("configuration or account problem: %s", exc)
            except Exception:  # keep the daemon alive across unexpected errors
                log.exception("scan failed")
            self._heartbeat()
            elapsed = time.monotonic() - started
            self._stop.wait(max(1.0, self._config.watch.poll_interval_seconds - elapsed))
        self._executor.shutdown(wait=True)
        log.info("watcher stopped")

    # -- one pass ---------------------------------------------------------

    def run_cycle(self, *, wait: bool = False, wait_for_stability: bool = False) -> CycleStats:
        """Scan once and queue whatever is ready. Optionally wait for the uploads."""
        stats = CycleStats()
        futures: list[Future[None]] = []
        seen_paths: set[str] = set()

        for candidate in self._scanner.scan():
            stats.seen += 1
            seen_paths.add(str(candidate.path))
            if not self._should_consider(candidate):
                continue
            if not self._is_ready(candidate, wait_for_stability=wait_for_stability):
                continue
            if not self._claim(candidate.path):
                continue
            stats.queued += 1
            futures.append(self._executor.submit(self._process, candidate, stats))

        self._stability.prune(seen_paths)
        if wait:
            for future in futures:
                future.result()
        return stats

    def baseline(self, *, dry_run: bool = False) -> int:
        """Record every file present right now as already handled.

        Pointing the watcher at an archive that already holds finished exports
        would otherwise upload the whole backlog on the second scan. Running
        this once before the first start makes "from now on" the starting
        point. Files recorded this way can be released again with
        ``retry --status baseline``.
        """
        marked = 0
        for candidate in self._scanner.scan():
            record = self._state.get(str(candidate.path))
            if record is not None and record.matches(candidate.size, candidate.mtime_ns):
                # Already uploaded, skipped or baselined in this exact form.
                continue
            if not dry_run:
                self._state.record(
                    str(candidate.path),
                    size=candidate.size,
                    mtime_ns=candidate.mtime_ns,
                    status=STATUS_BASELINE,
                    error="already present when the watcher was set up",
                )
            marked += 1
        return marked

    def _is_ready(self, candidate: Candidate, *, wait_for_stability: bool) -> bool:
        if self._stability.observe(candidate):
            return True
        if not wait_for_stability:
            return False

        # One-shot mode has no second scan to fall back on, so watch the file
        # actively for as long as the stability settings require.
        stability = self._config.watch.stability
        deadline = time.time() + stability.min_age_seconds + stability.interval_seconds * (
            stability.checks + 1
        )
        while time.time() < deadline and not self._stop.is_set():
            time.sleep(min(stability.interval_seconds, 5.0))
            try:
                stat = candidate.path.stat()
            except OSError:
                return False
            refreshed = Candidate(
                path=candidate.path,
                export_dir=candidate.export_dir,
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
            )
            if self._stability.observe(refreshed):
                return True
        log.info("%s is still changing; leaving it for the next scan", candidate.path)
        return False

    def _should_consider(self, candidate: Candidate) -> bool:
        record = self._state.get(str(candidate.path))
        if record is None or not record.matches(candidate.size, candidate.mtime_ns):
            return True
        if record.status in _TERMINAL_STATUSES:
            return False
        if record.status == STATUS_FAILED:
            backoff = min(
                self._config.upload.retry_backoff_max_seconds,
                self._config.upload.retry_backoff_seconds * (2 ** max(0, record.attempts - 1)),
            )
            if time.time() - record.updated_at < backoff:
                return False
        return True

    def _claim(self, path: Path) -> bool:
        with self._lock:
            key = str(path)
            if key in self._in_flight:
                return False
            self._in_flight.add(key)
            return True

    def _release(self, path: Path) -> None:
        with self._lock:
            self._in_flight.discard(str(path))

    # -- per file ---------------------------------------------------------

    def _process(self, candidate: Candidate, stats: CycleStats) -> None:
        path = candidate.path
        try:
            # Re-stat right before uploading: the file may have been rewritten
            # or removed between the scan and this worker picking it up.
            try:
                stat = path.stat()
            except OSError as exc:
                log.info("%s disappeared before upload (%s)", path, exc)
                return
            if stat.st_size != candidate.size or stat.st_mtime_ns != candidate.mtime_ns:
                log.info("%s changed while queued; waiting for it to settle", path)
                self._stability.forget(path)
                return

            outcome = self._resolver.resolve(candidate.export_dir.fields)
            if isinstance(outcome, NoMatch):
                stats.count("skipped_no_match")
                log.info("skipping %s: %s", path, outcome.reason)
                self._state.record(
                    str(path),
                    size=candidate.size,
                    mtime_ns=candidate.mtime_ns,
                    status=STATUS_NO_MATCH,
                    error=outcome.reason,
                )
                return

            subpath = (
                candidate.subpath if self._config.frameio.create_subfolders else ()
            )

            if self._config.dry_run:
                log.info(
                    "[dry-run] would upload %s (%s bytes) to %s",
                    path,
                    f"{candidate.size:,}",
                    "/".join((outcome.display, *subpath)),
                )
                return

            # Folders below the export folder are created on demand; the
            # project/client/case folders above it never are.
            destination = (
                self._resolver.resolve_subfolder(outcome, subpath)
                if subpath
                else outcome
            )
            self._upload(candidate, destination, stats)
        except Exception as exc:  # noqa: BLE001 - a worker must never die silently
            stats.count("failed", error=f"{path}: {exc}")
            attempts = self._state.bump_failure(
                str(path),
                size=candidate.size,
                mtime_ns=candidate.mtime_ns,
                error=str(exc),
                max_attempts=self._config.upload.max_attempts,
            )
            if attempts >= self._config.upload.max_attempts:
                log.error(
                    "giving up on %s after %d attempts: %s", path, attempts, exc
                )
            else:
                log.warning("upload of %s failed (attempt %d): %s", path, attempts, exc)
        finally:
            self._release(path)

    def _upload(
        self, candidate: Candidate, destination: Destination, stats: CycleStats
    ) -> None:
        try:
            result = self._uploader.upload(candidate.path, candidate.name, destination)
        except UploadError:
            raise
        stats.count("uploaded")
        self._state.record(
            str(candidate.path),
            size=candidate.size,
            mtime_ns=candidate.mtime_ns,
            status=STATUS_UPLOADED,
            file_id=result.file_id,
            folder_id=destination.folder_id,
            error=None,
            attempts=0,
        )
        log.info(
            "uploaded %s to %s in %.1fs%s",
            candidate.name,
            destination.display,
            result.duration_seconds,
            " (new version)" if result.versioned_onto else "",
        )

    # -- misc -------------------------------------------------------------

    def _heartbeat(self) -> None:
        target = self._config.heartbeat_file
        if not target:
            return
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(int(time.time())), encoding="utf-8")
        except OSError as exc:
            log.warning("could not write heartbeat file %s: %s", target, exc)
