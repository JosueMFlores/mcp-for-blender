"""Bounded, serial operations that can be polled without contacting Blender.

An operation runs once, even when a caller's wait expires. Cancellation can
prevent queued work, but running Blender commands are never forcibly stopped.
Only this manager's jobs are serialized; direct Blender tools still need the
connection's own serialization. Persisted jobs are history, never a work queue.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import os
import re
import tempfile
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from collections import deque
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "interrupted"})
ACTIVE_STATES = frozenset({"queued", "running"})
MAX_WAIT_SECONDS = 30.0
_OMITTED_TEXT = "[text omitted from persisted history]"
_SENSITIVE_KEYS = frozenset({
    "code", "script", "source", "source_code", "python_code", "arguments",
    "params", "prompt", "text_prompt", "api_key", "apikey", "key", "secret",
    "password", "authorization", "token", "access_token", "refresh_token",
    "subscription_key", "headers", "request", "traceback",
})


class OperationError(Exception):
    """A manager error suitable for displaying to a caller."""


class OperationQueueFull(OperationError):
    pass


class OperationConflict(OperationError):
    pass


class OperationNotFound(OperationError):
    pass


class ProgressReporter:
    """Callable progress reporter passed to submitted work.

    ``progress(1, 3, "Importing")`` records progress. ``cancel_requested`` is
    advisory: the callable may stop at a safe boundary, but the manager never
    interrupts a running command or labels it cancelled while it still runs.
    """

    def __init__(self, manager: OperationManager, operation_id: str):
        self._manager = manager
        self.operation_id = operation_id

    def __call__(self, current: float, total: float | None = None,
                 message: str = "") -> None:
        self._manager._progress(self.operation_id, current, total, message)

    update = __call__

    @property
    def cancel_requested(self) -> bool:
        with self._manager._condition:
            entry = self._manager._entries.get(self.operation_id)
            return bool(entry and entry["cancel_requested"])

    def set_checkpoint(self, checkpoint: Any) -> None:
        """Attach recovery information once a worker creates its checkpoint."""
        self._manager._checkpoint(self.operation_id, checkpoint)


class OperationManager:
    """A single worker with bounded admission and bounded retained history.

    ``max_queue`` is the number of waiting slots in addition to one execution
    slot. ``request_id`` requires a caller-supplied fingerprint of *all* inputs;
    duplicates return the existing job and conflicting reuse raises an error.
    Idempotency lasts while the job is retained in history. Neither identifier
    nor fingerprint is saved verbatim.

    Results must be JSON serializable. Oversized results become a bounded JSON
    preview with ``result_truncated=True``. Disk history omits free text and
    sensitive fields; ``result_redacted`` tells restarted callers when this
    occurred. No callable, command arguments, code, or traceback is persisted.
    Queued/running history loaded after restart becomes ``interrupted`` and is
    never submitted again automatically.
    """

    def __init__(self, path: str | Path | None = None, *, max_queue: int = 16,
                 max_history: int = 100, result_limit_bytes: int = 262144):
        if (isinstance(max_queue, bool) or not isinstance(max_queue, int) or max_queue < 0
                or isinstance(max_history, bool) or not isinstance(max_history, int)
                or max_history < max_queue + 1):
            raise ValueError("max_queue must be nonnegative and max_history must cover all active slots")
        if (isinstance(result_limit_bytes, bool) or not isinstance(result_limit_bytes, int)
                or result_limit_bytes < 128):
            raise ValueError("result_limit_bytes must be at least 128")
        self.path = Path(path) if path is not None else None
        self.max_queue = max_queue
        self.max_history = max_history
        self.result_limit_bytes = result_limit_bytes
        self._condition = threading.Condition(threading.RLock())
        self._entries: dict[str, dict] = {}
        self._requests: dict[str, str] = {}
        self._pending: deque[tuple[str, Callable[[ProgressReporter], Any]]] = deque()
        self._worker_active = False
        self._worker_future: Future | None = None
        self._closed = False
        self._last_progress_save = 0.0
        self._load()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="blender-operation")

    def submit(self, kind: str, work: Callable[[ProgressReporter], Any], *,
               request_id: str | None = None, fingerprint: Any = None,
               checkpoint: Any = None) -> dict:
        if not isinstance(kind, str) or not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,80}", kind):
            raise ValueError("kind must be a short operation name")
        if not callable(work):
            raise TypeError("work must be callable")
        request_hash = None
        fingerprint_hash = None
        if request_id is not None:
            if not isinstance(request_id, str) or not request_id or len(request_id) > 4096:
                raise ValueError("request_id must be a nonempty string of at most 4096 characters")
            if fingerprint is None:
                raise ValueError("request_id requires a fingerprint of the operation inputs")
            request_hash = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
            encoded = json.dumps([kind, fingerprint], sort_keys=True, ensure_ascii=True,
                                 allow_nan=False, separators=(",", ":"))
            fingerprint_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        checkpoint_copy = self._bounded_json(checkpoint)[0] if checkpoint is not None else None
        with self._condition:
            if self._closed:
                raise OperationError("Operation manager is closed")
            if request_hash in self._requests:
                existing = self._entries[self._requests[request_hash]]
                if existing["_fingerprint_hash"] != fingerprint_hash:
                    raise OperationConflict("request_id already belongs to an operation with different inputs")
                return self._snapshot(existing)
            active = sum(e["state"] in ACTIVE_STATES for e in self._entries.values())
            if active >= self.max_queue + 1:
                raise OperationQueueFull("Operation queue is full; wait for an existing operation to finish")
            now = time.time()
            ident = str(uuid.uuid4())
            entry = {
                "id": ident, "kind": kind, "state": "queued", "created_at": now,
                "started_at": None, "finished_at": None, "updated_at": now,
                "progress": {"current": 0.0, "total": None, "message": "Queued"},
                "cancel_requested": False, "result": None, "error": None,
                "result_truncated": False, "result_redacted": False,
                "recovery_checkpoint": checkpoint_copy,
                "_request_hash": request_hash, "_fingerprint_hash": fingerprint_hash,
            }
            self._entries[ident] = entry
            if request_hash is not None:
                self._requests[request_hash] = ident
            self._prune_locked()
            self._persist_locked()
            # Feed one pump to the executor, rather than a Future for every
            # operation. Otherwise repeatedly cancelling queued Futures leaves
            # unbounded tombstones in ThreadPoolExecutor's internal work queue.
            self._pending.append((ident, work))
            try:
                if not self._worker_active:
                    self._worker_active = True
                    self._worker_future = self._executor.submit(self._drain)
            except Exception:
                self._worker_active = False
                self._pending.pop()
                self._entries.pop(ident)
                if request_hash is not None:
                    self._requests.pop(request_hash, None)
                self._persist_locked()
                raise
            return self._snapshot(entry)

    def get(self, operation_id: str) -> dict:
        with self._condition:
            return self._snapshot(self._entry_locked(operation_id))

    def list(self) -> list[dict]:
        with self._condition:
            return [self._snapshot(entry) for entry in reversed(tuple(self._entries.values()))]

    def has_active(self) -> bool:
        with self._condition:
            return any(entry["state"] in ACTIVE_STATES for entry in self._entries.values())

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    def wait(self, operation_id: str, seconds: float = 0.0) -> dict:
        """Wait at most 30 seconds. Expiration leaves the worker untouched."""
        seconds = self._number(seconds, "seconds")
        deadline = time.monotonic() + min(seconds, MAX_WAIT_SECONDS)
        with self._condition:
            while True:
                entry = self._entry_locked(operation_id)
                if entry["state"] in TERMINAL_STATES:
                    return self._snapshot(entry)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self._snapshot(entry)
                self._condition.wait(remaining)

    def cancel(self, operation_id: str) -> dict:
        with self._condition:
            entry = self._entry_locked(operation_id)
            if entry["state"] in TERMINAL_STATES:
                return self._snapshot(entry)
            entry["cancel_requested"] = True
            if entry["state"] == "queued":
                entry["state"] = "cancelled"
                entry["finished_at"] = time.time()
                entry["progress"]["message"] = "Cancelled before execution"
                self._pending = deque((ident, work) for ident, work in self._pending
                                      if ident != operation_id)
            else:
                entry["progress"]["message"] = "Cancellation requested; running work cannot be forcibly stopped"
            entry["updated_at"] = time.time()
            self._persist_locked()
            self._condition.notify_all()
            return self._snapshot(entry)

    def shutdown(self, wait: bool = False) -> None:
        """Reject new work, cancel queued jobs, and let running work finish.

        ``wait=False`` returns immediately; it does not terminate Blender or a
        running worker. Call before interpreter exit to prevent queued changes.
        """
        with self._condition:
            if not self._closed:
                self._closed = True
                for ident, entry in tuple(self._entries.items()):
                    if entry["state"] == "queued":
                        self.cancel(ident)
        self._executor.shutdown(wait=wait, cancel_futures=True)

    close = shutdown

    def _entry_locked(self, operation_id: str) -> dict:
        try:
            return self._entries[operation_id]
        except (KeyError, TypeError) as exc:
            raise OperationNotFound("Operation not found or no longer retained in history") from exc

    @staticmethod
    def _snapshot(entry: dict) -> dict:
        return copy.deepcopy({key: value for key, value in entry.items() if not key.startswith("_")})

    @staticmethod
    def _number(value: float, name: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be a finite nonnegative number")
        converted = float(value)
        if not math.isfinite(converted) or converted < 0:
            raise ValueError(f"{name} must be a finite nonnegative number")
        return converted

    def _bounded_json(self, value: Any) -> tuple[Any, bool]:
        encoded = json.dumps(value, ensure_ascii=True, allow_nan=False,
                             separators=(",", ":")).encode("utf-8")
        if len(encoded) > self.result_limit_bytes:
            # A preview is explicitly incomplete JSON, never presented as the
            # operation's complete data. Its text is omitted from disk history.
            preview = encoded[:self.result_limit_bytes // 2].decode("utf-8", errors="ignore")
            return {"preview": preview, "original_bytes": len(encoded), "omitted": True}, True
        return json.loads(encoded), False

    def _checkpoint(self, ident: str, checkpoint: Any) -> None:
        copied, _ = self._bounded_json(checkpoint)
        with self._condition:
            entry = self._entry_locked(ident)
            entry["recovery_checkpoint"] = copied
            entry["updated_at"] = time.time()
            self._persist_locked()

    def _progress(self, ident: str, current: float, total: float | None, message: str) -> None:
        current = self._number(current, "current")
        total = self._number(total, "total") if total is not None else None
        if total is not None and current > total:
            raise ValueError("current cannot exceed total")
        if not isinstance(message, str):
            raise ValueError("message must be text")
        with self._condition:
            entry = self._entry_locked(ident)
            if entry["state"] != "running":
                return
            entry["progress"] = {"current": current, "total": total, "message": message[:2048]}
            entry["updated_at"] = time.time()
            now = time.monotonic()
            if now - self._last_progress_save >= 0.5:
                self._persist_locked()
                self._last_progress_save = now
            self._condition.notify_all()

    def _drain(self) -> None:
        while True:
            with self._condition:
                if not self._pending:
                    self._worker_active = False
                    return
                ident, work = self._pending.popleft()
            self._run(ident, work)

    def _run(self, ident: str, work: Callable[[ProgressReporter], Any]) -> None:
        with self._condition:
            entry = self._entries.get(ident)
            if entry is None or entry["state"] != "queued":
                return
            # A queued cancellation can win after the pump claims this work
            # but before acquiring this lock. The state check above ensures
            # not a single line of its callable is invoked in that race.
            entry["state"] = "running"
            entry["started_at"] = entry["updated_at"] = time.time()
            entry["progress"]["message"] = "Running"
            self._persist_locked()
            self._condition.notify_all()
        try:
            result, truncated = self._bounded_json(work(ProgressReporter(self, ident)))
            state, error = "succeeded", None
        except BaseException as exc:
            # Capture even an explicit SystemExit from user work so history
            # cannot stay 'running' forever. Do not store a traceback.
            result, truncated, state = None, False, "failed"
            try:
                message = str(exc)[:4096]
            except Exception:
                message = "Operation failed (error message unavailable)"
            error = {"type": type(exc).__name__[:80], "message": message}
            recovery = getattr(exc, "checkpoint_id", None) or getattr(exc, "recovery_checkpoint", None)
            if recovery is not None:
                try:
                    error["recovery_checkpoint"] = self._bounded_json(recovery)[0]
                except (TypeError, ValueError):
                    pass
        with self._condition:
            entry = self._entries[ident]
            entry.update(state=state, result=result, error=error, result_truncated=truncated,
                         finished_at=time.time(), updated_at=time.time())
            if error:
                if error.get("recovery_checkpoint") is not None:
                    entry["recovery_checkpoint"] = copy.deepcopy(error["recovery_checkpoint"])
                elif entry["recovery_checkpoint"] is not None:
                    error["recovery_checkpoint"] = copy.deepcopy(entry["recovery_checkpoint"])
            entry["progress"]["message"] = "Completed" if state == "succeeded" else "Failed"
            self._persist_locked()
            self._condition.notify_all()

    def _prune_locked(self) -> None:
        while len(self._entries) > self.max_history:
            ident = next((ident for ident, entry in self._entries.items()
                          if entry["state"] in TERMINAL_STATES), None)
            if ident is None:
                break
            removed = self._entries.pop(ident)
            if removed.get("_request_hash"):
                self._requests.pop(removed["_request_hash"], None)

    @staticmethod
    def _disk_checkpoint(value: Any) -> Any:
        """Keep only an opaque checkpoint UUID, sufficient for restoration."""
        if isinstance(value, str) and re.fullmatch(r"[a-fA-F0-9]{32}|[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}", value):
            return value
        if isinstance(value, dict):
            for key in ("checkpoint_id", "id"):
                ident = OperationManager._disk_checkpoint(value.get(key))
                if isinstance(ident, str):
                    return {key: ident}
        return None

    @staticmethod
    def _disk_safe(value: Any) -> tuple[Any, bool]:
        """Omit arbitrary strings and sensitive keys, including nested ones."""
        if isinstance(value, str):
            return _OMITTED_TEXT, True
        if isinstance(value, list):
            values = [OperationManager._disk_safe(item) for item in value]
            return [item for item, _ in values], any(changed for _, changed in values)
        if isinstance(value, dict):
            result, changed = {}, False
            for key, item in value.items():
                normalized = str(key).lower().replace("-", "_")
                if (normalized in _SENSITIVE_KEYS or "secret" in normalized or "password" in normalized
                        or "token" in normalized or "api_key" in normalized):
                    changed = True
                    continue
                result[key], redacted = OperationManager._disk_safe(item)
                changed = changed or redacted
            return result, changed
        return value, False

    def _persist_locked(self) -> None:
        if self.path is None:
            return
        records = []
        for entry in self._entries.values():
            record = copy.deepcopy(entry)
            record["progress"]["message"] = ""
            record["result"], redacted = self._disk_safe(record["result"])
            record["result_redacted"] = record["result_redacted"] or redacted
            record["recovery_checkpoint"] = self._disk_checkpoint(record["recovery_checkpoint"])
            if record["error"] is not None:
                record["error"] = {"type": record["error"]["type"],
                                   "message": "Operation failed; detailed error was omitted from disk history"}
                if record["recovery_checkpoint"] is not None:
                    record["error"]["recovery_checkpoint"] = record["recovery_checkpoint"]
            records.append(record)
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp",
                                                     dir=self.path.parent)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump({"version": 1, "operations": records}, stream, ensure_ascii=True, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except (OSError, ValueError) as exc:
            logger.warning("Could not persist operation history: %s", type(exc).__name__)
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass

    def _load(self) -> None:
        if self.path is None:
            return
        try:
            # Reject unbounded/corrupt history before reading it into memory.
            if self.path.stat().st_size > self.max_history * (self.result_limit_bytes + 16384):
                raise ValueError("History file exceeds configured size limit")
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("version") != 1 or not isinstance(data.get("operations"), list):
                raise ValueError("Unsupported history format")
            entries = data["operations"][-self.max_history:]
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                try:
                    uuid.UUID(entry["id"])
                    if entry["state"] not in TERMINAL_STATES | ACTIVE_STATES:
                        continue
                    if not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,80}", entry["kind"]):
                        continue
                    required = {"created_at", "started_at", "finished_at", "updated_at", "progress",
                                "cancel_requested", "result", "error", "result_truncated",
                                "result_redacted", "recovery_checkpoint", "_request_hash", "_fingerprint_hash"}
                    if not required.issubset(entry) or not isinstance(entry["progress"], dict):
                        continue
                except (KeyError, TypeError, ValueError, AttributeError):
                    continue
                if entry["state"] in ACTIVE_STATES:
                    entry["state"] = "interrupted"
                    entry["finished_at"] = entry["updated_at"] = time.time()
                    entry["error"] = {"type": "InterruptedOperation",
                                      "message": "Server restarted; Blender may have completed or partially changed the scene. Inspect it before starting new work."}
                    if entry["recovery_checkpoint"] is not None:
                        entry["error"]["recovery_checkpoint"] = entry["recovery_checkpoint"]
                self._entries[entry["id"]] = entry
                if isinstance(entry.get("_request_hash"), str):
                    self._requests[entry["_request_hash"]] = entry["id"]
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            logger.warning("Could not read operation history: %s", type(exc).__name__)
        if self._entries:
            self._persist_locked()
