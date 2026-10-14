"""Scene recovery uses real temporary files and a small Blender operator stub."""
from __future__ import annotations

import ast
import io
import json
import threading
import traceback
import types
from contextlib import redirect_stdout

import pytest

from conftest import ROOT_ADDON


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    source = ast.parse(ROOT_ADDON.read_text(encoding="utf-8"))
    original = next(node for node in source.body
                    if isinstance(node, ast.ClassDef) and node.name == "BlenderMCPServer")
    methods = [node for node in original.body
               if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and
                   ("checkpoint" in node.name or node.name == "execute_code")) or
               (isinstance(node, ast.Assign) and
                any(isinstance(target, ast.Name) and target.id == "_checkpoint_store"
                    for target in node.targets))]
    cls = ast.ClassDef(name="RecoveryServer", bases=[], keywords=[], body=methods,
                       decorator_list=[])
    tree = ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[]))
    project = tmp_path / "original.blend"
    project.write_bytes(b"ORIGINAL PROJECT CONTENT")
    storage = tmp_path / "recovery"
    storage.mkdir()
    monkeypatch.setattr("tempfile.mkdtemp", lambda **_kwargs: str(storage))
    saves, opens = [], []
    bpy = types.SimpleNamespace(
        data=types.SimpleNamespace(filepath=str(project), value="initial"),
        context=types.SimpleNamespace(objects_in_mode=[], window=object()),
    )

    def save(**kwargs):
        saves.append(kwargs)
        assert threading.current_thread() is threading.main_thread()
        assert kwargs["copy"] is True
        assert kwargs["relative_remap"] is True
        with open(kwargs["filepath"], "w", encoding="utf-8") as stream:
            json.dump({"value": bpy.data.value}, stream)
        return {'FINISHED'}

    def load(**kwargs):
        opens.append(kwargs)
        assert threading.current_thread() is threading.main_thread()
        with open(kwargs["filepath"], encoding="utf-8") as stream:
            bpy.data.value = json.load(stream)["value"]
        bpy.data.filepath = kwargs["filepath"]
        return {'FINISHED'}

    bpy.ops = types.SimpleNamespace(wm=types.SimpleNamespace(save_as_mainfile=save,
                                                           open_mainfile=load))
    namespace = {"bpy": bpy, "io": io, "json": json,
                 "traceback": traceback, "redirect_stdout": redirect_stdout}
    exec(compile(tree, "<recovery-test>", "exec"), namespace)
    server = namespace["RecoveryServer"]()
    return types.SimpleNamespace(server=server, bpy=bpy, saves=saves, opens=opens,
                                 project=project, storage=storage)


def test_create_copy_keeps_original_filepath_and_persists_metadata(recovery):
    state = recovery
    entry = state.server.create_checkpoint("Antes de editar iluminación")
    assert len(entry["checkpoint_id"]) == 32
    assert entry["original_filepath"] == str(state.project)
    assert state.bpy.data.filepath == str(state.project)
    assert state.project.read_bytes() == b"ORIGINAL PROJECT CONTENT"
    assert entry["bytes"] > 0
    index = json.loads((state.storage / "index.json").read_text(encoding="utf-8"))
    assert index == {"version": 1, "checkpoints": [entry]}
    # A reconnect creates another server, but recovery state belongs to the
    # add-on session, rather than an individual scene or socket connection.
    assert type(state.server)().list_checkpoints() == {"checkpoints": [entry], "limit": 10}


def test_unsaved_headless_scene_can_be_checkpointed(recovery):
    recovery.bpy.data.filepath = ""
    recovery.bpy.app = types.SimpleNamespace(background=True)
    result = recovery.server.create_checkpoint()
    assert result["original_filepath"] == ""
    assert recovery.bpy.data.filepath == ""


def test_retention_bounds_files_and_index_to_ten(recovery):
    entries = [recovery.server.create_checkpoint(str(i)) for i in range(12)]
    listed = recovery.server.list_checkpoints()["checkpoints"]
    assert [entry["checkpoint_id"] for entry in listed] == [entry["checkpoint_id"] for entry in entries[2:]]
    assert len(list(recovery.storage.glob("*.blend"))) == 10
    assert not (recovery.storage / (entries[0]["checkpoint_id"] + ".blend")).exists()
    assert json.loads((recovery.storage / "index.json").read_text())["checkpoints"] == listed


def test_restore_oldest_at_limit_preserves_target_and_safety_checkpoint(recovery):
    state = recovery
    entries = [state.server.create_checkpoint(str(i)) for i in range(10)]
    state.bpy.data.value = "changed"
    window = state.bpy.context.window
    result = state.server.restore_checkpoint(entries[0]["checkpoint_id"])
    assert state.bpy.data.value == "initial"
    assert state.opens[0]["load_ui"] is False
    assert state.opens[0]["use_scripts"] is False
    assert state.bpy.context.window is window
    assert state.project.read_bytes() == b"ORIGINAL PROJECT CONTENT"
    assert result["original_filepath"] == str(state.project)
    assert result["restored_filepath"] == state.bpy.data.filepath
    assert "Save As" in result["message"]
    retained = state.server.list_checkpoints()["checkpoints"]
    assert len(retained) == 10
    assert any(entry["checkpoint_id"] == entries[0]["checkpoint_id"] for entry in retained)
    safety_path = state.storage / (result["safety_checkpoint_id"] + ".blend")
    assert json.loads(safety_path.read_text())["value"] == "changed"
    # The safety point is itself restorable after opening the recovery copy.
    state.server.restore_checkpoint(result["safety_checkpoint_id"])
    assert state.bpy.data.value == "changed"


@pytest.mark.parametrize("bad_id", ["../../original.blend", "C:\\original.blend", "../" + "a" * 32,
                                    "a" * 32, None, 42])
def test_restore_accepts_only_known_session_ids(recovery, bad_id):
    with pytest.raises(ValueError):
        recovery.server.restore_checkpoint(bad_id)
    assert recovery.opens == []
    assert recovery.saves == []


def test_missing_checkpoint_is_rejected_before_safety_save(recovery):
    entry = recovery.server.create_checkpoint()
    (recovery.storage / (entry["checkpoint_id"] + ".blend")).unlink()
    with pytest.raises(ValueError, match="not found"):
        recovery.server.restore_checkpoint(entry["checkpoint_id"])
    assert len(recovery.saves) == 1
    assert recovery.opens == []


def test_symbolic_link_checkpoint_is_rejected(recovery, monkeypatch):
    entry = recovery.server.create_checkpoint()
    path = str(recovery.storage / (entry["checkpoint_id"] + ".blend"))
    monkeypatch.setattr("os.path.islink", lambda value: str(value) == path)
    with pytest.raises(ValueError, match="symbolic links"):
        recovery.server.restore_checkpoint(entry["checkpoint_id"])
    assert recovery.opens == []
    assert len(recovery.saves) == 1


def test_create_and_restore_refuse_worker_thread(recovery):
    entry = recovery.server.create_checkpoint()
    errors = []

    def worker():
        for method in (lambda: recovery.server.create_checkpoint(),
                       lambda: recovery.server.restore_checkpoint(entry["checkpoint_id"])):
            try:
                method()
            except RuntimeError as exc:
                errors.append(str(exc))

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=2)
    assert len(errors) == 2
    assert all("main thread" in error for error in errors)
    assert len(recovery.saves) == 1
    assert recovery.opens == []


def test_edit_mesh_changes_are_flushed_before_checkpoint(recovery):
    called = []
    recovery.bpy.context.objects_in_mode = [types.SimpleNamespace(
        mode="EDIT", update_from_editmode=lambda: called.append(True))]
    recovery.server.create_checkpoint()
    assert called == [True]


def test_unsupported_edit_capture_blocks_code(recovery):
    recovery.bpy.context.objects_in_mode = [types.SimpleNamespace(mode="EDIT")]
    with pytest.raises(Exception) as failure:
        recovery.server.execute_code("bpy.data.value = 'changed'", checkpoint=True)
    error = json.loads(str(failure.value))
    assert error["stage"] == "checkpoint"
    assert "Object Mode" in error["message"]
    assert recovery.bpy.data.value == "initial"
    assert recovery.saves == []


def test_execute_code_default_remains_compatible(recovery):
    assert recovery.server.execute_code("print('ok')") == {"executed": True, "result": "ok\n"}
    assert recovery.saves == []


def test_execute_code_returns_checkpoint_on_success_and_partial_failure(recovery):
    success = recovery.server.execute_code("bpy.data.value = 'first'", checkpoint=True)
    assert success["checkpoint_id"] == recovery.server.list_checkpoints()["checkpoints"][0]["checkpoint_id"]
    with pytest.raises(Exception) as failure:
        recovery.server.execute_code("bpy.data.value = 'partial'\nraise ValueError('broken')", checkpoint=True)
    error = json.loads(str(failure.value))
    assert error["message"] == "broken"
    assert error["exception_type"] == "ValueError"
    assert error["checkpoint"] == error["checkpoint_id"]
    assert recovery.bpy.data.value == "partial", "recovery is manual, never automatic rollback"
    recovery.server.restore_checkpoint(error["checkpoint_id"])
    assert recovery.bpy.data.value == "first"


def test_cancelled_save_blocks_execution_and_removes_partial_checkpoint(recovery):
    def cancelled(**kwargs):
        from pathlib import Path
        Path(kwargs["filepath"]).write_bytes(b"partial")
        return {'CANCELLED'}

    recovery.bpy.ops.wm.save_as_mainfile = cancelled
    with pytest.raises(Exception) as failure:
        recovery.server.execute_code("bpy.data.value = 'changed'", checkpoint=True)
    error = json.loads(str(failure.value))
    assert error["stage"] == "checkpoint"
    assert recovery.bpy.data.value == "initial"
    assert not list(recovery.storage.glob("*.blend"))
    assert recovery.server.list_checkpoints()["checkpoints"] == []


def test_restore_failure_reports_safety_checkpoint(recovery):
    checkpoint = recovery.server.create_checkpoint()
    recovery.bpy.data.value = "current"
    recovery.bpy.ops.wm.open_mainfile = lambda **kwargs: {'CANCELLED'}
    with pytest.raises(RuntimeError) as failure:
        recovery.server.restore_checkpoint(checkpoint["checkpoint_id"])
    error = json.loads(str(failure.value))
    safety_path = recovery.storage / (error["safety_checkpoint_id"] + ".blend")
    assert json.loads(safety_path.read_text())["value"] == "current"
    assert error["checkpoint_id"] == checkpoint["checkpoint_id"]
    assert recovery.bpy.data.value == "current"


def test_failed_index_commit_removes_new_copy_and_retains_old_index(recovery, monkeypatch):
    original = recovery.server.create_checkpoint()
    monkeypatch.setattr(recovery.server, "_write_checkpoint_index", lambda entries: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        recovery.server.create_checkpoint()
    assert recovery.server.list_checkpoints()["checkpoints"] == [original]
    assert len(list(recovery.storage.glob("*.blend"))) == 1
