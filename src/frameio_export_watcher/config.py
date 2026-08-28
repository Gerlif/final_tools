"""Configuration loading: YAML file plus environment overrides.

Secrets are never read from the YAML file. They come from environment
variables, each of which also accepts a ``_FILE`` variant pointing at a file
(Docker/Synology secret style).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .paths import Template, parse_template

# Bookkeeping the NAS and the desktop operating systems scatter through every
# share. None of it is ever a deliverable, and uploading it once was enough to
# put an "@eaDir" folder on Frame.io, so this list applies whatever
# watch.ignore_patterns is set to.
SYSTEM_IGNORE_PATTERNS = (
    ".*",                    # .DS_Store, ._resource forks, .AppleDouble, ...
    "@eaDir",                # Synology thumbnails and index data
    "*@SynoEAStream",        # Synology extended-attribute sidecars
    "*@SynoResource",
    "#recycle",              # Synology recycle bin
    "#snapshot",
    "Thumbs.db",
    "desktop.ini",
    "Network Trash Folder",
    "Temporary Items",
)

# Work in progress, as opposed to system noise. Replaced wholesale when
# watch.ignore_patterns is given.
DEFAULT_IGNORE_PATTERNS = (
    "~$*",
    "*.tmp",
    "*.temp",
    "*.part",
    "*.partial",
    "*.crdownload",
    "*.download",
    "*.filepart",
)

DEFAULT_IMS_TOKEN_URL = "https://ims-na1.adobelogin.com/ims/token/v3"
DEFAULT_IMS_SCOPE = "openid,AdobeID,frame.s2s.all"
DEFAULT_API_BASE_URL = "https://api.frame.io/v4"


class ConfigError(ValueError):
    """Raised when the configuration is missing or contradictory."""


@dataclass(frozen=True)
class StabilityConfig:
    """How long a file must sit still before we believe it is fully written."""

    min_age_seconds: float = 60.0
    checks: int = 2
    interval_seconds: float = 15.0
    use_open_handle_check: bool = False
    upload_as_soon_as_closed: bool = False


@dataclass(frozen=True)
class WatchConfig:
    root: Path
    export_template: Template
    poll_interval_seconds: float = 60.0
    recursive: bool = False
    ignore_patterns: tuple[str, ...] = DEFAULT_IGNORE_PATTERNS
    min_file_size_bytes: int = 1
    stability: StabilityConfig = field(default_factory=StabilityConfig)


@dataclass(frozen=True)
class FrameioConfig:
    project_template: Template
    folder_template: Template | None
    account_id: str | None = None
    workspace_name: str | None = None
    case_sensitive_names: bool = False
    version_stack_on_duplicate: bool = True
    stack_version_suffixes: bool = True
    create_subfolders: bool = True
    lookup_cache_seconds: float = 300.0
    missing_folder_cache_seconds: float = 900.0
    api_base_url: str = DEFAULT_API_BASE_URL


@dataclass(frozen=True)
class UploadConfig:
    max_concurrent_files: int = 2
    max_attempts: int = 5
    retry_backoff_seconds: float = 60.0
    retry_backoff_max_seconds: float = 3600.0
    chunk_attempts: int = 4
    request_timeout_seconds: float = 60.0
    upload_timeout_seconds: float = 3600.0
    status_poll_seconds: float = 5.0
    status_timeout_seconds: float = 900.0
    min_api_interval_seconds: float = 0.25


@dataclass(frozen=True)
class AuthConfig:
    mode: str = "ims"
    client_id: str | None = None
    client_secret: str | None = None
    scope: str = DEFAULT_IMS_SCOPE
    token_url: str = DEFAULT_IMS_TOKEN_URL
    legacy_token: str | None = None


@dataclass(frozen=True)
class AppConfig:
    watch: WatchConfig
    frameio: FrameioConfig
    upload: UploadConfig
    auth: AuthConfig
    state_db: Path = Path("/state/state.db")
    heartbeat_file: Path | None = Path("/state/heartbeat")
    log_level: str = "INFO"
    log_format: str = "text"
    dry_run: bool = False


def _secret(name: str) -> str | None:
    """Read a secret from ``NAME`` or from the file named by ``NAME_FILE``."""
    file_path = os.environ.get(f"{name}_FILE")
    if file_path:
        return Path(file_path).read_text(encoding="utf-8").strip() or None
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else None


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _section(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key) or {}
    if not isinstance(value, dict):
        raise ConfigError(f"config section {key!r} must be a mapping")
    return value


def load_config(path: Path | None = None) -> AppConfig:
    """Load configuration from YAML, then apply environment overrides."""
    config_path = path or Path(os.environ.get("CONFIG_PATH", "/config/config.yaml"))
    if config_path.exists():
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ConfigError(f"{config_path} must contain a YAML mapping")
    else:
        raw = {}
        if path is not None:
            raise ConfigError(f"config file not found: {config_path}")

    watch_raw = _section(raw, "watch")
    frameio_raw = _section(raw, "frameio")
    upload_raw = _section(raw, "upload")
    auth_raw = _section(raw, "auth")

    case_sensitive = _as_bool(frameio_raw.get("case_sensitive_names"), False)

    root = os.environ.get("WATCH_ROOT") or watch_raw.get("root")
    if not root:
        raise ConfigError("watch.root is required (or set WATCH_ROOT)")

    export_template = watch_raw.get("export_template")
    if not export_template:
        raise ConfigError("watch.export_template is required")

    project_template = frameio_raw.get("project_template")
    if not project_template:
        raise ConfigError("frameio.project_template is required")

    stability_raw = _section(watch_raw, "stability")
    stability = StabilityConfig(
        min_age_seconds=float(stability_raw.get("min_age_seconds", 60)),
        checks=max(1, int(stability_raw.get("checks", 2))),
        interval_seconds=float(stability_raw.get("interval_seconds", 15)),
        use_open_handle_check=_as_bool(stability_raw.get("use_open_handle_check"), False),
        upload_as_soon_as_closed=_as_bool(
            stability_raw.get("upload_as_soon_as_closed"), False
        ),
    )

    ignore = watch_raw.get("ignore_patterns")
    watch = WatchConfig(
        root=Path(root),
        export_template=parse_template(export_template, case_sensitive=case_sensitive),
        poll_interval_seconds=float(watch_raw.get("poll_interval_seconds", 60)),
        recursive=_as_bool(watch_raw.get("recursive"), False),
        ignore_patterns=tuple(ignore) if ignore else DEFAULT_IGNORE_PATTERNS,
        min_file_size_bytes=int(watch_raw.get("min_file_size_bytes", 1)),
        stability=stability,
    )

    folder_template_raw = frameio_raw.get("folder_template")
    frameio = FrameioConfig(
        project_template=parse_template(project_template, case_sensitive=case_sensitive),
        folder_template=(
            parse_template(folder_template_raw, case_sensitive=case_sensitive)
            if folder_template_raw
            else None
        ),
        account_id=os.environ.get("FRAMEIO_ACCOUNT_ID") or frameio_raw.get("account_id"),
        workspace_name=frameio_raw.get("workspace_name"),
        case_sensitive_names=case_sensitive,
        version_stack_on_duplicate=_as_bool(
            frameio_raw.get("version_stack_on_duplicate"), True
        ),
        stack_version_suffixes=_as_bool(
            frameio_raw.get("stack_version_suffixes"), True
        ),
        create_subfolders=_as_bool(frameio_raw.get("create_subfolders"), True),
        lookup_cache_seconds=float(frameio_raw.get("lookup_cache_seconds", 300)),
        missing_folder_cache_seconds=float(
            frameio_raw.get("missing_folder_cache_seconds", 900)
        ),
        api_base_url=(
            os.environ.get("FRAMEIO_API_BASE_URL")
            or frameio_raw.get("api_base_url")
            or DEFAULT_API_BASE_URL
        ).rstrip("/"),
    )

    upload = UploadConfig(
        max_concurrent_files=max(1, int(upload_raw.get("max_concurrent_files", 2))),
        max_attempts=max(1, int(upload_raw.get("max_attempts", 5))),
        retry_backoff_seconds=float(upload_raw.get("retry_backoff_seconds", 60)),
        retry_backoff_max_seconds=float(upload_raw.get("retry_backoff_max_seconds", 3600)),
        chunk_attempts=max(1, int(upload_raw.get("chunk_attempts", 4))),
        request_timeout_seconds=float(upload_raw.get("request_timeout_seconds", 60)),
        upload_timeout_seconds=float(upload_raw.get("upload_timeout_seconds", 3600)),
        status_poll_seconds=float(upload_raw.get("status_poll_seconds", 5)),
        status_timeout_seconds=float(upload_raw.get("status_timeout_seconds", 900)),
        min_api_interval_seconds=float(upload_raw.get("min_api_interval_seconds", 0.25)),
    )

    mode = (os.environ.get("FRAMEIO_AUTH_MODE") or auth_raw.get("mode") or "ims").lower()
    if mode not in {"ims", "legacy"}:
        raise ConfigError("auth.mode must be either 'ims' or 'legacy'")

    scope = auth_raw.get("scope") or DEFAULT_IMS_SCOPE
    auth = AuthConfig(
        mode=mode,
        client_id=_secret("FRAMEIO_CLIENT_ID"),
        client_secret=_secret("FRAMEIO_CLIENT_SECRET"),
        scope=",".join(scope) if isinstance(scope, (list, tuple)) else str(scope),
        token_url=auth_raw.get("token_url") or DEFAULT_IMS_TOKEN_URL,
        legacy_token=_secret("FRAMEIO_LEGACY_TOKEN"),
    )
    if mode == "ims" and not (auth.client_id and auth.client_secret):
        raise ConfigError(
            "auth.mode 'ims' requires FRAMEIO_CLIENT_ID and FRAMEIO_CLIENT_SECRET "
            "(or the matching *_FILE variables)"
        )
    if mode == "legacy" and not auth.legacy_token:
        raise ConfigError("auth.mode 'legacy' requires FRAMEIO_LEGACY_TOKEN")

    runtime_raw = _section(raw, "runtime")
    heartbeat = runtime_raw.get("heartbeat_file", "/state/heartbeat")
    return AppConfig(
        watch=watch,
        frameio=frameio,
        upload=upload,
        auth=auth,
        state_db=Path(
            os.environ.get("STATE_DB") or runtime_raw.get("state_db") or "/state/state.db"
        ),
        heartbeat_file=Path(heartbeat) if heartbeat else None,
        log_level=(os.environ.get("LOG_LEVEL") or runtime_raw.get("log_level") or "INFO").upper(),
        log_format=(os.environ.get("LOG_FORMAT") or runtime_raw.get("log_format") or "text").lower(),
        dry_run=_as_bool(os.environ.get("DRY_RUN"), _as_bool(runtime_raw.get("dry_run"), False)),
    )
