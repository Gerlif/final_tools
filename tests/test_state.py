from frameio_export_watcher.state import (
    STATUS_FAILED,
    STATUS_GIVEN_UP,
    STATUS_UPLOADED,
    StateStore,
)


def test_remembers_an_upload(tmp_path):
    store = StateStore(tmp_path / "state.db")
    store.record("/a/spot.mp4", size=10, mtime_ns=1, status=STATUS_UPLOADED, file_id="f1")
    record = store.get("/a/spot.mp4")
    assert record.status == STATUS_UPLOADED
    assert record.frameio_file_id == "f1"
    assert record.matches(10, 1)
    assert not record.matches(11, 1)


def test_failures_escalate_to_given_up(tmp_path):
    store = StateStore(tmp_path / "state.db")
    for expected in (1, 2):
        attempts = store.bump_failure(
            "/a/spot.mp4", size=10, mtime_ns=1, error="boom", max_attempts=3
        )
        assert attempts == expected
        assert store.get("/a/spot.mp4").status == STATUS_FAILED
    assert store.bump_failure("/a/spot.mp4", size=10, mtime_ns=1, error="boom", max_attempts=3) == 3
    assert store.get("/a/spot.mp4").status == STATUS_GIVEN_UP


def test_a_re_export_resets_the_attempt_count(tmp_path):
    store = StateStore(tmp_path / "state.db")
    store.bump_failure("/a/spot.mp4", size=10, mtime_ns=1, error="boom", max_attempts=3)
    # Same name, new content: this is a fresh file as far as we are concerned.
    store.record("/a/spot.mp4", size=99, mtime_ns=2, status=STATUS_FAILED, error="boom")
    assert store.get("/a/spot.mp4").attempts == 0


def test_counts_and_forget(tmp_path):
    store = StateStore(tmp_path / "state.db")
    store.record("/a/1.mp4", size=1, mtime_ns=1, status=STATUS_UPLOADED)
    store.record("/a/2.mp4", size=1, mtime_ns=1, status=STATUS_UPLOADED)
    assert store.counts() == {STATUS_UPLOADED: 2}
    store.forget("/a/1.mp4")
    assert store.counts() == {STATUS_UPLOADED: 1}


def test_state_survives_a_restart(tmp_path):
    db = tmp_path / "state.db"
    store = StateStore(db)
    store.record("/a/spot.mp4", size=10, mtime_ns=1, status=STATUS_UPLOADED)
    store.close()
    assert StateStore(db).get("/a/spot.mp4").status == STATUS_UPLOADED
