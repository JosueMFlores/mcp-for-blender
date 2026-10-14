"""
Where this fork's server is installed and updated from.

The `mcp-for-blender` package on PyPI is published by upstream
(github.com/ahujasid/blender-mcp), so a plain `uvx mcp-for-blender` would run
upstream's server and install upstream's addon. Client entries, `update` and
the hints we print instead run uvx with `--from` pointed at this repo.

BLENDER_MCP_SOURCE overrides that: "pypi" goes back to the PyPI package
(upstream), and any other value (another git URL, a local checkout) is passed
to `--from` as is.

Client entries pin a git source to a commit (pin_source): uvx fetches an
unpinned one on every launch, so the server wouldn't start while GitHub is
unreachable, but runs a pinned one from its cache. One-off commands (`update`,
the hints we print) keep the unpinned source so they get the newest commit.

Imports nothing from the package, so any module can use it.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

PACKAGE = "mcp-for-blender"
REPO_URL = "https://github.com/JosueMFlores/mcp-for-blender"
UPSTREAM_REPO_URL = "https://github.com/ahujasid/blender-mcp"
GIT_SOURCE = "git+" + REPO_URL
SOURCE_ENV = "BLENDER_MCP_SOURCE"
PYPI = "pypi"


def server_source(env=None) -> str | None:
    """The `--from` spec uvx runs the server from, or None for the PyPI package."""
    env = os.environ if env is None else env
    value = (env.get(SOURCE_ENV) or "").strip()
    if not value:
        return GIT_SOURCE
    if value.lower() == PYPI:
        return None
    return value


def describe_source(source: str | None) -> str:
    return "PyPI" if source is None else source


def run_args(source: str | None) -> list[str]:
    """What follows `uvx` to run the server from `source` (None: the PyPI package)."""
    return [PACKAGE] if source is None else ["--from", source, PACKAGE]


def uvx_args(*extra: str) -> list[str]:
    """What follows `uvx` to run the server (plus `extra`, e.g. a subcommand)."""
    return [*run_args(server_source()), *extra]


def _quoted(arg: str) -> str:
    # Only when needed, so a local checkout with spaces still pastes.
    return f'"{arg}"' if any(c.isspace() for c in arg) else arg


def uvx_command(*extra: str) -> str:
    """The uvx command line to show users, e.g. uvx_command("install-addon")."""
    return " ".join(["uvx", *map(_quoted, uvx_args(*extra))])


def fork_command(*extra: str, env=None, platform: str | None = None) -> str:
    """The command line that runs this fork's CLI (`extra`, e.g. "setup") with
    the current source, for hints about client entries.

    A non-default source rides along in BLENDER_MCP_SOURCE: without it setup
    writes this fork's default, and PyPI's `setup` is upstream's, which can't
    move entries off this fork.
    """
    env = os.environ if env is None else env
    value = (env.get(SOURCE_ENV) or "").strip()
    command = " ".join(["uvx", *map(_quoted, run_args(server_source(env) or GIT_SOURCE)), *extra])
    if not value:
        return command
    if (platform or sys.platform) == "win32":
        return f'$env:{SOURCE_ENV}="{value}"; {command}'  # PowerShell, like install.ps1
    return f"{SOURCE_ENV}={_quoted(value)} {command}"


def pip_command(source: str | None) -> str:
    """How to update a pip install of the server from `source`."""
    if source is None:
        return f"pip install -U {PACKAGE}"
    # pip skips a direct URL whose version it already has, and commits on the
    # repo don't always bump it.
    return f"pip install --upgrade --force-reinstall {_quoted(source)}"


def split_rev(source: str) -> tuple[str, str | None]:
    """(source without its @rev, the rev) for a git+ URL; other sources have none."""
    if not source.startswith("git+"):
        return source, None
    url, hash_, fragment = source.partition("#")
    scheme_end = url.find("://")
    # Past the host, so the @ of git+ssh://git@host isn't taken for a rev.
    path_start = url.find("/", scheme_end + 3) if scheme_end >= 0 else -1
    at = url.rfind("@", path_start) if path_start >= 0 else -1
    if at < 0:
        return source, None
    return url[:at] + hash_ + fragment, url[at + 1:] or None


def pin_source(source: str | None, timeout: float = 10.0) -> tuple[str | None, str]:
    """(`source` pinned to the commit its repo's HEAD is on, why it couldn't be).

    Only a git+ URL without a rev is looked up; anything else comes back as
    is. On failure the source comes back unpinned along with the reason.
    """
    if source is None or not source.startswith("git+") or split_rev(source)[1]:
        return source, ""
    url, hash_, fragment = source.partition("#")
    try:
        proc = subprocess.run(
            ["git", "ls-remote", url[len("git+"):], "HEAD"],
            capture_output=True, text=True, errors="replace", timeout=timeout, stdin=subprocess.DEVNULL,
            # Fail rather than wait for a password nobody will type.
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except subprocess.TimeoutExpired:
        return source, f"git ls-remote timed out after {timeout:g}s"
    except OSError as e:
        return source, f"couldn't run git: {e}"
    for line in (proc.stdout or "").splitlines():
        commit, _, ref = line.strip().partition("\t")
        if ref == "HEAD" and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
            return f"{url}@{commit}{hash_}{fragment}", ""
    errors = (proc.stderr or "").strip().splitlines()
    return source, errors[-1] if errors else "git ls-remote didn't report a HEAD commit"
