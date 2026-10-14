"""Tests for where the server is installed and updated from (distribution.py)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from blender_mcp.distribution import (
    GIT_SOURCE,
    PACKAGE,
    REPO_URL,
    SOURCE_ENV,
    describe_source,
    server_source,
    uvx_args,
    uvx_command,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def default_source(monkeypatch):
    monkeypatch.delenv(SOURCE_ENV, raising=False)


def test_defaults_to_this_fork():
    assert REPO_URL == "https://github.com/JosueMFlores/mcp-for-blender"
    assert GIT_SOURCE == "git+" + REPO_URL
    assert server_source() == GIT_SOURCE
    assert uvx_args() == ["--from", GIT_SOURCE, PACKAGE]
    assert uvx_command("install-addon") == (
        "uvx --from git+https://github.com/JosueMFlores/mcp-for-blender mcp-for-blender install-addon")


@pytest.mark.parametrize("value", ["pypi", "PyPI", " pypi "])
def test_pypi_goes_back_to_the_upstream_package(monkeypatch, value):
    monkeypatch.setenv(SOURCE_ENV, value)
    assert server_source() is None
    assert uvx_args("setup") == ["mcp-for-blender", "setup"]
    assert uvx_command("install-addon") == "uvx mcp-for-blender install-addon"
    assert describe_source(server_source()) == "PyPI"


def test_any_other_value_is_used_as_is(monkeypatch):
    monkeypatch.setenv(SOURCE_ENV, "git+https://example.com/me/fork@dev")
    assert uvx_args("update") == ["--from", "git+https://example.com/me/fork@dev", PACKAGE, "update"]


def test_blank_value_means_this_fork(monkeypatch):
    monkeypatch.setenv(SOURCE_ENV, "  ")
    assert server_source() == GIT_SOURCE


def test_server_source_reads_the_env_it_is_given():
    assert server_source({}) == GIT_SOURCE
    assert server_source({SOURCE_ENV: "pypi"}) is None
    assert server_source({SOURCE_ENV: "/src/checkout"}) == "/src/checkout"


def test_command_quotes_a_path_with_spaces(monkeypatch):
    monkeypatch.setenv(SOURCE_ENV, "/Users/me/my checkout")
    assert uvx_command("setup") == 'uvx --from "/Users/me/my checkout" mcp-for-blender setup'


# --- everything else that names the source ------------------------------------

def test_codex_plugin_runs_what_setup_writes():
    path = ROOT / "integrations" / "codex" / "plugins" / "mcp-for-blender" / ".mcp.json"
    [entry] = json.loads(path.read_text(encoding="utf-8"))["mcpServers"].values()
    assert entry["command"] == "uvx" and entry["args"] == uvx_args()
    plugin = json.loads((path.parent / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert plugin["repository"] == REPO_URL


@pytest.mark.parametrize("name", ["install.sh", "install.ps1"])
def test_installers_default_to_this_fork(name):
    text = (ROOT / name).read_text(encoding="utf-8")
    assert GIT_SOURCE in text and SOURCE_ENV in text
    assert "mcp-for-blender.com/install" not in text


def test_addon_updates_from_this_fork():
    text = (ROOT / "addon.py").read_text(encoding="utf-8")
    update_url = re.search(r'^ADDON_UPDATE_URL = "([^"]+)"', text, re.M).group(1)
    assert update_url == "https://raw.githubusercontent.com/JosueMFlores/mcp-for-blender/main/addon.py"
    assert "github.com/ahujasid" not in text.split("\n", 1)[1], "only the copyright line credits upstream"


def test_no_hint_runs_the_upstream_package():
    """Hints go through uvx_command(), so they follow BLENDER_MCP_SOURCE."""
    for path in (ROOT / "src" / "blender_mcp").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"uvx mcp-for-blender (install-addon|setup|update|addon-paths)", text), path.name
