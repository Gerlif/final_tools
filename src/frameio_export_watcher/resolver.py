"""Mapping a server path to the matching folder on Frame.io.

    /volume1/AktiveProjekter/2026/Beierholm/Kundecase #0711/Projektfiler/Eksport
      -> project "2026"  ->  folder "Beierholm"  ->  folder "Kundecase #0711"

If any step has no counterpart on Frame.io the mapping fails and nothing is
uploaded: the project, client and case folders are created by the production
bot, never by this tool.

Folders *below* the export folder are the exception. Those mirror whatever an
editor put under Eksport/, so they are created on demand -- see
``resolve_subfolder``.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, replace

from .config import FrameioConfig
from .frameio import FrameioClient, FrameioError, Project
from .paths import TemplateError, fold

log = logging.getLogger(__name__)


class ResolveError(RuntimeError):
    """Raised when the mapping cannot be attempted at all (config/API problem)."""


@dataclass(frozen=True)
class Destination:
    """Where a file should land on Frame.io."""

    account_id: str
    project_id: str
    project_name: str
    folder_id: str
    folder_path: tuple[str, ...]

    @property
    def display(self) -> str:
        return "/".join((self.project_name,) + self.folder_path)


@dataclass(frozen=True)
class NoMatch:
    """The server folder has no counterpart on Frame.io."""

    reason: str


class DestinationResolver:
    """Resolves and caches server-path -> Frame.io-folder mappings."""

    def __init__(self, client: FrameioClient, config: FrameioConfig) -> None:
        self._client = client
        self._config = config
        self._lock = threading.RLock()
        self._account_id: str | None = config.account_id
        self._workspace_id: str | None = None
        self._projects: dict[str, Project] = {}
        self._projects_expire = 0.0
        self._cache: dict[tuple[str, ...], tuple[Destination | NoMatch, float]] = {}
        self._subfolders: dict[tuple[str, str], tuple[str, float]] = {}

    # -- account / workspace ---------------------------------------------

    def account_id(self) -> str:
        with self._lock:
            if self._account_id:
                return self._account_id
            accounts = self._client.list_accounts()
            if not accounts:
                raise ResolveError("the credentials have access to no Frame.io accounts")
            if len(accounts) > 1:
                names = ", ".join(
                    f"{a.get('display_name')} ({a.get('id')})" for a in accounts
                )
                raise ResolveError(
                    "the credentials see several Frame.io accounts; set "
                    f"frameio.account_id (or FRAMEIO_ACCOUNT_ID) to one of: {names}"
                )
            self._account_id = accounts[0]["id"]
            log.info(
                "using Frame.io account %s (%s)",
                accounts[0].get("display_name"),
                self._account_id,
            )
            return self._account_id

    def _workspace_filter(self) -> str | None:
        if not self._config.workspace_name:
            return None
        with self._lock:
            if self._workspace_id:
                return self._workspace_id
            wanted = fold(self._config.workspace_name, self._config.case_sensitive_names)
            for workspace in self._client.list_workspaces(self.account_id()):
                if fold(workspace.get("name", ""), self._config.case_sensitive_names) == wanted:
                    self._workspace_id = workspace["id"]
                    return self._workspace_id
            raise ResolveError(
                f"no workspace named {self._config.workspace_name!r} in this account"
            )

    # -- projects ---------------------------------------------------------

    def _project_index(self) -> dict[str, Project]:
        with self._lock:
            now = time.monotonic()
            if self._projects and now < self._projects_expire:
                return self._projects
            workspace_id = self._workspace_filter()
            index: dict[str, Project] = {}
            for project in self._client.list_projects(self.account_id()):
                if workspace_id and project.workspace_id != workspace_id:
                    continue
                index.setdefault(
                    fold(project.name, self._config.case_sensitive_names), project
                )
            self._projects = index
            self._projects_expire = now + self._config.lookup_cache_seconds
            log.debug("cached %d Frame.io projects", len(index))
            return index

    # -- resolution -------------------------------------------------------

    def resolve(self, fields: dict[str, str]) -> Destination | NoMatch:
        """Map extracted path fields to a Frame.io folder."""
        try:
            project_parts = self._config.project_template.render(fields)
            folder_parts = (
                self._config.folder_template.render(fields)
                if self._config.folder_template
                else ()
            )
        except TemplateError as exc:
            raise ResolveError(str(exc)) from exc

        if len(project_parts) != 1:
            raise ResolveError(
                "frameio.project_template must resolve to a single project name, "
                f"got {'/'.join(project_parts)!r}"
            )
        project_name = project_parts[0]
        cache_key = (project_name, *folder_parts)

        with self._lock:
            cached = self._cache.get(cache_key)
            if cached and time.monotonic() < cached[1]:
                return cached[0]

        outcome = self._resolve_uncached(project_name, folder_parts)
        ttl = (
            self._config.missing_folder_cache_seconds
            if isinstance(outcome, NoMatch)
            else self._config.lookup_cache_seconds
        )
        with self._lock:
            self._cache[cache_key] = (outcome, time.monotonic() + ttl)
        return outcome

    def _resolve_uncached(
        self, project_name: str, folder_parts: tuple[str, ...]
    ) -> Destination | NoMatch:
        key = fold(project_name, self._config.case_sensitive_names)
        project = self._project_index().get(key)
        if project is None:
            # The project may have been created since the last listing.
            self.invalidate_projects()
            project = self._project_index().get(key)
        if project is None:
            return NoMatch(f"no Frame.io project named {project_name!r}")

        folder_id = project.root_folder_id
        walked: list[str] = []
        for part in folder_parts:
            try:
                children = self._client.list_folder_children(
                    self.account_id(), folder_id, asset_type="folder"
                )
            except FrameioError as exc:
                raise ResolveError(
                    f"could not list children of folder {folder_id}: {exc}"
                ) from exc
            wanted = fold(part, self._config.case_sensitive_names)
            match = next(
                (
                    child
                    for child in children
                    if child.type == "folder"
                    and fold(child.name, self._config.case_sensitive_names) == wanted
                ),
                None,
            )
            if match is None:
                trail = "/".join([project_name, *walked, part])
                return NoMatch(f"no Frame.io folder matching {trail!r}")
            folder_id = match.id
            walked.append(match.name)

        return Destination(
            account_id=self.account_id(),
            project_id=project.id,
            project_name=project.name,
            folder_id=folder_id,
            folder_path=tuple(walked),
        )

    # -- subfolders below the matched case folder -------------------------

    def resolve_subfolder(
        self, destination: Destination, parts: tuple[str, ...]
    ) -> Destination:
        """Descend into the folders below an already-matched destination.

        Unlike the project/client/case lookup, these folders are created when
        they are missing: everything under the export folder is this tool's to
        mirror, while the structure above it belongs to the production bot.

        Creation happens under the resolver lock so two upload threads cannot
        create the same folder twice.
        """
        if not parts:
            return destination

        with self._lock:
            folder_id = destination.folder_id
            walked = list(destination.folder_path)
            for part in parts:
                folder_id = self._descend(folder_id, part)
                walked.append(part)
            return replace(
                destination, folder_id=folder_id, folder_path=tuple(walked)
            )

    def _descend(self, parent_id: str, name: str) -> str:
        key = (parent_id, fold(name, self._config.case_sensitive_names))
        cached = self._subfolders.get(key)
        if cached and time.monotonic() < cached[1]:
            return cached[0]

        folder_id = self._find_or_create(parent_id, name)
        self._subfolders[key] = (
            folder_id,
            time.monotonic() + self._config.lookup_cache_seconds,
        )
        return folder_id

    def _find_or_create(self, parent_id: str, name: str) -> str:
        existing = self._child_folder(parent_id, name)
        if existing is not None:
            return existing
        try:
            created = self._client.create_folder(self.account_id(), parent_id, name)
        except FrameioError:
            # Something else may have created it in the meantime; look again
            # before giving up, so a race does not fail the upload.
            existing = self._child_folder(parent_id, name)
            if existing is not None:
                return existing
            raise
        log.info("created Frame.io folder %r under %s", name, parent_id)
        return created.id

    def _child_folder(self, parent_id: str, name: str) -> str | None:
        wanted = fold(name, self._config.case_sensitive_names)
        for child in self._client.list_folder_children(
            self.account_id(), parent_id, asset_type="folder"
        ):
            if child.type == "folder" and fold(
                child.name, self._config.case_sensitive_names
            ) == wanted:
                return child.id
        return None

    def invalidate_projects(self) -> None:
        with self._lock:
            self._projects = {}
            self._projects_expire = 0.0

    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()
            self._subfolders.clear()
            self.invalidate_projects()
