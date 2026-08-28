"""An in-memory stand-in for the Frame.io V4 API."""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from frameio_export_watcher.frameio import Asset, Project, UploadTarget

_ids = itertools.count(1)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{next(_ids)}"


@dataclass
class FakeFrameio:
    """Implements the subset of FrameioClient that the tool calls."""

    accounts: list[dict] = field(default_factory=lambda: [{"id": "acc-1", "display_name": "Final"}])
    workspaces: list[dict] = field(default_factory=list)
    projects: list[Project] = field(default_factory=list)
    children: dict[str, list[Asset]] = field(default_factory=dict)
    uploaded: dict[str, bytes] = field(default_factory=dict)
    calls: list[tuple] = field(default_factory=list)
    chunk_size: int = 8
    fail_status_for: set = field(default_factory=set)

    # -- helpers used by tests -------------------------------------------

    def add_project(self, name: str) -> Project:
        project = Project(
            id=_new_id("proj"),
            name=name,
            root_folder_id=_new_id("folder"),
            workspace_id="ws-1",
        )
        self.projects.append(project)
        self.children.setdefault(project.root_folder_id, [])
        return project

    def add_folder(self, parent_id: str, name: str) -> Asset:
        folder = Asset(id=_new_id("folder"), name=name, type="folder", parent_id=parent_id)
        self.children.setdefault(parent_id, []).append(folder)
        self.children.setdefault(folder.id, [])
        return folder

    def add_file(self, parent_id: str, name: str, asset_type: str = "file") -> Asset:
        asset = Asset(id=_new_id("file"), name=name, type=asset_type, parent_id=parent_id)
        self.children.setdefault(parent_id, []).append(asset)
        return asset

    # -- API surface ------------------------------------------------------

    def list_accounts(self) -> list[dict]:
        return list(self.accounts)

    def list_workspaces(self, account_id: str) -> list[dict]:
        return list(self.workspaces)

    def list_projects(self, account_id: str) -> list[Project]:
        self.calls.append(("list_projects", account_id))
        return list(self.projects)

    def list_folder_children(self, account_id, folder_id, *, asset_type=None):
        self.calls.append(("children", folder_id, asset_type))
        items = self.children.get(folder_id, [])
        if asset_type:
            items = [item for item in items if item.type == asset_type]
        return list(items)

    def create_folder(self, account_id, parent_id, name) -> Asset:
        self.calls.append(("create_folder", parent_id, name))
        return self.add_folder(parent_id, name)

    def create_local_upload(self, account_id, folder_id, name, file_size) -> UploadTarget:
        file_id = _new_id("file")
        self.calls.append(("create_local_upload", folder_id, name, file_size))
        sizes = []
        remaining = file_size
        while remaining > 0:
            take = min(self.chunk_size, remaining)
            sizes.append(take)
            remaining -= take
        urls = tuple((f"https://s3.test/{file_id}/part{i}", size) for i, size in enumerate(sizes))
        self.children.setdefault(folder_id, []).append(
            Asset(id=file_id, name=name, type="file", parent_id=folder_id, file_size=file_size)
        )
        return UploadTarget(file_id=file_id, media_type="video/mp4", upload_urls=urls)

    def get_upload_status(self, account_id, file_id) -> dict:
        if file_id in self.fail_status_for:
            return {"upload_failed": True, "upload_complete": False}
        return {"upload_complete": True, "upload_failed": False}

    def create_version_stack(self, account_id, folder_id, file_ids) -> Asset:
        self.calls.append(("version_stack", folder_id, tuple(file_ids)))
        # Frame.io names a stack after its head version.
        head = next(
            (
                child.name
                for child in self.children.get(folder_id, [])
                if child.id == file_ids[-1]
            ),
            "stack",
        )
        self.children[folder_id] = [
            child
            for child in self.children.get(folder_id, [])
            if child.id not in set(file_ids)
        ]
        stack = Asset(id=_new_id("stack"), name=head, type="version_stack", parent_id=folder_id)
        self.children.setdefault(folder_id, []).append(stack)
        return stack

    def move_file(self, account_id, file_id, parent_id) -> Asset:
        self.calls.append(("move", file_id, parent_id))
        moved = None
        for folder_id, items in self.children.items():
            for item in items:
                if item.id == file_id:
                    moved = item
                    break
            if moved is not None:
                # A moved file leaves its old folder listing, as on Frame.io.
                self.children[folder_id] = [i for i in items if i.id != file_id]
                break
        moved = moved or Asset(id=file_id, name="moved", type="file")
        relocated = Asset(
            id=moved.id, name=moved.name, type="file", parent_id=parent_id
        )
        self.children.setdefault(parent_id, []).append(relocated)
        return relocated

    def delete_file(self, account_id, file_id) -> None:
        self.calls.append(("delete", file_id))


class FakeSession:
    """Captures the presigned PUT requests the uploader makes."""

    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code
        self.puts: list[tuple[str, bytes, dict]] = []

    def put(self, url, data=None, headers=None, timeout=None):
        body = data.read() if hasattr(data, "read") else data
        self.puts.append((url, body, dict(headers or {})))
        return _FakeResponse(self.status_code)


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.text = "" if status_code < 300 else "error"
