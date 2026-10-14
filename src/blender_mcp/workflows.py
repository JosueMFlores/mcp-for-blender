"""Recoverable recipes and background workflows exposed by the MCP server."""

import asyncio
import base64
import hashlib
import inspect
import json
import threading
from contextvars import ContextVar

from . import blender_scripts, context_log, generation, local_generation, operations, recipes
from .safe_mode import safe_mode_enabled, validate_code

_manager = None
_manager_lock = threading.Lock()
_restore_gate = threading.Lock()
_CURRENT_MANAGER = ContextVar("current_blender_operation_manager", default=None)


def check_session_active():
    owner = _CURRENT_MANAGER.get()
    if owner is not None and owner.closed:
        raise OperationFailure("MCP session closed; stopped before sending another command to Blender")


class OperationFailure(RuntimeError):
    def __init__(self, message, checkpoint_id=None):
        super().__init__(message)
        self.checkpoint_id = checkpoint_id
        self.recovery_checkpoint = checkpoint_id


def manager():
    from . import server
    global _manager
    with _manager_lock:
        if _manager is None or (_manager.closed and not _manager.has_active()):
            endpoint = server.resolve_connection(server.CLI_HOST, server.CLI_PORT)
            key = hashlib.sha256(repr(endpoint).encode()).hexdigest()[:12]
            path = context_log.context_log_path().with_name(f"operations-{key}.json")
            _manager = operations.OperationManager(path=path)
        return _manager


def _send(command, params=None):
    from . import server
    check_session_active()
    return server.get_blender_connection().send_command(command, params)


def _require_recovery():
    from . import server
    server.get_blender_connection()
    if (server._addon_protocol() or 0) < 15:
        raise OperationFailure(server.missing_feature("recovery checkpoints and local background generation"))


def _checkpoint(label):
    _require_recovery()
    result = _send("create_checkpoint", {"label": label})
    _check_reply(result)
    if not isinstance(result, dict) or not result.get("checkpoint_id"):
        raise OperationFailure("Addon did not return a recovery checkpoint")
    return result["checkpoint_id"]


def _check_reply(result):
    if isinstance(result, str):
        lower = result.strip().lower()
        if lower.startswith(("error", "failed", "rejected", "the blender addon is too old")) or \
                " is switched off." in lower:
            raise OperationFailure(result)
    if isinstance(result, dict) and (result.get("error") or result.get("succeed") is False):
        raise OperationFailure(str(result.get("error") or result.get("message") or "Operation failed"),
                               result.get("checkpoint_id"))
    return result


def _execute(code, checkpoint=False):
    if safe_mode_enabled():
        validate_code(code)
    if checkpoint:
        _require_recovery()
    params = {"code": code}
    if checkpoint:
        params["checkpoint"] = True
    try:
        return _check_reply(_send("execute_code", params))
    except Exception as exc:
        # The addon preserves the checkpoint ID alongside the script traceback.
        try:
            detail = json.loads(str(exc).split("Communication error with Blender: ", 1)[-1])
        except (TypeError, ValueError):
            raise
        raise OperationFailure(detail.get("message", str(exc)), detail.get("checkpoint_id")) from exc


def checkpoint_scene(action="create", checkpoint_id=None, label=""):
    with _restore_gate:
        _require_recovery()
        if action == "create":
            return _check_reply(_send("create_checkpoint", {"label": label}))
        if action == "list":
            return _send("list_checkpoints")
        if action != "restore" or not checkpoint_id:
            raise ValueError("Use create, list, or restore with a checkpoint_id")
        if _manager is not None and any(j["state"] in ("queued", "running") for j in _manager.list()):
            raise ValueError("Finish or cancel queued operations before restoring the scene")
        return _check_reply(_send("restore_checkpoint", {"checkpoint_id": checkpoint_id}))


def run_recipe(name=None, object_names=None, options=None, checkpoint=True):
    if name is None:
        return {"recipes": recipes.list_recipes()}
    code = recipes.build_recipe(name, object_names, options)
    result = _execute(code, checkpoint=checkpoint)
    report = blender_scripts.parse_result(result.get("result", ""))
    if result.get("checkpoint_id"):
        report["checkpoint_id"] = result["checkpoint_id"]
    _check_reply(report)
    return report


def local_config():
    from . import server
    if (server._addon_protocol() or 0) < 15:
        return None
    config = _send("get_hunyuan3d_local_config")
    return config if isinstance(config, dict) and config.get("enabled") else None


def _local_generate(config, arguments, progress):
    raw = local_generation.generate(config, prompt=arguments.get("prompt"), image=arguments.get("image"),
                                    progress=progress)
    result = _check_reply(_send("import_local_hunyuan", {
        "glb_base64": base64.b64encode(raw).decode("ascii"),
        "name": arguments.get("name") or generation.default_name(arguments.get("prompt")),
    }))
    progress(3, 3, "Generated model imported")
    return result


def _generate(arguments, progress):
    from . import server
    name = arguments.get("name") or generation.default_name(arguments.get("prompt"))
    if arguments.get("job"):
        job = generation.Job.parse(arguments["job"], name)
    else:
        premium = server._premium_generators(server.get_blender_connection())
        chosen, is_premium = generation.choose_provider(arguments.get("provider", "auto"), premium,
            {} if premium else server._own_key_generators(server.get_blender_connection()))
        config = local_config() if chosen == "hunyuan3d" and not is_premium else None
        if config:
            return _local_generate(config, arguments, progress)
        pending = server._pending_generations()
        key = generation.request_key(chosen, arguments.get("prompt"), arguments.get("image"),
                                     arguments.get("quality"), arguments.get("bbox_condition"))
        def submit_once():
            return generation.submit(server._generation_send, chosen, name, arguments.get("prompt"),
                arguments.get("image"), arguments.get("quality"), arguments.get("bbox_condition"),
                supports_quality=(server._addon_protocol() or 0) >= 11)
        job, _ = pending.find_or_submit(key, submit_once)
        if isinstance(job, str):
            return _check_reply(job)
    progress(0, None, f"Generation submitted: {job.handle}")

    async def report(current, total):
        progress(current, total, f"Waiting for {job.provider}; resume handle {job.handle}")

    imported, detail = asyncio.run(generation.wait_and_import(server._generation_send, job, 900, report,
                                                              pending=server._pending_generations()))
    if not imported:
        raise OperationFailure(f"Generation remains pending after fifteen minutes ({detail}). {job.resume_hint}")
    return {"imported": True, "name": job.name, "provider": job.provider}


def _validate(kind, arguments):
    from . import server
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    functions = {"code": (lambda code: None), "recipe": recipes.build_recipe,
                 "import_asset": server.import_asset, "generate_3d": server.generate_3d}
    if kind == "render":
        allowed = {"filepath", "animation"}
        if set(arguments) - allowed:
            raise ValueError("render accepts only filepath and animation")
        if arguments.get("filepath") is not None and not isinstance(arguments["filepath"], str):
            raise ValueError("filepath must be a string")
        if "animation" in arguments and not isinstance(arguments["animation"], bool):
            raise ValueError("animation must be true or false")
        return
    if kind not in functions:
        raise ValueError("kind must be code, recipe, render, import_asset or generate_3d")
    function = functions[kind]
    signature = inspect.signature(function)
    supplied = dict(arguments)
    if kind in ("import_asset", "generate_3d"):
        if "ctx" in supplied or "user_prompt" in supplied or "wait_seconds" in supplied:
            raise ValueError("Do not pass ctx, user_prompt or wait_seconds inside arguments")
        supplied["ctx"] = None
    try:
        signature.bind(**supplied)
    except TypeError as exc:
        raise ValueError(str(exc)) from exc
    if kind == "code":
        if not isinstance(arguments["code"], str) or not arguments["code"].strip():
            raise ValueError("code must be a nonempty string")
        if safe_mode_enabled():
            validate_code(arguments["code"])
    if kind == "recipe":
        recipes.build_recipe(**arguments)
    if kind == "generate_3d":
        if not arguments.get("job") and bool(arguments.get("prompt")) == bool(arguments.get("image")):
            raise ValueError("Give exactly one of prompt or image, or a job to resume")
        if arguments.get("quality") not in (None, "standard", "high"):
            raise ValueError("quality must be standard or high")
        if arguments.get("provider", "auto") not in ("auto", *generation.PROVIDERS):
            raise ValueError("Unknown generation provider")


def run_operation(kind, arguments, request_id=None, checkpoint=True, user_prompt=""):
    from . import server
    _validate(kind, arguments)
    # Copy inputs so a caller cannot change a queued operation after validation.
    arguments = json.loads(json.dumps(arguments, allow_nan=False))
    fingerprint = json.dumps([kind, arguments, checkpoint], sort_keys=True, separators=(",", ":"))

    def work(progress):
        token = server.COMMAND_TIMEOUT.set(900.0)
        owner_token = _CURRENT_MANAGER.set(jobs)
        recovery = None
        try:
            progress(0, None, "Saving recovery point" if checkpoint else "Starting operation")
            if checkpoint:
                recovery = _checkpoint("Before " + kind)
                progress.set_checkpoint(recovery)
            progress(0, None, "Running " + kind)
            if kind == "code":
                result = _execute(arguments["code"])
            elif kind == "recipe":
                result = run_recipe(**arguments, checkpoint=False)
            elif kind == "render":
                code = blender_scripts.build('''
import bpy
scene = bpy.context.scene
original = scene.render.filepath
try:
    if ARGS.get("filepath"):
        scene.render.filepath = ARGS["filepath"]
    animation = ARGS.get("animation", False)
    bpy.ops.render.render(animation=animation, write_still=bool(ARGS.get("filepath")) and not animation)
    return {"rendered": True, "filepath": scene.render.filepath, "animation": animation}
finally:
    scene.render.filepath = original
''', arguments)
                result = blender_scripts.parse_result(_execute(code).get("result", ""))
            elif kind == "import_asset":
                result = _check_reply(asyncio.run(server.import_asset(None, **arguments, user_prompt=user_prompt)))
            else:
                result = _generate(arguments, progress)
            if recovery:
                result = {"result": result, "checkpoint_id": recovery}
            return result
        except Exception as exc:
            raise OperationFailure(str(exc), getattr(exc, "checkpoint_id", None) or recovery) from exc
        finally:
            _CURRENT_MANAGER.reset(owner_token)
            server.COMMAND_TIMEOUT.reset(token)

    with _restore_gate:
        # Identical submissions resume automatically. An explicit request_id can
        # distinguish intentionally repeated operations (e.g. another variant).
        request_id = request_id or "auto:" + hashlib.sha256(fingerprint.encode()).hexdigest()
        jobs = manager()
        return jobs.submit(kind, work, request_id=request_id, fingerprint=fingerprint)
