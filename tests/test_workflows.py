"""Model-facing recovery, recipes and jobs preserve safety and do not replay work."""

import asyncio
import json
import threading

import pytest

from blender_mcp import blender_scripts, operations, server, workflows
from blender_mcp.addon_manager import AddonHandshake


@pytest.fixture
def jobs(monkeypatch, tmp_path):
    manager = operations.OperationManager(path=tmp_path / "jobs.json")
    monkeypatch.setattr(workflows, "_manager", manager)
    yield manager
    manager.close(wait=True)


def connected(monkeypatch, send, protocol=15):
    class Blender:
        def send_command(self, command, params=None, read_only=False):
            return send(command, params)
    monkeypatch.setattr(server, "get_blender_connection", lambda: Blender())
    monkeypatch.setattr(server, "_addon_handshake", AddonHandshake(True, protocol, [1, 8], [], "4.2", "native"))


def test_checkpointed_code_adds_recovery_id_and_failure_keeps_it(monkeypatch):
    sent = []
    def send(command, params):
        sent.append(params)
        return {"result": "done", "checkpoint_id": "saved"}
    connected(monkeypatch, send)
    reply = asyncio.run(server.execute_blender_code(None, "print('done')", checkpoint=True))
    assert "saved" in reply and sent == [{"code": "print('done')", "checkpoint": True}]
    def broken(*args):
        raise Exception("Communication error with Blender: " + json.dumps({
            "message": "bad", "traceback": "line 2", "checkpoint_id": "recover"}))
    connected(monkeypatch, broken)
    reply = asyncio.run(server.execute_blender_code(None, "x", checkpoint=True))
    assert "recover" in reply and "line 2" in reply and reply.startswith("Error")


def test_old_addon_refuses_requested_checkpoint_before_editing(monkeypatch):
    connected(monkeypatch, lambda *args: pytest.fail("edited without recovery"), protocol=14)
    reply = asyncio.run(server.execute_blender_code(None, "x", checkpoint=True))
    assert "install-addon" in reply and reply.startswith("Error")


def test_listing_recipes_does_not_connect_to_blender(monkeypatch):
    monkeypatch.setattr(server, "get_blender_connection", lambda: pytest.fail("connected"))
    result = asyncio.run(server.run_recipe(None))
    assert {r["name"] for r in result["recipes"]} == {
        "prepare_game_asset", "product_lighting", "create_variants"}


def test_recipe_report_and_checkpoint_id(monkeypatch):
    sent = []
    def send(command, params):
        sent.append((command, params))
        return {"result": blender_scripts.RESULT_MARKER + json.dumps({"created": ["Copy"]}),
                "checkpoint_id": "recovery"}
    connected(monkeypatch, send)
    result = asyncio.run(server.run_recipe(None, "create_variants", ["Cube"]))
    assert result == {"created": ["Copy"], "checkpoint_id": "recovery"}
    assert sent[0][1]["checkpoint"] is True


def test_recipe_error_preserves_checkpoint(monkeypatch):
    connected(monkeypatch, lambda *args: {
        "result": blender_scripts.RESULT_MARKER + '{"error":"UV failed"}', "checkpoint_id": "saved"})
    result = asyncio.run(server.run_recipe(None, "prepare_game_asset", ["Cube"]))
    assert result["error"] == "UV failed" and result["checkpoint_id"] == "saved"


def test_operation_returns_while_blender_busy_status_uses_no_socket(monkeypatch, jobs):
    started, release = threading.Event(), threading.Event()
    calls = []
    def send(command, params):
        calls.append(command)
        if command == "create_checkpoint":
            return {"checkpoint_id": "saved"}
        started.set()
        assert release.wait(3)
        return {"result": "done"}
    connected(monkeypatch, send)
    result = asyncio.run(server.run_operation(None, "code", {"code": "print('done')"}))
    try:
        assert started.wait(2)
        status = asyncio.run(server.operation_status(None, result["id"]))
        assert status["state"] == "running"
        assert status["recovery_checkpoint"] == "saved"
        duplicate = asyncio.run(server.run_operation(None, "code", {"code": "print('done')"}))
        assert duplicate["id"] == result["id"]
        assert calls == ["create_checkpoint", "execute_code"]
        refused = asyncio.run(server.checkpoint_scene(None, "restore", "saved"))
        assert "before restoring" in refused["error"]
    finally:
        release.set()
    done = jobs.wait(result["id"], 2)
    assert done["state"] == "succeeded" and done["result"]["checkpoint_id"] == "saved"


def test_checkpoint_failure_prevents_operation(monkeypatch, jobs):
    calls = []
    def send(command, params):
        calls.append(command)
        raise RuntimeError("disk full")
    connected(monkeypatch, send)
    entry = workflows.run_operation("code", {"code": "danger()"})
    done = jobs.wait(entry["id"], 2)
    assert done["state"] == "failed" and calls == ["create_checkpoint"]


def test_operation_failure_preserves_recovery(monkeypatch, jobs):
    def send(command, params):
        if command == "create_checkpoint":
            return {"checkpoint_id": "recovery"}
        raise RuntimeError("Blender failed halfway")
    connected(monkeypatch, send)
    entry = workflows.run_operation("code", {"code": "work()"})
    done = jobs.wait(entry["id"], 2)
    assert done["state"] == "failed"
    assert done["recovery_checkpoint"] == "recovery"


@pytest.mark.parametrize("kind,args", [
    ("unknown", {}), ("code", {"code": "x", "typo": True}),
    ("recipe", {"name": "create_variants", "object_names": ["Cube"], "options": {"count": -1}}),
    ("generate_3d", {"prompt": "a", "image": "b"}),
    ("render", {"animation": "true"}), ("import_asset", {"ctx": None}),
])
def test_invalid_job_rejected_before_creating_checkpoint(kind, args, monkeypatch, jobs):
    connected(monkeypatch, lambda *args: pytest.fail("contacted Blender"))
    result = asyncio.run(server.run_operation(None, kind, args))
    assert result.get("error") and jobs.list() == []


def test_safe_mode_rejects_code_before_queuing(monkeypatch, jobs):
    monkeypatch.setenv("BLENDER_MCP_SAFE_MODE", "1")
    result = asyncio.run(server.run_operation(None, "code", {"code": "import os"}))
    assert result.get("error") and jobs.list() == []


def test_background_render_restores_filepath(monkeypatch, jobs):
    monkeypatch.setenv("BLENDER_MCP_SAFE_MODE", "1")
    seen = []
    def send(command, params):
        seen.append(params["code"])
        return {"result": blender_scripts.RESULT_MARKER + '{"rendered":true}'}
    connected(monkeypatch, send)
    result = workflows.run_operation("render", {"filepath": "//render.png"}, checkpoint=False)
    done = jobs.wait(result["id"], 2)
    assert done["state"] == "succeeded" and done["result"]["rendered"]
    assert "finally:" in seen[0] and "scene.render.filepath = original" in seen[0]


def test_local_hunyuan_http_never_uses_bpy_thread(monkeypatch, jobs):
    root_thread = threading.get_ident()
    network_threads, commands = [], []
    def generate(*args, **kwargs):
        network_threads.append(threading.get_ident())
        return b"glTFtestdata!"
    monkeypatch.setattr(workflows.local_generation, "generate", generate)
    def send(command, params):
        commands.append(command)
        if command == "get_hunyuan3d_local_config":
            return {"enabled": True, "base_url": "http://localhost", "settings": {}}
        if command == "import_local_hunyuan":
            assert params["name"] == "Chair"
            return {"succeed": True, "name": "Chair"}
        return {}
    connected(monkeypatch, send)
    monkeypatch.setattr(server, "_premium_generators", lambda b: [])
    monkeypatch.setattr(server, "_own_key_generators", lambda b: {"hunyuan3d": True})
    entry = workflows.run_operation("generate_3d", {"prompt": "chair"}, checkpoint=False)
    assert jobs.wait(entry["id"], 2)["state"] == "succeeded"
    assert network_threads and network_threads[0] != root_thread
    assert "create_hunyuan_job" not in commands


def test_job_timeout_override_does_not_leak_to_normal_calls(monkeypatch, jobs):
    timeouts = []
    connected(monkeypatch, lambda *args: (timeouts.append(server.COMMAND_TIMEOUT.get()) or {"result": "ok"}))
    entry = workflows.run_operation("code", {"code": "x"}, checkpoint=False)
    assert jobs.wait(entry["id"], 2)["state"] == "succeeded"
    assert timeouts == [900.0] and server.COMMAND_TIMEOUT.get() == 180.0


@pytest.mark.parametrize("reply", ["Sketchfab is switched off. Enable it.",
                                   "The Blender addon is too old for this import."])
def test_unavailable_import_fails_job(reply, monkeypatch, jobs):
    async def unavailable(*args, **kwargs):
        return reply
    monkeypatch.setattr(server, "import_asset", unavailable)
    entry = workflows.run_operation("import_asset", {"source": "sketchfab", "id": "x"}, checkpoint=False)
    assert jobs.wait(entry["id"], 2)["state"] == "failed"


def test_shutdown_during_local_http_never_imports(monkeypatch, jobs):
    started, release = threading.Event(), threading.Event()
    commands = []
    def generate(*args, **kwargs):
        started.set()
        assert release.wait(3)
        return b"glTFtestdata!"
    monkeypatch.setattr(workflows.local_generation, "generate", generate)
    def send(command, params):
        commands.append(command)
        return {"enabled": True, "base_url": "http://localhost", "settings": {}}
    connected(monkeypatch, send)
    monkeypatch.setattr(server, "_premium_generators", lambda b: [])
    monkeypatch.setattr(server, "_own_key_generators", lambda b: {"hunyuan3d": True})
    entry = workflows.run_operation("generate_3d", {"prompt": "chair"}, checkpoint=False)
    try:
        assert started.wait(2)
        jobs.close()
    finally:
        release.set()
    done = jobs.wait(entry["id"], 2)
    assert done["state"] == "failed" and "import_local_hunyuan" not in commands
