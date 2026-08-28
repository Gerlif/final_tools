"""Wiring: build every collaborator from a configuration object."""

from __future__ import annotations

from dataclasses import dataclass

import requests
from requests.adapters import HTTPAdapter

from .auth import TokenProvider, build_token_provider
from .config import AppConfig
from .frameio import FrameioClient, RateLimiter
from .resolver import DestinationResolver
from .scanner import ExportScanner
from .service import WatcherService
from .state import StateStore
from .uploader import Uploader


@dataclass
class Application:
    config: AppConfig
    tokens: TokenProvider
    client: FrameioClient
    state: StateStore
    resolver: DestinationResolver
    uploader: Uploader
    scanner: ExportScanner
    service: WatcherService

    def close(self) -> None:
        self.state.close()


def _session(pool_size: int) -> requests.Session:
    session = requests.Session()
    adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def build_application(config: AppConfig) -> Application:
    pool_size = max(4, config.upload.max_concurrent_files * 2)
    session = _session(pool_size)
    tokens = build_token_provider(config.auth, session=session)
    client = FrameioClient(
        tokens,
        base_url=config.frameio.api_base_url,
        session=session,
        rate_limiter=RateLimiter(config.upload.min_api_interval_seconds),
        timeout=config.upload.request_timeout_seconds,
    )
    state = StateStore(config.state_db)
    resolver = DestinationResolver(client, config.frameio)
    uploader = Uploader(
        client,
        config.upload,
        session=session,
        case_sensitive_names=config.frameio.case_sensitive_names,
        version_stack_on_duplicate=config.frameio.version_stack_on_duplicate,
        stack_version_suffixes=config.frameio.stack_version_suffixes,
    )
    service = WatcherService(config, client, state, uploader, resolver)
    return Application(
        config=config,
        tokens=tokens,
        client=client,
        state=state,
        resolver=resolver,
        uploader=uploader,
        scanner=ExportScanner(config.watch),
        service=service,
    )
