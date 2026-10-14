"""Local Hunyuan HTTP work runs in the MCP worker process, away from Blender's UI."""

import base64
from pathlib import Path
from urllib.parse import urlparse

import httpx

MAX_MODEL_BYTES = 128 * 1024 * 1024
MAX_IMAGE_BYTES = 16 * 1024 * 1024


def _read_response(response, limit):
    response.raise_for_status()
    chunks, size = [], 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > limit:
            raise ValueError(f"Response exceeds the {limit // (1024 * 1024)} MiB limit")
        chunks.append(chunk)
    return b"".join(chunks)


def generate(config, prompt=None, image=None, progress=None, client=None):
    """Return a completed GLB. No bpy, no automatic HTTP retries, no scene edits."""
    if bool(prompt) == bool(image):
        raise ValueError("Give exactly one of prompt or image")
    base_url = config.get("base_url", "").rstrip("/")
    if urlparse(base_url).scheme not in ("http", "https"):
        raise ValueError("Local Hunyuan URL must use http or https")
    own_client = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(630.0, connect=10.0), follow_redirects=True)
    report = progress or (lambda *args: None)
    try:
        data = dict(config.get("settings") or {})
        if prompt:
            if not isinstance(prompt, str) or len(prompt) > 1024:
                raise ValueError("Prompt must be a string of at most 1024 characters")
            data["text"] = prompt
        else:
            report(0, 3, "Reading reference image")
            if urlparse(image).scheme in ("http", "https"):
                with client.stream("GET", image) as response:
                    raw = _read_response(response, MAX_IMAGE_BYTES)
            else:
                path = Path(image)
                if not path.is_absolute() or not path.is_file():
                    raise ValueError("Reference image must be an absolute file path or an http(s) URL")
                if path.stat().st_size > MAX_IMAGE_BYTES:
                    raise ValueError("Reference image exceeds 16 MiB")
                raw = path.read_bytes()
            data["image"] = base64.b64encode(raw).decode("ascii")
        report(1, 3, "Generating model; waiting for the local Hunyuan server")
        with client.stream("POST", base_url + "/generate", json=data) as response:
            raw = _read_response(response, MAX_MODEL_BYTES)
        if len(raw) < 12 or raw[:4] != b"glTF":
            raise ValueError("Local Hunyuan did not return a GLB model")
        report(2, 3, "Model downloaded; preparing import into Blender")
        return raw
    finally:
        if own_client:
            client.close()
