"""Tests for where the server is installed and updated from (distribution.py)."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from blender_mcp import distribution
from blender_mcp.distribution import (
    GIT_SOURCE,
    PACKAGE,
    REPO_URL,
    SOURCE_ENV,
    describe_source,
    fork_command,
    pin_source,
    pip_command,
    server_source,
    split_rev,
    uvx_args,
    uvx_command,
)

SHA = "1a2b3c4d5e6f" + "0" * 28

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


def test_fork_command_carries_a_non_default_source():
    setup = f"uvx --from {GIT_SOURCE} mcp-for-blender setup"
    assert fork_command("setup", env={}) == setup
    # PyPI's setup is upstream's, so this fork's runs, told to write PyPI entries.
    assert fork_command("setup", env={SOURCE_ENV: "pypi"}, platform="linux") == f"{SOURCE_ENV}=pypi {setup}"
    assert fork_command("setup", env={SOURCE_ENV: "pypi"}, platform="win32") == f'$env:{SOURCE_ENV}="pypi"; {setup}'
    assert fork_command("update", env={SOURCE_ENV: "/src/my checkout"}, platform="darwin") == (
        f'{SOURCE_ENV}="/src/my checkout" uvx --from "/src/my checkout" mcp-for-blender update')


def test_pip_command():
    assert pip_command(None) == "pip install -U mcp-for-blender"
    # pip skips a direct URL whose version it already has.
    assert pip_command(GIT_SOURCE) == f"pip install --upgrade --force-reinstall {GIT_SOURCE}"
    assert pip_command("/src/my checkout") == 'pip install --upgrade --force-reinstall "/src/my checkout"'


# --- pinning client entries to a commit --------------------------------------

@pytest.mark.parametrize("source, base, rev", [
    (GIT_SOURCE, GIT_SOURCE, None),
    (f"{GIT_SOURCE}@{SHA}", GIT_SOURCE, SHA),
    ("git+https://example.com/me/fork.git@feature/x", "git+https://example.com/me/fork.git", "feature/x"),
    ("git+ssh://git@github.com/me/fork", "git+ssh://git@github.com/me/fork", None),
    ("git+ssh://git@github.com/me/fork@v2#subdirectory=pkg", "git+ssh://git@github.com/me/fork#subdirectory=pkg", "v2"),
    ("git+file:///src/fork@main", "git+file:///src/fork", "main"),
    ("/Users/me@work/checkout", "/Users/me@work/checkout", None),
])
def test_split_rev(source, base, rev):
    assert split_rev(source) == (base, rev)


def _git(monkeypatch, result):
    """Fake subprocess.run: `result` is what it returns or raises; returns its calls."""
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if isinstance(result, BaseException):
            raise result
        return subprocess.CompletedProcess(cmd, *result)

    monkeypatch.setattr(distribution.subprocess, "run", run)
    return calls


def test_pin_source_pins_to_the_repos_head(monkeypatch):
    calls = _git(monkeypatch, (0, f"{SHA}\tHEAD\n", ""))
    assert pin_source(GIT_SOURCE) == (f"{GIT_SOURCE}@{SHA}", "")
    [(cmd, kwargs)] = calls
    assert cmd == ["git", "ls-remote", REPO_URL, "HEAD"]
    assert kwargs["timeout"] <= 15 and kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"


def test_pin_source_keeps_a_fragment_after_the_commit(monkeypatch):
    calls = _git(monkeypatch, (0, f"{SHA}\tHEAD\n", ""))
    assert pin_source("git+https://example.com/fork#subdirectory=pkg") == (
        f"git+https://example.com/fork@{SHA}#subdirectory=pkg", "")
    assert calls[0][0][2] == "https://example.com/fork"


@pytest.mark.parametrize("result, error", [
    ((128, "", "fatal: unable to access 'https://github.com/x/': Could not resolve host: github.com\n"),
     "fatal: unable to access 'https://github.com/x/': Could not resolve host: github.com"),
    ((0, "", ""), "git ls-remote didn't report a HEAD commit"),
    (FileNotFoundError(2, "No such file or directory", "git"), "couldn't run git: "),
    (subprocess.TimeoutExpired(["git"], 10), "git ls-remote timed out after 10s"),
])
def test_pin_source_falls_back_to_unpinned(monkeypatch, result, error):
    _git(monkeypatch, result)
    source, reason = pin_source(GIT_SOURCE)
    assert source == GIT_SOURCE and reason.startswith(error)


@pytest.mark.parametrize("source", [None, f"{GIT_SOURCE}@dev", "/src/checkout"])
def test_pin_source_leaves_other_sources_alone(monkeypatch, source):
    monkeypatch.setattr(distribution.subprocess, "run", lambda *a, **kw: pytest.fail("no lookup needed"))
    assert pin_source(source) == (source, "")


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
