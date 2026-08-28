"""Uploading one local file into one Frame.io folder.

The V4 local-upload flow is:

1. ``POST .../files/local_upload`` with the name and byte size. Frame.io
   creates an empty file record and hands back one presigned S3 URL per chunk.
2. ``PUT`` each chunk to its URL with ``Content-Type`` set to the ``media_type``
   Frame.io picked and ``x-amz-acl: private``.
3. Poll ``GET .../files/{id}/status`` until the upload is registered.

If a file with the same name already sits in the target folder, the new file is
stacked on top of it as a new version.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import requests

from .config import UploadConfig
from .frameio import Asset, FrameioAPIError, FrameioClient, FrameioError, USER_AGENT
from .paths import fold
from .resolver import Destination

log = logging.getLogger(__name__)


class UploadError(RuntimeError):
    """A file could not be uploaded; the caller decides whether to retry."""


@dataclass(frozen=True)
class UploadResult:
    file_id: str
    name: str
    destination: Destination
    versioned_onto: str | None
    view_url: str | None
    duration_seconds: float
    size: int


class _ChunkReader:
    """A read-only window onto part of a file, for streaming one S3 chunk."""

    def __init__(self, handle: BinaryIO, offset: int, length: int) -> None:
        self._handle = handle
        self._remaining = length
        handle.seek(offset)

    def __len__(self) -> int:
        return self._remaining

    def read(self, size: int = -1) -> bytes:
        if self._remaining <= 0:
            return b""
        want = self._remaining if size is None or size < 0 else min(size, self._remaining)
        data = self._handle.read(want)
        self._remaining -= len(data)
        return data


class Uploader:
    """Performs the three-step local upload, with retries around each chunk."""

    def __init__(
        self,
        client: FrameioClient,
        config: UploadConfig,
        *,
        session: requests.Session | None = None,
        case_sensitive_names: bool = False,
        version_stack_on_duplicate: bool = True,
    ) -> None:
        self._client = client
        self._config = config
        self._session = session or requests.Session()
        self._case_sensitive = case_sensitive_names
        self._version_stack = version_stack_on_duplicate

    def upload(self, path: Path, name: str, destination: Destination) -> UploadResult:
        started = time.monotonic()
        size = path.stat().st_size

        existing = self._find_existing(destination, name) if self._version_stack else None
        target = self._client.create_local_upload(
            destination.account_id, destination.folder_id, name, size
        )
        log.info(
            "uploading %s (%s bytes, %d chunk(s)) to %s",
            name,
            f"{size:,}",
            len(target.upload_urls),
            destination.display,
        )

        try:
            self._put_chunks(path, size, target.media_type, target.upload_urls)
            self._await_completion(destination.account_id, target.file_id)
        except Exception as exc:
            self._discard_placeholder(destination.account_id, target.file_id)
            raise UploadError(str(exc)) from exc

        versioned_onto = None
        if existing is not None:
            versioned_onto = self._stack_version(destination, existing, target.file_id)

        return UploadResult(
            file_id=target.file_id,
            name=name,
            destination=destination,
            versioned_onto=versioned_onto,
            view_url=target.view_url,
            duration_seconds=time.monotonic() - started,
            size=size,
        )

    # -- steps ------------------------------------------------------------

    def _find_existing(self, destination: Destination, name: str) -> Asset | None:
        wanted = fold(name, self._case_sensitive)
        stem = fold(Path(name).stem, self._case_sensitive)
        children = self._client.list_folder_children(
            destination.account_id, destination.folder_id
        )
        for child in children:
            if child.type == "version_stack":
                # Frame.io names a stack after its versions; match on the stem so
                # "spot_v2.mp4" stacks onto a stack created from "spot.mp4".
                if fold(child.name, self._case_sensitive) in {wanted, stem}:
                    return child
            elif child.type == "file" and fold(child.name, self._case_sensitive) == wanted:
                return child
        return None

    def _put_chunks(
        self,
        path: Path,
        size: int,
        media_type: str,
        upload_urls: tuple[tuple[str, int], ...],
    ) -> None:
        total = sum(length for _, length in upload_urls)
        if total != size:
            raise UploadError(
                f"Frame.io allocated {total} bytes of upload URLs for a "
                f"{size} byte file; the file changed while it was queued"
            )

        offset = 0
        with path.open("rb") as handle:
            for index, (url, length) in enumerate(upload_urls, start=1):
                self._put_one_chunk(handle, url, offset, length, media_type, index)
                offset += length

    def _put_one_chunk(
        self,
        handle: BinaryIO,
        url: str,
        offset: int,
        length: int,
        media_type: str,
        index: int,
    ) -> None:
        headers = {
            "Content-Type": media_type,
            "x-amz-acl": "private",
            "Content-Length": str(length),
            "User-Agent": USER_AGENT,
        }
        last_error: Exception | None = None
        for attempt in range(1, self._config.chunk_attempts + 1):
            try:
                response = self._session.put(
                    url,
                    data=_ChunkReader(handle, offset, length),
                    headers=headers,
                    timeout=(self._config.request_timeout_seconds, self._config.upload_timeout_seconds),
                )
            except requests.RequestException as exc:
                last_error = exc
            else:
                if response.status_code < 300:
                    return
                last_error = UploadError(
                    f"chunk {index} rejected with HTTP {response.status_code}: "
                    f"{response.text[:300]}"
                )
                if response.status_code < 500 and response.status_code != 429:
                    # 403 usually means the presigned URL expired; a retry with
                    # the same URL will not help, so fail fast and re-create.
                    break
            if attempt < self._config.chunk_attempts:
                time.sleep(min(30.0, 2 ** (attempt - 1) + random.uniform(0, 0.5)))
        raise UploadError(f"could not upload chunk {index}: {last_error}")

    def _await_completion(self, account_id: str, file_id: str) -> None:
        deadline = time.monotonic() + self._config.status_timeout_seconds
        while time.monotonic() < deadline:
            status = self._client.get_upload_status(account_id, file_id)
            if status.get("upload_failed"):
                raise UploadError("Frame.io reported the upload as failed")
            if status.get("upload_complete"):
                return
            time.sleep(self._config.status_poll_seconds)
        raise UploadError(
            f"Frame.io did not confirm the upload within "
            f"{self._config.status_timeout_seconds:.0f}s"
        )

    def _stack_version(
        self, destination: Destination, existing: Asset, new_file_id: str
    ) -> str | None:
        """Put the new file on top of the asset that already carries this name."""
        try:
            if existing.type == "version_stack":
                self._client.move_file(destination.account_id, new_file_id, existing.id)
                return existing.id
            stack = self._client.create_version_stack(
                destination.account_id, destination.folder_id, [existing.id, new_file_id]
            )
            return stack.id
        except (FrameioAPIError, FrameioError) as exc:
            # The upload itself succeeded; leaving the file unstacked is far
            # better than failing and uploading it a second time.
            log.warning(
                "uploaded %s but could not stack it as a new version of %s: %s",
                existing.name,
                existing.id,
                exc,
            )
            return None

    def _discard_placeholder(self, account_id: str, file_id: str) -> None:
        """Remove the empty file record left behind by a failed upload."""
        try:
            self._client.delete_file(account_id, file_id)
        except FrameioError as exc:
            log.warning("could not remove incomplete file %s from Frame.io: %s", file_id, exc)
