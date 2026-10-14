"""
Where this fork's server is installed and updated from.

The `mcp-for-blender` package on PyPI is published by upstream
(github.com/ahujasid/blender-mcp), so a plain `uvx mcp-for-blender` would run
upstream's server and install upstream's addon. Client entries, `update` and
the hints we print instead run uvx with `--from` pointed at this repo.

BLENDER_MCP_SOURCE overrides that: "pypi" goes back to the PyPI package
(upstream), and any other value (another git URL, a local checkout) is passed
to `--from` as is.

Imports nothing from the package, so any module can use it.
"""

from __future__ import annotations

import os

PACKAGE = "mcp-for-blender"
REPO_URL = "https://github.com/JosueMFlores/mcp-for-blender"
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


def uvx_args(*extra: str) -> list[str]:
    """What follows `uvx` to run the server (plus `extra`, e.g. a subcommand)."""
    source = server_source()
    if source is None:
        return [PACKAGE, *extra]
    return ["--from", source, PACKAGE, *extra]


def uvx_command(*extra: str) -> str:
    """The uvx command line to show users, e.g. uvx_command("install-addon")."""
    # Quoted only when needed, so a local checkout with spaces still pastes.
    args = [f'"{a}"' if any(c.isspace() for c in a) else a for a in uvx_args(*extra)]
    return " ".join(["uvx", *args])
