"""Tests for `mcp-for-blender update` (no network, uv or Blender touched)."""

from __future__ import annotations

import re
import types
from pathlib import Path

import pytest

from blender_mcp import update_cli
from blender_mcp.addon_manager import (
    addon_release_key,
    get_bundled_addon_path,
    update_installed_addons,
)
from blender_mcp.distribution import GIT_SOURCE, SOURCE_ENV
from blender_mcp.setup_cli import OUTDATED, Client, ClientState
from blender_mcp.update_cli import REEXEC_ENV, run_update, version_key

BUNDLED = get_bundled_addon_path().read_text(encoding="utf-8")
BUNDLED_KEY = addon_release_key(BUNDLED)


def _with_version(version, protocol) -> str:
    text = re.sub(r'"version": \(\d+, \d+(?:, \d+)?\)', f'"version": {version}', BUNDLED, count=1)
    return re.sub(r"^ADDON_PROTOCOL_VERSION = \d+", f"ADDON_PROTOCOL_VERSION = {protocol}", text, count=1, flags=re.M)


def _install(addons: Path, name: str, text: str) -> Path:
    addons.mkdir(parents=True, exist_ok=True)
    path = addons / name
    path.write_text(text, encoding="utf-8")
    return path


# --- addon -------------------------------------------------------------------

def test_release_key_reads_bundled_addon():
    assert BUNDLED_KEY is not None
    assert addon_release_key(_with_version((2, 0, 3), 40)) == (2, 0, 3, 40)
    assert addon_release_key("print('hello')") is None


def test_updates_older_addon_in_place_with_backup(tmp_path):
    old = _with_version((1, 0), 1)
    path = _install(tmp_path, "addon.py", old)
    [result] = update_installed_addons([tmp_path])
    assert result.action == "updated" and result.installed == (1, 0, 0, 1)
    assert path.read_text(encoding="utf-8") == BUNDLED
    assert path.with_suffix(".py.bak").read_text(encoding="utf-8") == old
    # Writes over the existing file only; no second copy under another name.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["addon.py", "addon.py.bak"]


def test_updates_addon_from_before_the_rename(tmp_path):
    old = 'bl_info = {\n    "name": "Blender MCP",\n    "version": (1, 2),\n}\n'
    path = _install(tmp_path, "blender_mcp.py", old)
    [result] = update_installed_addons([tmp_path])
    assert result.action == "updated"
    assert path.read_text(encoding="utf-8") == BUNDLED


def test_never_downgrades_addon_from_main(tmp_path):
    major, minor, patch, protocol = BUNDLED_KEY
    newer = _with_version((major, minor + 1), protocol)
    path = _install(tmp_path, "blender_mcp.py", newer)
    [result] = update_installed_addons([tmp_path])
    assert result.action == "newer"
    assert path.read_text(encoding="utf-8") == newer


def test_same_release_with_local_edits_is_left_alone(tmp_path):
    edited = BUNDLED + "\n# my tweak\n"
    path = _install(tmp_path, "blender_mcp.py", edited)
    [result] = update_installed_addons([tmp_path])
    assert result.action == "current"
    assert path.read_text(encoding="utf-8") == edited


def test_dry_run_writes_nothing(tmp_path):
    old = _with_version((1, 0), 1)
    path = _install(tmp_path, "blender_mcp.py", old)
    [result] = update_installed_addons([tmp_path], dry_run=True)
    assert result.action == "updated"
    assert path.read_text(encoding="utf-8") == old
    assert not path.with_suffix(".py.bak").exists()


# --- server ------------------------------------------------------------------

def test_version_key():
    assert version_key("2.1.10") > version_key("2.1.9")
    assert version_key("2.2.0rc1") == (2, 2, 0)
    assert version_key("junk") == ()


@pytest.fixture
def quiet(monkeypatch):
    """No clients, Blender or addon installs on the test machine leak in."""
    monkeypatch.delenv(REEXEC_ENV, raising=False)
    monkeypatch.delenv(SOURCE_ENV, raising=False)
    monkeypatch.setattr(update_cli, "detect_clients", lambda: [])
    monkeypatch.setattr(update_cli, "blender_is_running", lambda: False)
    monkeypatch.setattr(update_cli, "update_installed_addons", lambda dry_run=False: [])
    monkeypatch.setattr(update_cli, "current_version", lambda: "2.1.0")


@pytest.fixture
def pypi(quiet, monkeypatch):
    """BLENDER_MCP_SOURCE=pypi: upstream's package and its release checks."""
    monkeypatch.setenv(SOURCE_ENV, "pypi")


def _record_runs(monkeypatch, calls):
    monkeypatch.setattr(update_cli.subprocess, "run",
                        lambda cmd, env=None, **kw: calls.append((cmd, env)) or types.SimpleNamespace(returncode=0))


# --- from PyPI ---------------------------------------------------------------

def test_newer_release_reruns_from_it_through_uvx(pypi, monkeypatch):
    calls = []
    monkeypatch.setattr(update_cli, "fetch_latest_version", lambda: ("2.2.0", ""))
    monkeypatch.setattr(update_cli, "find_uvx", lambda: "/abs/uvx")
    monkeypatch.setattr(update_cli, "find_uv", lambda uvx: None)
    monkeypatch.setattr(update_cli.subprocess, "run",
                        lambda cmd, env=None, **kw: calls.append((cmd, env)) or types.SimpleNamespace(returncode=0))
    assert run_update() == 0
    [(cmd, env)] = calls
    assert cmd == ["/abs/uvx", "--refresh-package", "mcp-for-blender", "mcp-for-blender@2.2.0", "update"]
    assert env[REEXEC_ENV] == "2.1.0"


def test_uv_tool_install_is_upgraded_first(pypi, monkeypatch):
    calls = []
    monkeypatch.setattr(update_cli, "fetch_latest_version", lambda: ("2.2.0", ""))
    monkeypatch.setattr(update_cli, "find_uvx", lambda: "/abs/uvx")
    monkeypatch.setattr(update_cli, "find_uv", lambda uvx: "/abs/uv")
    monkeypatch.setattr(update_cli, "uv_tool_installed", lambda uv: True)
    monkeypatch.setattr(update_cli.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd) or types.SimpleNamespace(returncode=0))
    assert run_update() == 0
    assert calls[0] == ["/abs/uv", "tool", "upgrade", "mcp-for-blender"]
    assert calls[1][0] == "/abs/uvx"


def test_latest_release_updates_addon_without_rerunning(pypi, monkeypatch, capsys):
    monkeypatch.setattr(update_cli, "fetch_latest_version", lambda: ("2.1.0", ""))
    monkeypatch.setattr(update_cli.subprocess, "run", lambda *a, **kw: pytest.fail("should not re-run"))
    assert run_update() == 0
    out = capsys.readouterr().out
    assert "Server source: PyPI" in out
    assert "2.1.0, the latest release" in out
    assert "`uvx mcp-for-blender setup`" in out  # addon not installed → points at setup


def test_rerun_does_not_check_pypi_again(pypi, monkeypatch, capsys):
    monkeypatch.setenv(REEXEC_ENV, "2.0.0")
    monkeypatch.setattr(update_cli, "fetch_latest_version", lambda: pytest.fail("should not loop"))
    assert run_update() == 0
    out = capsys.readouterr().out
    assert "updated 2.0.0 → 2.1.0" in out
    assert "Restart your MCP clients" in out


def test_offline_still_updates_addon(pypi, monkeypatch, capsys):
    monkeypatch.setattr(update_cli, "fetch_latest_version", lambda: (None, "no network"))
    assert run_update() == 0
    assert "couldn't check PyPI" in capsys.readouterr().out


def test_without_uvx_points_at_pip(pypi, monkeypatch, capsys):
    monkeypatch.setattr(update_cli, "fetch_latest_version", lambda: ("2.2.0", ""))
    monkeypatch.setattr(update_cli, "find_uvx", lambda: None)
    assert run_update() == 1
    assert "pip install -U mcp-for-blender" in capsys.readouterr().out


# --- from this fork's repo (the default) ------------------------------------

def test_git_source_always_refreshes_from_it(quiet, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(update_cli, "fetch_latest_version", lambda: pytest.fail("no PyPI release to compare"))
    monkeypatch.setattr(update_cli, "find_uvx", lambda: "/abs/uvx")
    monkeypatch.setattr(update_cli, "find_uv", lambda uvx: None)
    _record_runs(monkeypatch, calls)
    assert run_update() == 0
    [(cmd, env)] = calls
    assert cmd == ["/abs/uvx", "--refresh-package", "mcp-for-blender",
                   "--from", GIT_SOURCE, "mcp-for-blender", "update"]
    assert env[REEXEC_ENV] == "2.1.0"
    out = capsys.readouterr().out
    assert f"Server source: {GIT_SOURCE}" in out
    assert f"refreshing from {GIT_SOURCE}" in out


def test_git_source_dry_run_passes_it_on(quiet, monkeypatch):
    calls = []
    monkeypatch.setattr(update_cli, "find_uvx", lambda: "/abs/uvx")
    monkeypatch.setattr(update_cli, "find_uv", lambda uvx: "/abs/uv")
    monkeypatch.setattr(update_cli, "uv_tool_installed", lambda uv: True)
    _record_runs(monkeypatch, calls)
    assert run_update(dry_run=True) == 0
    [(cmd, _)] = calls  # the uv tool upgrade is only printed
    assert cmd[-2:] == ["update", "--dry-run"]


def test_git_source_upgrades_uv_tool_install_first(quiet, monkeypatch):
    calls = []
    monkeypatch.setattr(update_cli, "find_uvx", lambda: "/abs/uvx")
    monkeypatch.setattr(update_cli, "find_uv", lambda uvx: "/abs/uv")
    monkeypatch.setattr(update_cli, "uv_tool_installed", lambda uv: True)
    _record_runs(monkeypatch, calls)
    assert run_update() == 0
    assert [cmd for cmd, _ in calls] == [
        ["/abs/uv", "tool", "upgrade", "mcp-for-blender"],
        ["/abs/uvx", "--refresh-package", "mcp-for-blender", "--from", GIT_SOURCE, "mcp-for-blender", "update"],
    ]


def test_custom_source_is_refreshed_from(quiet, monkeypatch):
    calls = []
    monkeypatch.setenv(SOURCE_ENV, "/src/checkout")
    monkeypatch.setattr(update_cli, "find_uvx", lambda: "/abs/uvx")
    monkeypatch.setattr(update_cli, "find_uv", lambda uvx: None)
    _record_runs(monkeypatch, calls)
    assert run_update() == 0
    [(cmd, _)] = calls
    assert cmd[3:] == ["--from", "/src/checkout", "mcp-for-blender", "update"]


@pytest.mark.parametrize("updated_from, line", [
    ("2.1.0", f"refreshed 2.1.0 from {GIT_SOURCE}"),
    ("2.0.0", "updated 2.0.0 → 2.1.0"),
])
def test_git_source_rerun_does_not_loop(quiet, monkeypatch, capsys, updated_from, line):
    monkeypatch.setenv(REEXEC_ENV, updated_from)
    monkeypatch.setattr(update_cli.subprocess, "run", lambda *a, **kw: pytest.fail("should not loop"))
    assert run_update() == 0
    out = capsys.readouterr().out
    assert line in out
    assert "Restart your MCP clients" in out


def test_git_source_without_uvx_still_updates_addon(quiet, monkeypatch, capsys):
    monkeypatch.setattr(update_cli, "find_uvx", lambda: None)
    monkeypatch.setattr(update_cli.subprocess, "run", lambda *a, **kw: pytest.fail("nothing to run"))
    assert run_update() == 0
    out = capsys.readouterr().out
    assert f"Update the server with `pip install -U {GIT_SOURCE}`" in out
    assert "Blender addon:" in out
    assert "Nothing to do" not in out


def test_points_clients_on_another_source_at_setup(quiet, monkeypatch, capsys):
    monkeypatch.setenv(REEXEC_ENV, "2.1.0")
    monkeypatch.setattr(update_cli, "detect_clients", lambda: [Client("cursor", "Cursor", "json")])
    monkeypatch.setattr(update_cli, "client_state",
                        lambda c: ClientState(OUTDATED, "blender", "runs mcp-for-blender from PyPI"))
    assert run_update() == 0
    out = capsys.readouterr().out
    assert "Cursor (runs mcp-for-blender from PyPI)" in out
    assert f"`uvx --from {GIT_SOURCE} mcp-for-blender setup`" in out
