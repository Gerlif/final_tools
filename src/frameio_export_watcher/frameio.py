"""A small client for the parts of the Frame.io V4 API this tool needs.

Endpoints used (see https://next.developer.frame.io/platform):

    GET   /v4/accounts
    GET   /v4/accounts/{account_id}/workspaces
    GET   /v4/accounts/{account_id}/projects
    GET   /v4/accounts/{account_id}/folders/{folder_id}/children
    POST  /v4/accounts/{account_id}/folders/{folder_id}/folders
    POST  /v4/accounts/{account_id}/folders/{folder_id}/files/local_upload
    GET   /v4/accounts/{account_id}/files/{file_id}/status
    POST  /v4/accounts/{account_id}/folders/{folder_id}/version_stacks
    PATCH /v4/accounts/{account_id}/files/{file_id}/move
"""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterator

import requests

from urllib.parse import urljoin, urlsplit, urlunsplit

from .auth import TokenProvider

log = logging.getLogger(__name__)

USER_AGENT = "final-frameio-export-watcher/1.0"

# The V4 API is rate limited per account user; the tightest limits we touch are
# 5 calls/second (local_upload, status) and 100 calls/minute (listings).
_RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


class FrameioError(RuntimeError):
    """Any failure while talking to Frame.io."""


class FrameioAPIError(FrameioError):
    """A non-successful HTTP response from the API."""

    def __init__(self, status_code: int, method: str, url: str, body: str) -> None:
        super().__init__(f"{method} {url} failed with HTTP {status_code}: {body[:400]}")
        self.status_code = status_code
        self.body = body


@dataclass(frozen=True)
class Asset:
    """A file, folder or version stack as returned by the children endpoint."""

    id: str
    name: str
    type: str
    parent_id: str | None = None
    file_size: int | None = None
    media_type: str | None = None
    status: str | None = None
    view_url: str | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "Asset":
        return cls(
            id=data["id"],
            name=data.get("name", ""),
            type=data.get("type", "file"),
            parent_id=data.get("parent_id"),
            file_size=data.get("file_size"),
            media_type=data.get("media_type"),
            status=data.get("status"),
            view_url=data.get("view_url"),
        )


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    root_folder_id: str
    workspace_id: str
    status: str | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "Project":
        return cls(
            id=data["id"],
            name=data.get("name", ""),
            root_folder_id=data["root_folder_id"],
            workspace_id=data.get("workspace_id", ""),
            status=data.get("status"),
        )


@dataclass(frozen=True)
class UploadTarget:
    """The placeholder file plus the presigned chunk URLs to PUT into."""

    file_id: str
    media_type: str
    upload_urls: tuple[tuple[str, int], ...]
    view_url: str | None = None


class RateLimiter:
    """Keeps a minimum interval between API calls across all worker threads."""

    def __init__(self, min_interval_seconds: float) -> None:
        self._min_interval = max(0.0, min_interval_seconds)
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def wait(self) -> None:
        if not self._min_interval:
            return
        with self._lock:
            now = time.monotonic()
            delay = self._next_allowed - now
            self._next_allowed = max(now, self._next_allowed) + self._min_interval
        if delay > 0:
            time.sleep(delay)


class FrameioClient:
    """Thin, retrying wrapper around the Frame.io V4 REST API."""

    def __init__(
        self,
        token_provider: TokenProvider,
        *,
        base_url: str,
        session: requests.Session | None = None,
        rate_limiter: RateLimiter | None = None,
        timeout: float = 60.0,
        max_attempts: int = 5,
    ) -> None:
        self._tokens = token_provider
        self._base_url = base_url.rstrip("/")
        self._session = session or requests.Session()
        self._limiter = rate_limiter or RateLimiter(0.25)
        self._timeout = timeout
        self._max_attempts = max_attempts

    # -- plumbing ---------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._tokens.token()}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        headers.update(self._tokens.extra_headers)
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = path if path.startswith("http") else f"{self._base_url}{path}"
        last_error: Exception | None = None
        refreshed = False

        for attempt in range(1, self._max_attempts + 1):
            self._limiter.wait()
            try:
                response = self._session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=self._headers(),
                    timeout=self._timeout,
                )
            except requests.RequestException as exc:
                last_error = FrameioError(f"{method} {url} failed: {exc}")
                self._sleep_before_retry(attempt, None)
                continue

            if response.status_code == 401 and not refreshed:
                # The cached token expired or was revoked; get a fresh one once.
                refreshed = True
                self._tokens.invalidate()
                continue

            if response.status_code in _RETRY_STATUSES and attempt < self._max_attempts:
                last_error = FrameioAPIError(
                    response.status_code, method, url, response.text
                )
                self._sleep_before_retry(attempt, response.headers.get("Retry-After"))
                continue

            if response.status_code >= 400:
                raise FrameioAPIError(response.status_code, method, url, response.text)

            if not response.content:
                return {}
            try:
                return response.json()
            except ValueError as exc:
                raise FrameioError(f"{method} {url} returned invalid JSON") from exc

        raise last_error or FrameioError(f"{method} {url} failed after retries")

    def _sleep_before_retry(self, attempt: int, retry_after: str | None) -> None:
        if retry_after:
            try:
                time.sleep(min(60.0, float(retry_after)))
                return
            except ValueError:
                pass
        time.sleep(min(30.0, (2 ** (attempt - 1)) + random.uniform(0, 0.5)))

    def _absolute(self, link: str) -> str:
        """Turn a pagination link into an absolute URL.

        Frame.io returns ``links.next`` either absolute or as a root-relative
        path; joining the latter onto the versioned base URL would duplicate
        the ``/v4`` prefix.
        """
        if link.startswith("http"):
            return link
        parsed = urlsplit(self._base_url)
        origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
        if link.startswith("/"):
            return urljoin(origin, link)
        return urljoin(f"{self._base_url}/", link)

    def _paginate(
        self, path: str, params: dict[str, Any] | None = None
    ) -> Iterator[dict[str, Any]]:
        query = dict(params or {})
        query.setdefault("page_size", 50)
        next_path: str | None = path
        while next_path:
            payload = self._request("GET", next_path, params=query)
            for item in payload.get("data") or []:
                yield item
            link = (payload.get("links") or {}).get("next")
            next_path = self._absolute(link) if link else None
            # The cursor is baked into the ``next`` link; sending the original
            # query params again would override it.
            query = {}

    # -- resources --------------------------------------------------------

    def list_accounts(self) -> list[dict[str, Any]]:
        return list(self._paginate("/accounts"))

    def list_workspaces(self, account_id: str) -> list[dict[str, Any]]:
        return list(self._paginate(f"/accounts/{account_id}/workspaces"))

    def list_projects(self, account_id: str) -> list[Project]:
        return [
            Project.from_api(item)
            for item in self._paginate(f"/accounts/{account_id}/projects")
        ]

    def list_folder_children(
        self, account_id: str, folder_id: str, *, asset_type: str | None = None
    ) -> list[Asset]:
        params = {"type": asset_type} if asset_type else None
        return [
            Asset.from_api(item)
            for item in self._paginate(
                f"/accounts/{account_id}/folders/{folder_id}/children", params
            )
        ]

    def create_folder(self, account_id: str, parent_id: str, name: str) -> Asset:
        payload = self._request(
            "POST",
            f"/accounts/{account_id}/folders/{parent_id}/folders",
            json_body={"data": {"name": name}},
        )
        return Asset.from_api(payload.get("data") or {})

    def create_local_upload(
        self, account_id: str, folder_id: str, name: str, file_size: int
    ) -> UploadTarget:
        payload = self._request(
            "POST",
            f"/accounts/{account_id}/folders/{folder_id}/files/local_upload",
            json_body={"data": {"name": name, "file_size": file_size}},
        )
        data = payload.get("data") or {}
        urls = tuple(
            (item["url"], int(item["size"])) for item in data.get("upload_urls") or []
        )
        if not urls:
            raise FrameioError(
                f"Frame.io returned no upload URLs for {name!r}; "
                "the file placeholder cannot be filled"
            )
        return UploadTarget(
            file_id=data["id"],
            media_type=data.get("media_type") or "application/octet-stream",
            upload_urls=urls,
            view_url=data.get("view_url"),
        )

    def get_upload_status(self, account_id: str, file_id: str) -> dict[str, Any]:
        payload = self._request("GET", f"/accounts/{account_id}/files/{file_id}/status")
        return payload.get("data") or {}

    def delete_file(self, account_id: str, file_id: str) -> None:
        self._request("DELETE", f"/accounts/{account_id}/files/{file_id}")

    def create_version_stack(
        self, account_id: str, folder_id: str, file_ids: list[str]
    ) -> Asset:
        payload = self._request(
            "POST",
            f"/accounts/{account_id}/folders/{folder_id}/version_stacks",
            json_body={"data": {"file_ids": file_ids}},
        )
        return Asset.from_api(payload.get("data") or {})

    def move_file(self, account_id: str, file_id: str, parent_id: str) -> Asset:
        payload = self._request(
            "PATCH",
            f"/accounts/{account_id}/files/{file_id}/move",
            json_body={"data": {"parent_id": parent_id}},
        )
        return Asset.from_api(payload.get("data") or {})
