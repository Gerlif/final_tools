"""Tests for the HTTP plumbing: pagination, retries, token refresh."""

import pytest

from frameio_export_watcher.frameio import FrameioAPIError, FrameioClient, RateLimiter


class StubResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.text = str(self._payload)
        self.content = b"{}" if payload is not None else b""

    def json(self):
        return self._payload


class StubSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.requests.append((method, url, params, json, dict(headers or {})))
        return self._responses.pop(0)


class StubTokens:
    def __init__(self):
        self.tokens = ["old", "new"]
        self.invalidated = 0
        self.extra_headers = {}

    def token(self):
        return self.tokens[0]

    def invalidate(self):
        self.invalidated += 1
        self.tokens.pop(0)


def make_client(session, tokens=None, **kwargs):
    return FrameioClient(
        tokens or StubTokens(),
        base_url="https://api.frame.io/v4",
        session=session,
        rate_limiter=RateLimiter(0),
        **kwargs,
    )


def test_pagination_follows_the_next_link():
    session = StubSession(
        [
            StubResponse(payload={"data": [{"id": "1"}], "links": {"next": "/v4/accounts?after=x"}}),
            StubResponse(payload={"data": [{"id": "2"}], "links": {"next": None}}),
        ]
    )
    client = make_client(session)
    assert [a["id"] for a in client.list_accounts()] == ["1", "2"]
    assert session.requests[1][1] == "https://api.frame.io/v4/accounts?after=x"
    # The cursor carries the query; the original params must not override it.
    assert session.requests[1][2] == {}


def test_a_401_refreshes_the_token_once():
    tokens = StubTokens()
    session = StubSession([StubResponse(401), StubResponse(payload={"data": []})])
    make_client(session, tokens).list_accounts()
    assert tokens.invalidated == 1
    assert session.requests[0][4]["Authorization"] == "Bearer old"
    assert session.requests[1][4]["Authorization"] == "Bearer new"


def test_server_errors_are_retried(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)
    session = StubSession([StubResponse(503), StubResponse(payload={"data": []})])
    make_client(session).list_accounts()
    assert len(session.requests) == 2


def test_rate_limit_responses_honour_retry_after(monkeypatch):
    slept = []
    monkeypatch.setattr("time.sleep", lambda seconds: slept.append(seconds))
    session = StubSession(
        [StubResponse(429, headers={"Retry-After": "7"}), StubResponse(payload={"data": []})]
    )
    make_client(session).list_accounts()
    assert slept == [7.0]


def test_client_errors_are_raised_immediately():
    session = StubSession([StubResponse(404, payload={"error": "nope"})])
    with pytest.raises(FrameioAPIError) as excinfo:
        make_client(session).list_accounts()
    assert excinfo.value.status_code == 404
    assert len(session.requests) == 1


def test_local_upload_requires_upload_urls():
    session = StubSession([StubResponse(payload={"data": {"id": "f1", "upload_urls": []}})])
    with pytest.raises(Exception, match="no upload URLs"):
        make_client(session).create_local_upload("acc", "folder", "spot.mp4", 10)


def test_local_upload_parses_the_chunk_urls():
    session = StubSession(
        [
            StubResponse(
                payload={
                    "data": {
                        "id": "f1",
                        "media_type": "video/quicktime",
                        "upload_urls": [
                            {"url": "https://s3/1", "size": 5},
                            {"url": "https://s3/2", "size": 3},
                        ],
                    }
                }
            )
        ]
    )
    target = make_client(session).create_local_upload("acc", "folder", "spot.mov", 8)
    assert target.media_type == "video/quicktime"
    assert target.upload_urls == (("https://s3/1", 5), ("https://s3/2", 3))
    method, url, _, body, _ = session.requests[0]
    assert method == "POST"
    assert url.endswith("/accounts/acc/folders/folder/files/local_upload")
    assert body == {"data": {"name": "spot.mov", "file_size": 8}}


def test_rate_limiter_spaces_out_calls():
    limiter = RateLimiter(0.05)
    import time

    started = time.monotonic()
    for _ in range(3):
        limiter.wait()
    assert time.monotonic() - started >= 0.09
