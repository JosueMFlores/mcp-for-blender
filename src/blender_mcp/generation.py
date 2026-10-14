"""One generate/poll/import flow across Tripo, Hunyuan3D and Hyper3D Rodin.

Each provider used to be three or four tools with different id shapes. Here a
generation is a single call: submit, poll until done or out of time, import.
If time runs out the caller gets a job handle ("<provider>:<kind>:<id>") and
passes it back to resume, so no client timeout ever strands a paid generation.

The addon still does the provider work (and the Premium routing), through the
commands it has always had, so this needs no addon update.
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
import weakref
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from .distribution import uvx_command

logger = logging.getLogger("BlenderMCPServer")

PROVIDERS = ("tripo", "hunyuan3d", "hyper3d")

# Auto-pick order. Premium generators come first because the user is paying
# for them and they run the stronger models; among those Tripo is Premium-only.
PREMIUM_ORDER = ("tripo", "hunyuan3d", "hyper3d")
OWN_KEY_ORDER = ("hunyuan3d", "hyper3d")

POLL_INTERVAL_S = 5.0
# A whole generate_3d call, submit included, stays under this. MCP clients on
# the TypeScript SDK cut tool calls off at 60 s by default, and a reply that
# never arrives takes its job handle with it while the generation is charged.
MAX_CALL_S = 45.0
# Unfinished jobs older than this are dropped from the pending file: the
# Premium sweeper fails jobs after 60 minutes, and provider results expire.
PENDING_MAX_AGE_S = 2 * 3600
PENDING_STATE_CACHE_SIZE = 128
COMPLETED_IMPORT_CACHE_SIZE = 256

TRIPO_UNAVAILABLE = ("Tripo is only available with MCP for Blender Premium. If Premium is on, update the Blender "
                     f"addon: run `{uvx_command('install-addon')}`, then restart Blender.")

Send = Callable[[str, dict], Any]
Progress = Callable[[float, float], Awaitable[None]]


class GenerationError(Exception):
    """A failure to relay to the user as written, without retrying."""


class Unsupported(GenerationError):
    """The addon can't do this at all, so resuming the job won't help."""


@dataclass
class Job:
    provider: str
    kind: str   # tripo: "rid"; hyper3d: "fal" | "main"; hunyuan3d: "job"
    ident: str  # request id, job id, or "<task_uuid>|<subscription_key>"
    name: str

    @property
    def handle(self) -> str:
        return f"{self.provider}:{self.kind}:{self.ident}"

    @classmethod
    def parse(cls, handle: str, name: str) -> "Job":
        provider, kind, ident = (handle.split(":", 2) + ["", ""])[:3]
        if provider not in PROVIDERS or not kind or not ident:
            raise GenerationError(f"Not a generation job handle: {handle!r}")
        return cls(provider, kind, ident, name)

    @property
    def resume_hint(self) -> str:
        return (f'Call generate_3d(job="{self.handle}", name="{self.name}") to keep waiting; it imports the '
                "model when it's ready. Don't start a new generation.")


def request_key(provider: str, prompt: str | None, image: str | None,
                quality: str | None, bbox_condition: list | None) -> str:
    """Identifies a request, so a retry of one still running can be recognised."""
    raw = json.dumps([provider, prompt, image, quality, bbox_condition])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class _PendingState:
    def __init__(self):
        self.lock = threading.RLock()
        self.fallback: list[dict] = []
        self.unpersisted = False


_pending_registry_lock = threading.Lock()
_pending_states = weakref.WeakValueDictionary()
_pending_recent: OrderedDict[str, _PendingState] = OrderedDict()


def _pending_state(path: Path | None) -> _PendingState:
    if path is None:
        return _PendingState()
    key = os.path.normcase(str(path.expanduser().resolve()))
    with _pending_registry_lock:
        state = _pending_states.get(key)
        if state is None:
            state = _PendingState()
            _pending_states[key] = state
        # Retain recent fallback data across short-lived Pending instances.
        # Weak references keep a live instance's lock stable even if its
        # entry ages out of the bounded recent-state cache.
        _pending_recent[key] = state
        _pending_recent.move_to_end(key)
        while len(_pending_recent) > PENDING_STATE_CACHE_SIZE:
            _pending_recent.popitem(last=False)
        return state


class Pending:
    """Generations submitted but not yet imported or failed, kept on disk.

    A client timeout or a restart loses the job handle from the conversation,
    and the usual next step is the same request again, which pays twice. With
    the job written down here, that retry resumes it instead. Persistence is
    best-effort; submission errors still propagate to the caller.
    """

    def __init__(self, path: Path | None, clock: Callable[[], float] = time.time):
        self.path = path.expanduser().resolve() if path is not None else None
        self.clock = clock
        self._state = _pending_state(self.path)

    def _load(self) -> list[dict]:
        if not self.path or self._state.unpersisted:
            entries = self._state.fallback
        else:
            try:
                entries = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                entries = self._state.fallback
        if not isinstance(entries, list):
            entries = []
        oldest = self.clock() - PENDING_MAX_AGE_S
        return [e for e in entries if isinstance(e, dict)
                and isinstance(e.get("started_at"), (int, float))
                and e["started_at"] >= oldest]

    def _save(self, entries: list[dict]) -> None:
        self._state.fallback = entries
        self._state.unpersisted = True
        if not self.path:
            return
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Readers in another process see either complete version, never
            # an empty or half-written JSON document.
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                             dir=self.path.parent, prefix=self.path.name + ".",
                                             suffix=".tmp", delete=False) as stream:
                temporary = stream.name
                json.dump(entries, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
            self._state.unpersisted = False
        except Exception as e:
            logger.debug(f"Pending generations write failed: {e}")
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def _find(self, key: str) -> Job | str | None:
        for entry in self._load():
            if entry.get("key") == key:
                if isinstance(entry.get("result"), str):
                    return entry["result"]
                try:
                    return Job.parse(entry["handle"], entry.get("name") or "Generated")
                except (GenerationError, KeyError):
                    return None
        return None

    def find(self, key: str) -> Job | None:
        with self._state.lock:
            result = self._find(key)
            return result if isinstance(result, Job) else None

    def find_or_submit(self, key: str, factory: Callable[[], Job | str]) -> tuple[Job | str, bool]:
        """Reuse a request or submit it once while holding its file's lock.

        The synchronous factory may perform a paid network request. Async
        callers should call this method via asyncio.to_thread.
        """
        with self._state.lock:
            existing = self._find(key)
            if existing is not None:
                return existing, True
            submitted = factory()
            if isinstance(submitted, Job):
                self.add(key, submitted)
            elif isinstance(submitted, str):
                # LOCAL_API can finish and import during submission itself.
                # Remember its result too, so a concurrent retry cannot pay
                # again just because there is no asynchronous job handle.
                entries = [e for e in self._load() if e.get("key") != key]
                entries.append({"key": key, "result": submitted, "started_at": self.clock()})
                self._save(entries)
            else:
                raise TypeError("Generation submission must return a Job or a completed message")
            return submitted, False

    def add(self, key: str, job: Job) -> None:
        with self._state.lock:
            entries = [e for e in self._load() if e.get("handle") != job.handle and e.get("key") != key]
            entries.append({"key": key, "handle": job.handle, "name": job.name, "started_at": self.clock()})
            self._save(entries)

    def remove(self, job: Job) -> None:
        with self._state.lock:
            entries = self._load()
            kept = [e for e in entries if e.get("handle") != job.handle]
            if len(kept) != len(entries):
                self._save(kept)


def _relay(result: Any) -> None:
    """Raise with the provider's own words if the reply is a failure."""
    if not isinstance(result, dict):
        if isinstance(result, str) and result.lower().startswith("error"):
            raise GenerationError(result)
        return
    if result.get("code"):
        raise GenerationError(f"{result.get('message') or result['code']} (code {result['code']}; "
                              "tell the user as written and don't retry automatically)")
    if result.get("error"):
        raise GenerationError(str(result["error"]))


def default_name(prompt: str | None) -> str:
    words = re.findall(r"[A-Za-z0-9]+", prompt or "")[:3]
    return "".join(w.capitalize() for w in words) or "Generated"


def choose_provider(requested: str, premium: list[str], own_key_enabled: dict[str, bool]) -> tuple[str, bool]:
    """(provider, is_premium) for a request. `requested` is a provider or "auto"."""
    requested = (requested or "auto").lower()
    if requested != "auto":
        if requested not in PROVIDERS:
            raise GenerationError(f"Unknown provider {requested!r}. Use one of: auto, {', '.join(PROVIDERS)}")
        if premium:
            if requested not in premium:
                raise GenerationError(f"{requested} is not switched on in MCP for Blender Premium. "
                                      f"On: {', '.join(premium)}. Tick it in the Blender sidebar.")
            return requested, True
        if requested == "tripo":
            # Also what an addon from before Premium looks like, so say how to update.
            raise GenerationError(TRIPO_UNAVAILABLE)
        if not own_key_enabled.get(requested):
            raise GenerationError(f"{requested} is not enabled. Turn it on and add an API key in the "
                                  "MCP for Blender sidebar in Blender (press N in the 3D Viewport).")
        return requested, False
    for name in PREMIUM_ORDER:
        if name in premium:
            return name, True
    for name in OWN_KEY_ORDER:
        if own_key_enabled.get(name):
            return name, False
    raise GenerationError(
        "No 3D generator is enabled. In Blender's MCP for Blender sidebar, turn on Hunyuan3D or Hyper3D "
        "Rodin with an API key, or use MCP for Blender Premium (no keys needed): "
        "https://mcp-for-blender.com/premium"
    )


def _rodin_images(image: str) -> list:
    if urlparse(image).scheme in ("http", "https"):
        return [image]
    if not os.path.exists(image):
        raise GenerationError(f"Image not found: {image}. Give an absolute file path or an http(s) URL.")
    with open(image, "rb") as f:
        return [(Path(image).suffix, base64.b64encode(f.read()).decode("ascii"))]


def process_bbox(bbox: list | None) -> list[int] | None:
    if bbox is None:
        return None
    if len(bbox) != 3 or any(v <= 0 for v in bbox):
        raise GenerationError("bbox_condition must be three positive numbers [length, width, height]")
    if all(isinstance(v, int) for v in bbox):
        return list(bbox)
    return [int(float(v) / max(bbox) * 100) for v in bbox]


def submit(send: Send, provider: str, name: str, prompt: str | None, image: str | None,
           quality: str | None, bbox_condition: list | None, supports_quality: bool) -> Job | str:
    """Start a generation. Returns a Job, or a finished message for providers
    that generate synchronously (Hunyuan3D LOCAL_API)."""
    if provider == "tripo":
        params = {"text_prompt": prompt, "image": image}
        if quality:
            params["quality"] = quality
        result = send("create_tripo_job", params)
        _relay(result)
        if not result.get("request_id"):
            raise GenerationError(f"Tripo returned no request id: {result}")
        return Job("tripo", "rid", result["request_id"], name)

    if provider == "hunyuan3d":
        params = {"text_prompt": prompt, "image": image}
        if quality and supports_quality:
            params["quality"] = quality
        result = send("create_hunyuan_job", params)
        _relay(result)
        response = result.get("Response", {}) if isinstance(result, dict) else {}
        if response.get("Error"):
            raise GenerationError(str(response["Error"].get("Message") or response["Error"]))
        if "JobId" in response:
            return Job("hunyuan3d", "job", f"job_{response['JobId']}", name)
        if isinstance(result, dict) and result.get("status") == "DONE":
            return "Generated and imported by the local Hunyuan3D server. Find it with get_scene_info."
        raise GenerationError(f"Hunyuan3D returned no job: {result}")

    images = _rodin_images(image) if image else None
    result = send("create_rodin_job", {
        "text_prompt": prompt if not image else None,
        "images": images,
        "bbox_condition": process_bbox(bbox_condition),
    })
    _relay(result)
    if result.get("submit_time") and result.get("uuid"):
        return Job("hyper3d", "main", f"{result['uuid']}|{result['jobs']['subscription_key']}", name)
    if result.get("request_id"):
        return Job("hyper3d", "fal", result["request_id"], name)
    raise GenerationError(f"Hyper3D returned no job: {result}")


def poll(send: Send, job: Job) -> tuple[str, Any]:
    """("running" | "done" | "failed", detail) for one status check."""
    if job.provider == "tripo" or (job.provider == "hyper3d" and job.kind == "fal"):
        command = "poll_tripo_job_status" if job.provider == "tripo" else "poll_rodin_job_status"
        result = send(command, {"request_id": job.ident})
        status = str(result.get("status", "")).upper() if isinstance(result, dict) else ""
        # A failed job also carries "error"; read the status first so it counts
        # as the provider's verdict, not as a lost connection.
        if status not in ("COMPLETED", "IN_QUEUE", "IN_PROGRESS", ""):
            return "failed", result.get("error") or status
        _relay(result)
        if status == "COMPLETED":
            return "done", None
        return "running", status or "IN_QUEUE"

    if job.provider == "hyper3d":
        result = send("poll_rodin_job_status", {"subscription_key": job.ident.split("|", 1)[1]})
        _relay(result)
        statuses = result.get("status_list") or []
        if statuses and all(s == "Done" for s in statuses):
            return "done", None
        if any(s in ("Failed", "Canceled") for s in statuses):
            return "failed", ", ".join(statuses)
        return "running", ", ".join(statuses) or "Waiting"

    result = send("poll_hunyuan_job_status", {"job_id": job.ident})
    _relay(result)
    response = result.get("Response", {}) if isinstance(result, dict) else {}
    status = response.get("Status", "")
    if status == "DONE":
        files = response.get("ResultFile3Ds") or []
        glb = next((f.get("Url") for f in files if str(f.get("Type", "")).upper() == "GLB"), None)
        url = glb or next((f.get("Url") for f in files if f.get("Url")), None)
        if not url:
            return "failed", "finished without a model file"
        return "done", url
    if status == "FAIL" or response.get("Error"):
        return "failed", response.get("ErrorMessage") or response.get("Error") or "generation failed"
    return "running", status or "WAIT"


def import_result(send: Send, job: Job, detail: Any) -> Any:
    if job.provider == "tripo":
        result = send("import_generated_asset_tripo", {"request_id": job.ident, "name": job.name})
    elif job.provider == "hyper3d" and job.kind == "fal":
        result = send("import_generated_asset", {"request_id": job.ident, "name": job.name})
    elif job.provider == "hyper3d":
        result = send("import_generated_asset", {"task_uuid": job.ident.split("|", 1)[0], "name": job.name})
    else:
        result = send("import_generated_asset_hunyuan", {"name": job.name, "zip_file_url": detail})
    _relay(result)
    if isinstance(result, dict) and result.get("succeed") is False:
        raise GenerationError(result.get("error") or result.get("message") or "Import failed")
    return result


class _ImportGuard:
    def __init__(self):
        self.lock = threading.Lock()
        self.completed = None


@dataclass
class _CompletedImport:
    name: str
    result: Any


_import_registry_lock = threading.Lock()
_import_guards = weakref.WeakValueDictionary()
_completed_imports: OrderedDict[str, _CompletedImport] = OrderedDict()


def _completed_import(job: Job) -> _CompletedImport | None:
    with _import_registry_lock:
        result = _completed_imports.get(job.handle)
        if result is not None:
            _completed_imports.move_to_end(job.handle)
        return result


def _import_once(send: Send, job: Job, detail: Any) -> _CompletedImport:
    """Serialize only importing one handle; polling remains independent."""
    with _import_registry_lock:
        guard = _import_guards.get(job.handle)
        if guard is None:
            guard = _ImportGuard()
            _import_guards[job.handle] = guard
    with guard.lock:
        completed = guard.completed or _completed_import(job)
        if completed is not None:
            return completed
        completed = _CompletedImport(job.name, import_result(send, job, detail))
        guard.completed = completed
        with _import_registry_lock:
            _completed_imports[job.handle] = completed
            _completed_imports.move_to_end(job.handle)
            while len(_completed_imports) > COMPLETED_IMPORT_CACHE_SIZE:
                _completed_imports.popitem(last=False)
        return completed


async def wait_and_import(send: Send, job: Job, wait_seconds: float,
                          progress: Progress | None = None,
                          sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                          clock: Callable[[], float] = time.monotonic,
                          pending: Pending | None = None) -> tuple[bool, str]:
    """Poll until done or `wait_seconds` pass. (imported, message).

    Once a job is submitted, any error short of the provider saying it failed
    carries the job handle, since the generation may still finish and is paid.
    """
    start = clock()
    while True:
        completed = _completed_import(job)
        if completed is not None:
            if pending:
                await asyncio.to_thread(pending.remove, job)
            return True, completed.name
        try:
            state, detail = await asyncio.to_thread(poll, send, job)
            if state == "done":
                completed = await asyncio.to_thread(_import_once, send, job, detail)
        except Unsupported:
            raise
        except Exception as e:
            # Another waiter may have imported the same handle while this
            # caller was receiving a stale or failed polling response.
            completed = _completed_import(job)
            if completed is not None:
                if pending:
                    await asyncio.to_thread(pending.remove, job)
                return True, completed.name
            raise GenerationError(f"{e}. The generation may still be running or finished. {job.resume_hint}") from e
        if state == "done":
            if pending:
                await asyncio.to_thread(pending.remove, job)
            return True, completed.name
        completed = _completed_import(job)
        if completed is not None:
            if pending:
                await asyncio.to_thread(pending.remove, job)
            return True, completed.name
        if state == "failed":
            if pending:
                await asyncio.to_thread(pending.remove, job)
            raise GenerationError(f"Generation failed: {str(detail).rstrip('.')}. This attempt was not imported.")
        elapsed = clock() - start
        if elapsed + POLL_INTERVAL_S > wait_seconds:
            return False, str(detail)
        if progress:
            try:
                await progress(elapsed, wait_seconds)
            except Exception:
                pass
        await sleep(POLL_INTERVAL_S)
