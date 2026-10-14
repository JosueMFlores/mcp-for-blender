"""Operation admission, idempotency and lifecycle without Blender or network."""

import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from blender_mcp.operations import (
    OperationConflict,
    OperationError,
    OperationManager,
    OperationNotFound,
    OperationQueueFull,
)


@pytest.fixture
def manager():
    instance = OperationManager()
    yield instance
    instance.shutdown(wait=True)


def test_progress_results_and_snapshots_are_independent(manager):
    started, release = threading.Event(), threading.Event()

    def work(progress):
        progress(1, 3, "Importing model")
        started.set()
        assert release.wait(3)
        progress(3, 3, "Ready")
        return {"objects": ["Cube"]}

    job = manager.submit("import_asset", work)
    try:
        assert started.wait(3)
        snapshot = manager.get(job["id"])
        assert snapshot["state"] == "running"
        assert snapshot["progress"] == {"current": 1.0, "total": 3.0, "message": "Importing model"}
        snapshot["progress"]["current"] = 999
        assert manager.get(job["id"])["progress"]["current"] == 1
    finally:
        release.set()
    final = manager.wait(job["id"], 3)
    assert final["state"] == "succeeded"
    assert final["created_at"] <= final["started_at"] <= final["finished_at"]
    final["result"]["objects"].append("NotReallyPresent")
    assert manager.get(job["id"])["result"] == {"objects": ["Cube"]}
    assert not manager.has_active()


def test_expiring_waits_and_polls_never_repeat_work(manager):
    started, release = threading.Event(), threading.Event()
    calls = []

    def work(progress):
        calls.append(progress.operation_id)
        started.set()
        assert release.wait(3)
        return 42

    job = manager.submit("code", work)
    try:
        assert started.wait(3)
        before = time.monotonic()
        assert manager.wait(job["id"], 0.02)["state"] == "running"
        assert time.monotonic() - before < 0.5
        for _ in range(10):
            assert manager.get(job["id"])["state"] == "running"
        assert calls == [job["id"]]
    finally:
        release.set()
    assert manager.wait(job["id"], 3)["result"] == 42


def test_duplicate_requests_are_atomic_and_conflicting_inputs_fail(manager):
    started, release = threading.Event(), threading.Event()
    calls = []

    def work(_):
        calls.append(1)
        started.set()
        assert release.wait(3)
        return {"count": 1}

    try:
        with ThreadPoolExecutor(max_workers=8) as clients:
            futures = [clients.submit(manager.submit, "code", work, request_id="retry-me",
                                      fingerprint={"code_hash": "abc", "recovery": True})
                       for _ in range(8)]
            ids = {future.result(timeout=3)["id"] for future in futures}
        assert len(ids) == 1
        assert started.wait(3)
        ident = ids.pop()
        with pytest.raises(OperationConflict, match="different inputs"):
            manager.submit("code", work, request_id="retry-me", fingerprint={"code_hash": "def"})
        with pytest.raises(OperationConflict):
            manager.submit("render", work, request_id="retry-me",
                           fingerprint={"code_hash": "abc", "recovery": True})
        assert len(calls) == 1
    finally:
        release.set()
    assert manager.wait(ident, 3)["state"] == "succeeded"
    assert manager.submit("code", work, request_id="retry-me",
                          fingerprint={"recovery": True, "code_hash": "abc"})["id"] == ident
    assert len(calls) == 1


def test_idempotency_requires_fingerprint(manager):
    with pytest.raises(ValueError, match="fingerprint"):
        manager.submit("code", lambda _: None, request_id="missing-inputs")


def test_queue_admission_is_bounded_and_execution_serial():
    manager = OperationManager(max_queue=2, max_history=5)
    started, release = threading.Event(), threading.Event()
    calls = []

    def first(_):
        calls.append("first")
        started.set()
        assert release.wait(3)
        return 1

    try:
        job1 = manager.submit("code", first)
        assert started.wait(3)
        job2 = manager.submit("render", lambda _: calls.append("second"))
        job3 = manager.submit("recipe", lambda _: calls.append("third"))
        with pytest.raises(OperationQueueFull, match="full"):
            manager.submit("code", lambda _: calls.append("overflow"))
        assert manager.get(job2["id"])["state"] == "queued"
        assert manager.get(job3["id"])["state"] == "queued"
        assert calls == ["first"]
    finally:
        release.set()
        manager.shutdown(wait=True)
    # shutdown cancels queued work; the running command still finishes once.
    assert manager.get(job1["id"])["state"] == "succeeded"
    assert calls == ["first"]


def test_queued_work_runs_in_order_without_overlap(manager):
    started, release = threading.Event(), threading.Event()
    calls = []

    def first(_):
        calls.append("begin1")
        started.set()
        assert release.wait(3)
        calls.append("end1")

    first_job = manager.submit("code", first)
    try:
        assert started.wait(3)
        second_job = manager.submit("code", lambda _: calls.append("second"))
        third_job = manager.submit("code", lambda _: calls.append("third"))
        assert calls == ["begin1"]
    finally:
        release.set()
    assert manager.wait(first_job["id"], 3)["state"] == "succeeded"
    assert manager.wait(second_job["id"], 3)["state"] == "succeeded"
    assert manager.wait(third_job["id"], 3)["state"] == "succeeded"
    assert calls == ["begin1", "end1", "second", "third"]


def test_cancel_queued_job_prevents_callable_and_frees_capacity():
    manager = OperationManager(max_queue=1, max_history=4)
    started, release = threading.Event(), threading.Event()
    calls = []

    def first(_):
        started.set()
        assert release.wait(3)

    try:
        manager.submit("code", first)
        assert started.wait(3)
        cancelled = manager.submit("recipe", lambda _: calls.append("cancelled"))
        result = manager.cancel(cancelled["id"])
        assert result["state"] == "cancelled" and result["cancel_requested"]
        replacement = manager.submit("recipe", lambda _: calls.append("replacement"))
        assert replacement["state"] == "queued"
        release.set()
        assert manager.wait(replacement["id"], 3)["state"] == "succeeded"
        assert calls == ["replacement"]
    finally:
        release.set()
        manager.shutdown(wait=True)


def test_running_cancel_is_advisory_and_does_not_lie_about_completion(manager):
    started, release = threading.Event(), threading.Event()
    observed = []

    def work(progress):
        started.set()
        assert release.wait(3)
        observed.append(progress.cancel_requested)
        return "finished anyway"

    job = manager.submit("render", work)
    try:
        assert started.wait(3)
        result = manager.cancel(job["id"])
        assert result["state"] == "running" and result["cancel_requested"]
        assert manager.has_active()
    finally:
        release.set()
    final = manager.wait(job["id"], 3)
    assert final["state"] == "succeeded" and final["cancel_requested"]
    assert observed == [True]
    assert manager.cancel(job["id"])["state"] == "succeeded"


def test_repeated_queued_cancellation_does_not_accumulate_executor_tombstones():
    manager = OperationManager(max_queue=1, max_history=4)
    started, release = threading.Event(), threading.Event()

    def work(_):
        started.set()
        assert release.wait(3)

    try:
        manager.submit("code", work)
        assert started.wait(3)
        for _ in range(100):
            job = manager.submit("code", lambda _: None)
            assert manager.cancel(job["id"])["state"] == "cancelled"
        assert len(manager._pending) == 0
        assert len(manager.list()) <= manager.max_history
        assert manager._executor._work_queue.qsize() == 0
        replacement = manager.submit("code", lambda _: 5)
        release.set()
        assert manager.wait(replacement["id"], 3)["result"] == 5
    finally:
        release.set()
        manager.shutdown(wait=True)


def test_cancellation_race_before_worker_acquires_manager_lock_skips_work(manager):
    calls = []
    # The executor may have started its pump while this operation still says
    # queued. Cancellation must be honoured before entering user work.
    with manager._condition:
        job = manager.submit("code", lambda _: calls.append("should-not-run"))
        future = manager._worker_future
        deadline = time.monotonic() + 3
        while not future.running() and time.monotonic() < deadline:
            time.sleep(0.001)
        assert future.running()
        assert manager.cancel(job["id"])["cancel_requested"]
    assert manager.wait(job["id"], 3)["state"] == "cancelled"
    assert calls == []


def test_errors_include_recovery_checkpoint_and_do_not_block_next_job(manager):
    def broken(progress):
        progress.set_checkpoint({"id": "checkpoint-before-import"})
        raise RuntimeError("Import failed halfway")

    job = manager.submit("import_asset", broken)
    failed = manager.wait(job["id"], 3)
    assert failed["state"] == "failed"
    assert failed["error"] == {"type": "RuntimeError", "message": "Import failed halfway",
                               "recovery_checkpoint": {"id": "checkpoint-before-import"}}
    assert "traceback" not in failed["error"]
    next_job = manager.submit("code", lambda _: 123)
    assert manager.wait(next_job["id"], 3)["result"] == 123


def test_exception_checkpoint_id_survives_restart_without_error_text(tmp_path):
    path = tmp_path / "history.json"
    manager = OperationManager(path)
    checkpoint_id = uuid.uuid4().hex

    class ImportFailure(RuntimeError):
        pass

    def broken(_):
        failure = ImportFailure("private error text including command arguments")
        failure.checkpoint_id = checkpoint_id
        raise failure

    try:
        job = manager.submit("import_asset", broken)
        snapshot = manager.wait(job["id"], 3)
        assert snapshot["error"]["recovery_checkpoint"] == checkpoint_id
        assert snapshot["recovery_checkpoint"] == checkpoint_id
        assert "private error text" not in path.read_text()
    finally:
        manager.shutdown(wait=True)
    restarted = OperationManager(path)
    try:
        assert restarted.get(job["id"])["recovery_checkpoint"] == checkpoint_id
        assert restarted.get(job["id"])["error"]["recovery_checkpoint"] == checkpoint_id
    finally:
        restarted.shutdown(wait=True)


def test_initial_checkpoint_is_copied(manager):
    checkpoint = {"id": "before"}
    job = manager.submit("code", lambda _: 1, checkpoint=checkpoint)
    checkpoint["id"] = "changed"
    assert manager.wait(job["id"], 3)["recovery_checkpoint"] == {"id": "before"}


def test_results_are_json_serializable_and_bounded():
    manager = OperationManager(max_queue=1, max_history=3, result_limit_bytes=128)
    try:
        huge = manager.submit("render", lambda _: "x" * 10000)
        final = manager.wait(huge["id"], 3)
        assert final["state"] == "succeeded" and final["result_truncated"]
        assert final["result"]["original_bytes"] == 10002
        assert len(final["result"]["preview"]) <= 64
        invalid = manager.submit("code", lambda _: object())
        final = manager.wait(invalid["id"], 3)
        assert final["state"] == "failed" and final["error"]["type"] == "TypeError"
    finally:
        manager.shutdown(wait=True)


def test_history_prunes_terminal_jobs_and_retains_running_jobs():
    manager = OperationManager(max_queue=0, max_history=2)
    try:
        jobs = []
        for index in range(4):
            job = manager.submit("code", lambda _, value=index: value)
            jobs.append(job)
            assert manager.wait(job["id"], 3)["state"] == "succeeded"
        assert [entry["id"] for entry in manager.list()] == [jobs[3]["id"], jobs[2]["id"]]
        with pytest.raises(OperationNotFound):
            manager.get(jobs[0]["id"])
    finally:
        manager.shutdown(wait=True)


def test_restart_marks_queued_and_running_interrupted_and_never_reexecutes(tmp_path):
    original_path = tmp_path / "live.json"
    restart_path = tmp_path / "restart.json"
    manager = OperationManager(original_path)
    restarted = None
    started, release = threading.Event(), threading.Event()
    calls = []

    def running(_):
        calls.append("original")
        started.set()
        assert release.wait(3)

    try:
        first = manager.submit("code", running, request_id="once", fingerprint={"code": "secret code"})
        assert started.wait(3)
        second = manager.submit("render", lambda _: calls.append("queued"))
        restart_path.write_bytes(original_path.read_bytes())
        restarted = OperationManager(restart_path)
        assert not restarted.has_active()
        assert restarted.get(first["id"])["state"] == "interrupted"
        assert restarted.get(second["id"])["state"] == "interrupted"
        same = restarted.submit("code", lambda _: calls.append("replayed"),
                                request_id="once", fingerprint={"code": "secret code"})
        assert same["id"] == first["id"] and same["state"] == "interrupted"
        assert calls == ["original"]
    finally:
        release.set()
        manager.shutdown(wait=True)
        if restarted is not None:
            restarted.shutdown(wait=True)


def test_disk_history_omits_inputs_and_free_text_but_retains_safe_structure(tmp_path):
    path = tmp_path / "jobs.json"
    manager = OperationManager(path)
    try:
        def work(progress):
            progress(1, 2, "sensitive progress words")
            return {"count": 3, "code": "print('sensitive code')",
                    "api_key": "secret API key", "message": "secret output text",
                    "nested": [{"password": "secret password", "vertices": 4}]}

        job = manager.submit("code", work, request_id="secret request id",
                             fingerprint={"code": "secret fingerprint code"})
        snapshot = manager.wait(job["id"], 3)
        assert snapshot["result"]["api_key"] == "secret API key"
        saved = path.read_text(encoding="utf-8")
        for sensitive in ("sensitive progress", "sensitive code", "secret API", "secret output",
                          "secret password", "secret request", "secret fingerprint"):
            assert sensitive not in saved
        assert not list(tmp_path.glob("*.tmp"))
    finally:
        manager.shutdown(wait=True)
    restarted = OperationManager(path)
    try:
        final = restarted.get(job["id"])
        assert final["state"] == "succeeded" and final["result_redacted"]
        assert final["result"]["count"] == 3
        assert final["result"]["nested"] == [{"vertices": 4}]
        assert "api_key" not in final["result"] and "code" not in final["result"]
    finally:
        restarted.shutdown(wait=True)


def test_corrupt_or_unreadable_persistence_does_not_prevent_running(tmp_path):
    path = tmp_path / "invalid.json"
    path.write_text("not valid JSON", encoding="utf-8")
    manager = OperationManager(path)
    try:
        assert manager.list() == []
        job = manager.submit("code", lambda _: {"count": 2})
        assert manager.wait(job["id"], 3)["state"] == "succeeded"
        assert json.loads(path.read_text())["version"] == 1
    finally:
        manager.shutdown(wait=True)
    # A directory cannot be replaced by the metadata file, but work must still
    # be recorded in memory and usable; persistence failure only logs its type.
    manager = OperationManager(tmp_path)
    try:
        job = manager.submit("code", lambda _: True)
        assert manager.wait(job["id"], 3)["state"] == "succeeded"
    finally:
        manager.shutdown(wait=True)


def test_shutdown_returns_without_terminating_running_command():
    manager = OperationManager()
    started, release = threading.Event(), threading.Event()

    def work(_):
        started.set()
        assert release.wait(3)
        return True

    try:
        running = manager.submit("render", work)
        assert started.wait(3)
        queued = manager.submit("code", lambda _: None)
        manager.shutdown(wait=False)
        assert manager.get(running["id"])["state"] == "running"
        assert manager.get(queued["id"])["state"] == "cancelled"
        with pytest.raises(OperationError, match="closed"):
            manager.submit("code", lambda _: None)
    finally:
        release.set()
        manager.shutdown(wait=True)
    assert manager.get(running["id"])["result"] is True


@pytest.mark.parametrize("seconds", [-1, float("inf"), float("nan"), True, "1"])
def test_invalid_wait_is_rejected(manager, seconds):
    job = manager.submit("code", lambda _: 1)
    with pytest.raises(ValueError):
        manager.wait(job["id"], seconds)


def test_missing_operations_and_invalid_progress(manager):
    with pytest.raises(OperationNotFound):
        manager.get("not-an-id")
    with pytest.raises(OperationNotFound):
        manager.cancel("not-an-id")
    job = manager.submit("code", lambda progress: progress(3, 2, "Invalid"))
    assert manager.wait(job["id"], 3)["error"]["type"] == "ValueError"


def test_wait_is_capped_without_real_long_sleep(manager, monkeypatch):
    import blender_mcp.operations as operations

    started, release = threading.Event(), threading.Event()

    def work(_):
        started.set()
        assert release.wait(3)

    job = manager.submit("code", work)
    try:
        assert started.wait(3)
        monkeypatch.setattr(operations, "MAX_WAIT_SECONDS", 0.01)
        before = time.monotonic()
        assert manager.wait(job["id"], 10000)["state"] == "running"
        assert time.monotonic() - before < 0.5
    finally:
        release.set()
