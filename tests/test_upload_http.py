"""The chunk PUT against a real socket, so the streaming body is exercised."""

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import requests

from frameio_export_watcher.config import UploadConfig
from frameio_export_watcher.frameio import UploadTarget
from frameio_export_watcher.resolver import Destination
from frameio_export_watcher.uploader import Uploader

RECEIVED: list[tuple[str, bytes, dict]] = []


class Handler(BaseHTTPRequestHandler):
    def do_PUT(self):  # noqa: N802 - BaseHTTPRequestHandler naming
        length = int(self.headers["Content-Length"])
        RECEIVED.append((self.path, self.rfile.read(length), dict(self.headers)))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):  # silence the test output
        pass


@pytest.fixture
def server():
    RECEIVED.clear()
    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


class OneShotClient:
    """A Frame.io client stub that hands out URLs on the local test server."""

    def __init__(self, base: str, chunks: list[int]):
        self.base = base
        self.chunks = chunks
        self.deleted: list[str] = []

    def list_folder_children(self, *args, **kwargs):
        return []

    def create_local_upload(self, account_id, folder_id, name, file_size):
        urls = tuple(
            (f"{self.base}/part{index}", size) for index, size in enumerate(self.chunks)
        )
        return UploadTarget(file_id="file-1", media_type="video/mp4", upload_urls=urls)

    def get_upload_status(self, account_id, file_id):
        return {"upload_complete": True, "upload_failed": False}

    def delete_file(self, account_id, file_id):
        self.deleted.append(file_id)


DESTINATION = Destination(
    account_id="acc-1",
    project_id="p",
    project_name="2026",
    folder_id="folder-1",
    folder_path=("Beierholm", "Kundecase #0711"),
)


def test_chunks_arrive_intact_and_in_order(tmp_path, server):
    payload = bytes(range(256)) * 40  # 10 240 bytes
    path = tmp_path / "spot.mp4"
    path.write_bytes(payload)

    client = OneShotClient(server, [4096, 4096, 2048])
    uploader = Uploader(client, UploadConfig(status_poll_seconds=0), session=requests.Session())
    uploader.upload(path, "spot.mp4", DESTINATION)

    assert [item[0] for item in RECEIVED] == ["/part0", "/part1", "/part2"]
    assert b"".join(item[1] for item in RECEIVED) == payload
    for _, _, headers in RECEIVED:
        assert headers["Content-Type"] == "video/mp4"
        assert headers["x-amz-acl"] == "private"
        # No chunked transfer encoding: S3 presigned PUTs need a real length.
        assert "Transfer-Encoding" not in headers
