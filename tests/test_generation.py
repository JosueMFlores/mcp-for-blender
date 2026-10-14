"""generate_3d drives every provider through one submit/poll/import flow.

A fake addon replays each provider's real reply shapes, so these pin the
shapes the flow depends on: Premium and fal request ids, Rodin main-site
subscription keys, and Tencent-style Hunyuan responses.
"""

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from blender_mcp import generation
from blender_mcp.generation import GenerationError, Job, choose_provider


@pytest.fixture(autouse=True)
def isolate_imported_handles():
    # Fixtures reuse provider IDs across independent fake Blender sessions.
    with generation._import_registry_lock:
        generation._completed_imports.clear()
    yield
    with generation._import_registry_lock:
        generation._completed_imports.clear()


class FakeAddon:
    def __init__(self, replies):
        # command -> reply, or a list of replies consumed in order
        self.replies = replies
        self.calls = []

    def __call__(self, command, params):
        self.calls.append((command, params))
        reply = self.replies[command]
        if isinstance(reply, list):
            return reply.pop(0) if len(reply) > 1 else reply[0]
        return reply


def _run(send, job, wait=100):
    async def no_sleep(_):
        pass

    ticks = iter(range(0, 10_000, 5))
    return asyncio.run(generation.wait_and_import(send, job, wait, sleep=no_sleep, clock=lambda: next(ticks)))


# -------------------------------------------------------------- provider choice

def test_auto_prefers_premium_generators():
    assert choose_provider("auto", ["hyper3d", "tripo"], {}) == ("tripo", True)
    assert choose_provider("auto", ["hunyuan3d"], {"hyper3d": True}) == ("hunyuan3d", True)


def test_auto_falls_back_to_own_keys():
    assert choose_provider("auto", [], {"hunyuan3d": False, "hyper3d": True}) == ("hyper3d", False)


def test_nothing_enabled_points_at_setup():
    with pytest.raises(GenerationError, match="No 3D generator"):
        choose_provider("auto", [], {"hunyuan3d": False, "hyper3d": False})


def test_tripo_needs_premium():
    with pytest.raises(GenerationError, match="Premium"):
        choose_provider("tripo", [], {"hunyuan3d": True})


def test_explicit_provider_must_be_on_in_premium():
    with pytest.raises(GenerationError, match="not switched on"):
        choose_provider("hyper3d", ["tripo"], {})


# ------------------------------------------------------------------ flows

def test_tripo_submit_poll_import():
    send = FakeAddon({
        "create_tripo_job": {"request_id": "r1", "status": "IN_QUEUE"},
        "poll_tripo_job_status": [{"status": "IN_PROGRESS"}, {"status": "COMPLETED"}],
        "import_generated_asset_tripo": {"succeed": True},
    })
    job = generation.submit(send, "tripo", "Chest", "a chest", None, "high", None, True)
    assert job.handle == "tripo:rid:r1"
    assert send.calls[0] == ("create_tripo_job", {"text_prompt": "a chest", "image": None, "quality": "high"})
    assert _run(send, job) == (True, "Chest")
    assert send.calls[-1] == ("import_generated_asset_tripo", {"request_id": "r1", "name": "Chest"})


def test_premium_error_codes_are_relayed_not_retried():
    send = FakeAddon({"create_tripo_job": {"code": "QUOTA_EXHAUSTED", "message": "You've used all generations."}})
    with pytest.raises(GenerationError, match="used all generations.*don't retry"):
        generation.submit(send, "tripo", "X", "x", None, None, None, True)


def test_hunyuan_polls_tencent_shapes_and_imports_the_glb():
    send = FakeAddon({
        "create_hunyuan_job": {"Response": {"JobId": "42"}},
        "poll_hunyuan_job_status": [
            {"Response": {"Status": "RUN"}},
            {"Response": {"Status": "DONE", "ResultFile3Ds": [
                {"Type": "OBJ", "Url": "https://x/model.zip"}, {"Type": "GLB", "Url": "https://x/model.glb"}]}},
        ],
        "import_generated_asset_hunyuan": {"succeed": True},
    })
    job = generation.submit(send, "hunyuan3d", "Lamp", "a lamp", None, "high", None, supports_quality=False)
    assert job.handle == "hunyuan3d:job:job_42"
    assert "quality" not in send.calls[0][1]  # older addons reject unknown arguments
    assert _run(send, job) == (True, "Lamp")
    assert send.calls[-1] == ("import_generated_asset_hunyuan", {"name": "Lamp", "zip_file_url": "https://x/model.glb"})


def test_hunyuan_failure_says_why():
    send = FakeAddon({"poll_hunyuan_job_status": {"Response": {"Status": "FAIL", "ErrorMessage": "bad prompt"}}})
    with pytest.raises(GenerationError, match="bad prompt"):
        _run(send, Job("hunyuan3d", "job", "job_1", "X"))


def test_rodin_main_site_uses_subscription_key_then_task_uuid():
    send = FakeAddon({
        "create_rodin_job": {"submit_time": True, "uuid": "u1", "jobs": {"subscription_key": "s1"}},
        "poll_rodin_job_status": [{"status_list": ["Done", "Generating"]}, {"status_list": ["Done", "Done"]}],
        "import_generated_asset": {"succeed": True},
    })
    job = generation.submit(send, "hyper3d", "Car", "a car", None, None, [2.0, 1.0, 1.0], True)
    assert job.handle == "hyper3d:main:u1|s1"
    assert send.calls[0][1]["bbox_condition"] == [100, 50, 50]
    assert _run(send, job) == (True, "Car")
    assert ("poll_rodin_job_status", {"subscription_key": "s1"}) in send.calls
    assert send.calls[-1] == ("import_generated_asset", {"task_uuid": "u1", "name": "Car"})


def test_rodin_fal_and_premium_use_request_id():
    send = FakeAddon({
        "create_rodin_job": {"request_id": "q1"},
        "poll_rodin_job_status": {"status": "COMPLETED"},
        "import_generated_asset": {"succeed": True},
    })
    job = generation.submit(send, "hyper3d", "Car", None, "https://img/car.png", None, None, True)
    assert send.calls[0][1]["images"] == ["https://img/car.png"]
    assert send.calls[0][1]["text_prompt"] is None
    assert _run(send, job) == (True, "Car")
    assert send.calls[-1] == ("import_generated_asset", {"request_id": "q1", "name": "Car"})


def test_running_out_of_time_returns_a_resumable_handle():
    send = FakeAddon({"poll_tripo_job_status": {"status": "IN_PROGRESS"}})
    job = Job("tripo", "rid", "r9", "Robot")
    imported, detail = _run(send, job, wait=20)
    assert not imported and detail == "IN_PROGRESS"
    assert Job.parse(job.handle, "Robot") == job


def test_failed_import_is_an_error():
    send = FakeAddon({
        "poll_tripo_job_status": {"status": "COMPLETED"},
        "import_generated_asset_tripo": {"succeed": False, "error": "download failed"},
    })
    with pytest.raises(GenerationError, match="download failed"):
        _run(send, Job("tripo", "rid", "r1", "X"))


def test_bad_handles_are_rejected():
    for handle in ("", "tripo", "meshy:rid:1", "tripo:rid:"):
        with pytest.raises(GenerationError):
            Job.parse(handle, "X")


def test_default_name_comes_from_the_prompt():
    assert generation.default_name("a weathered wooden treasure chest") == "AWeatheredWooden"
    assert generation.default_name(None) == "Generated"


# -------------------------------------------------------------- stranded jobs

def test_errors_after_submit_keep_the_handle():
    class FlakyAddon(FakeAddon):
        def __call__(self, command, params):
            raise ConnectionError("Socket timeout while waiting for response from Blender")

    job = Job("tripo", "rid", "r7", "Chest")
    with pytest.raises(GenerationError) as e:
        _run(FlakyAddon({}), job)
    assert job.handle in str(e.value) and "Socket timeout" in str(e.value)


def test_pending_jobs_survive_on_disk_until_settled(tmp_path):
    path = tmp_path / "pending-generations.json"
    job = Job("tripo", "rid", "r1", "Robot")
    key = generation.request_key("tripo", "a robot", None, "high", None)
    generation.Pending(path).add(key, job)

    # A fresh instance, as after a restart, still finds it; a different request doesn't.
    assert generation.Pending(path).find(key) == job
    assert generation.Pending(path).find(generation.request_key("tripo", "a robot", None, None, None)) is None

    send = FakeAddon({"poll_tripo_job_status": {"status": "COMPLETED"},
                      "import_generated_asset_tripo": {"succeed": True}})
    asyncio.run(generation.wait_and_import(send, job, 100, sleep=lambda _: asyncio.sleep(0),
                                           pending=generation.Pending(path)))
    assert generation.Pending(path).find(key) is None


def test_failed_jobs_leave_pending_but_interrupted_ones_stay(tmp_path):
    path = tmp_path / "pending.json"
    failed, cut = Job("tripo", "rid", "f1", "A"), Job("tripo", "rid", "c1", "B")
    generation.Pending(path).add("kf", failed)
    generation.Pending(path).add("kc", cut)

    send = FakeAddon({"poll_tripo_job_status": {"status": "FAILED", "error": "bad input"}})
    with pytest.raises(GenerationError):
        asyncio.run(generation.wait_and_import(send, failed, 100, pending=generation.Pending(path)))
    send = FakeAddon({"poll_tripo_job_status": {"status": "IN_PROGRESS"}})
    asyncio.run(generation.wait_and_import(send, cut, 1, pending=generation.Pending(path)))

    assert generation.Pending(path).find("kf") is None
    assert generation.Pending(path).find("kc") == cut


def test_old_or_unreadable_pending_entries_are_ignored(tmp_path):
    path = tmp_path / "pending.json"
    now = [1_000_000.0]
    pending = generation.Pending(path, clock=lambda: now[0])
    pending.add("k", Job("tripo", "rid", "r1", "A"))
    now[0] += generation.PENDING_MAX_AGE_S + 1
    assert pending.find("k") is None

    path.write_text("not json")
    assert generation.Pending(path).find("k") is None
    generation.Pending(path).add("k2", Job("tripo", "rid", "r2", "B"))
    assert generation.Pending(path).find("k2") is not None
    assert generation.Pending(None).find("k2") is None


def test_repeating_an_unfinished_request_resumes_instead_of_paying_again(tmp_path, monkeypatch):
    from blender_mcp import server

    send = FakeAddon({"create_tripo_job": {"request_id": "r1"},
                      "poll_tripo_job_status": {"status": "IN_PROGRESS"}})
    path = tmp_path / "pending.json"
    monkeypatch.setattr(server, "get_blender_connection", lambda: None)
    monkeypatch.setattr(server, "_premium_generators", lambda _: ["tripo"])
    monkeypatch.setattr(server, "_addon_protocol", lambda: 13)
    monkeypatch.setattr(server, "_generation_send", send)
    monkeypatch.setattr(server, "_pending_generations", lambda: generation.Pending(path))
    monkeypatch.setattr(generation, "POLL_INTERVAL_S", 0.0)

    class Ctx:
        async def report_progress(self, *_):
            pass

    async def call(**kw):
        return await server.generate_3d(Ctx(), image="/tmp/ref.png", quality="high", wait_seconds=10, **kw)

    monkeypatch.setattr(generation, "submit", lambda send_, *a, **k: Job("tripo", "rid", send_("create_tripo_job", {})["request_id"], "Ref"))
    first = asyncio.run(call())
    second = asyncio.run(call())
    assert 'job="tripo:rid:r1"' in first and 'job="tripo:rid:r1"' in second
    assert "resumed" in second
    assert [c for c, _ in send.calls].count("create_tripo_job") == 1


def test_concurrent_submit_for_same_path_pays_once(tmp_path):
    path = tmp_path / "pending.json"
    # Distinct Pending instances must share the same lock, including paths
    # that spell the same file differently.
    first = generation.Pending(path)
    second = generation.Pending(path.parent / "." / path.name)
    start = threading.Barrier(2)
    calls = []
    job = Job("tripo", "rid", "threaded-submission", "Robot")

    def factory():
        calls.append(True)
        return job

    def submit(pending):
        start.wait(timeout=2)
        return pending.find_or_submit("same-request", factory)

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(submit, first)
        b = pool.submit(submit, second)
        results = [a.result(timeout=3), b.result(timeout=3)]
    assert calls == [True]
    assert sorted(resumed for _, resumed in results) == [False, True]
    assert all(result == job for result, _ in results)
    assert len(json.loads(path.read_text())) == 1


def test_concurrent_distinct_pending_adds_are_not_lost(tmp_path):
    path = tmp_path / "pending.json"

    def add(i):
        generation.Pending(path).add(str(i), Job("tripo", "rid", str(i), str(i)))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(add, range(20)))
    assert len(json.loads(path.read_text())) == 20
    assert all(generation.Pending(path).find(str(i)) is not None for i in range(20))


def test_find_or_submit_reuses_synchronous_completed_result(tmp_path):
    path = tmp_path / "pending.json"
    calls = []

    def factory():
        calls.append(True)
        return "Generated and imported locally"

    assert generation.Pending(path).find_or_submit("local", factory) == ("Generated and imported locally", False)
    assert generation.Pending(path).find_or_submit("local", factory) == ("Generated and imported locally", True)
    assert calls == [True]
    assert generation.Pending(path).find("local") is None, "legacy find returns only asynchronous Jobs"


def test_failed_factory_leaves_request_retryable(tmp_path):
    pending = generation.Pending(tmp_path / "pending.json")

    def fail():
        raise GenerationError("quota exhausted")

    with pytest.raises(GenerationError, match="quota exhausted"):
        pending.find_or_submit("request", fail)
    job = Job("tripo", "rid", "retry", "X")
    assert pending.find_or_submit("request", lambda: job) == (job, False)


def test_atomic_pending_write_keeps_prior_file_and_shared_fallback_on_failure(tmp_path, monkeypatch):
    path = tmp_path / "pending.json"
    first = Job("tripo", "rid", "first", "A")
    second = Job("tripo", "rid", "second", "B")
    generation.Pending(path).add("first", first)
    before = path.read_bytes()

    def broken_dump(entries, stream):
        stream.write("[{\"partial\":")
        raise OSError("disk full")

    monkeypatch.setattr(generation.json, "dump", broken_dump)
    result = generation.Pending(path).find_or_submit("second", lambda: second)
    assert result == (second, False)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))
    assert generation.Pending(path).find("first") == first
    assert generation.Pending(path).find_or_submit("second", lambda: pytest.fail("charged twice")) == (second, True)


def test_atomic_pending_write_replaces_only_complete_json(tmp_path, monkeypatch):
    path = tmp_path / "pending.json"
    generation.Pending(path).add("one", Job("tripo", "rid", "one", "A"))
    replace = generation.os.replace
    seen = []

    def inspect_replace(source, destination):
        from pathlib import Path
        old_entries = json.loads(path.read_text())
        new_entries = json.loads(Path(source).read_text())
        seen.append((len(old_entries), len(new_entries)))
        replace(source, destination)

    monkeypatch.setattr(generation.os, "replace", inspect_replace)
    generation.Pending(path).add("two", Job("tripo", "rid", "two", "B"))
    assert seen == [(1, 2)]
    assert not list(tmp_path.glob("*.tmp"))


def test_pending_state_cache_is_bounded_without_changing_live_locks(tmp_path, monkeypatch):
    monkeypatch.setattr(generation, "PENDING_STATE_CACHE_SIZE", 2)
    first = generation.Pending(tmp_path / "first.json")
    for i in range(5):
        generation.Pending(tmp_path / (str(i) + ".json"))
    assert len(generation._pending_recent) == 2
    assert generation.Pending(first.path)._state is first._state


def test_concurrent_waiters_import_same_job_once_and_reuse_result():
    calls = []
    polling = threading.Barrier(2)
    job = Job("tripo", "rid", "shared-import", "FirstName")

    def send(command, params):
        calls.append((command, params))
        if command == "poll_tripo_job_status":
            polling.wait(timeout=3)
            return {"status": "COMPLETED"}
        return {"succeed": True, "objects": ["FirstName"]}

    def wait(name):
        return asyncio.run(generation.wait_and_import(send, Job.parse(job.handle, name), 10))

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(wait, "FirstName")
        second = pool.submit(wait, "SecondName")
        results = [first.result(timeout=5), second.result(timeout=5)]
    imports = [params for command, params in calls if command == "import_generated_asset_tripo"]
    assert len(imports) == 1
    imported_name = imports[0]["name"]
    assert results == [(True, imported_name), (True, imported_name)]
    # A later retry skips both polling and import, and returns the name that
    # was actually imported, rather than renaming or duplicating the object.
    assert asyncio.run(generation.wait_and_import(lambda *_: pytest.fail("reimported"), job, 1)) == (True, imported_name)
    assert generation._completed_imports[job.handle].result == {"succeed": True, "objects": ["FirstName"]}


@pytest.mark.parametrize("blocking_command", ["poll_tripo_job_status", "import_generated_asset_tripo"])
def test_generation_poll_and_import_do_not_block_event_loop(blocking_command):
    started = threading.Event()
    release = threading.Event()
    job = Job("tripo", "rid", "responsive-poll", "X")

    def send(command, params):
        if command == blocking_command:
            started.set()
            assert release.wait(timeout=3)
        if command == "poll_tripo_job_status":
            return {"status": "COMPLETED"}
        return {"succeed": True}

    async def run():
        waiter = asyncio.create_task(generation.wait_and_import(send, job, 10))
        assert await asyncio.to_thread(started.wait, 2)
        # This callback must execute while the blocking provider poll is
        # still waiting in its worker thread.
        await asyncio.sleep(0)
        assert not waiter.done()
        release.set()
        assert await waiter == (True, "X")

    asyncio.run(run())


def test_completed_import_cache_is_bounded_and_failed_import_is_retryable(monkeypatch):
    monkeypatch.setattr(generation, "COMPLETED_IMPORT_CACHE_SIZE", 2)
    send = FakeAddon({"poll_tripo_job_status": {"status": "COMPLETED"},
                      "import_generated_asset_tripo": {"succeed": True}})
    for i in range(3):
        _run(send, Job("tripo", "rid", "bounded-" + str(i), "X"))
    assert len(generation._completed_imports) == 2
    assert "tripo:rid:bounded-0" not in generation._completed_imports
    failed_job = Job("tripo", "rid", "retry-import", "Retry")
    send.replies["import_generated_asset_tripo"] = {"succeed": False, "error": "download failed"}
    with pytest.raises(GenerationError, match="download failed"):
        _run(send, failed_job)
    assert failed_job.handle not in generation._completed_imports
    send.replies["import_generated_asset_tripo"] = {"succeed": True}
    assert _run(send, failed_job) == (True, "Retry")
