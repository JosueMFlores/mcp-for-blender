"""Tests for `mcp-for-blender setup` (no real clients or Blender touched)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from blender_mcp import setup_cli
from blender_mcp.distribution import GIT_SOURCE, SOURCE_ENV
from blender_mcp.setup_cli import (
    CONFIGURED,
    NEW,
    OUTDATED,
    UNREADABLE,
    Client,
    ClientState,
    _parse_selection,
    _strip_jsonc,
    addon_module_name,
    codex_state,
    configure_json_client,
    detect_clients,
    json_client_state,
    safe_to_save_prefs,
    servers_state,
)

UVX = "/abs/bin/uvx"
# What entries run by default: the server from this fork's repo.
FORK_ARGS = ["--from", GIT_SOURCE, "mcp-for-blender"]


@pytest.fixture(autouse=True)
def default_source(monkeypatch):
    monkeypatch.delenv(SOURCE_ENV, raising=False)


@pytest.fixture
def pypi(monkeypatch):
    monkeypatch.setenv(SOURCE_ENV, "pypi")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path


def _client(path: Path, style: str = "mcpServers") -> Client:
    return Client("x", "X", "json", path, style=style)


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")


# --- merging config files ---------------------------------------------------

def test_adds_entry_and_keeps_other_servers(tmp_path):
    path = tmp_path / "claude_desktop_config.json"
    _write(path, {"mcpServers": {"other": {"command": "npx", "args": ["x"]}}, "theme": "dark"})

    result = configure_json_client(_client(path), UVX, ClientState(NEW))

    assert result.ok
    data = json.loads(path.read_text())
    assert data["mcpServers"]["other"] == {"command": "npx", "args": ["x"]}
    assert data["mcpServers"]["blender"] == {"command": UVX, "args": FORK_ARGS}
    assert data["theme"] == "dark"
    assert result.backup and json.loads(result.backup.read_text())["mcpServers"].keys() == {"other"}


def test_creates_missing_file_without_backup(tmp_path):
    path = tmp_path / "sub" / "mcp.json"
    result = configure_json_client(_client(path), UVX, ClientState(NEW))
    assert result.ok and result.backup is None
    assert json.loads(path.read_text()) == {"mcpServers": {"blender": {"command": UVX, "args": FORK_ARGS}}}


def test_vscode_and_opencode_formats(tmp_path):
    vscode = tmp_path / "mcp.json"
    configure_json_client(_client(vscode, "vscode"), UVX, ClientState(NEW))
    assert json.loads(vscode.read_text()) == {
        "servers": {"blender": {"type": "stdio", "command": UVX, "args": FORK_ARGS}}
    }

    opencode = tmp_path / "opencode.json"
    configure_json_client(_client(opencode, "opencode"), UVX, ClientState(NEW))
    data = json.loads(opencode.read_text())
    assert data["mcp"]["blender"] == {"type": "local", "command": [UVX, *FORK_ARGS], "enabled": True}
    assert data["$schema"] == "https://opencode.ai/config.json"


def test_does_not_take_over_an_unrelated_blender_entry(tmp_path):
    path = tmp_path / "mcp.json"
    _write(path, {"mcpServers": {"blender": {"command": "some-other-blender-tool"}}})
    configure_json_client(_client(path), UVX, ClientState(NEW))
    servers = json.loads(path.read_text())["mcpServers"]
    assert servers["blender"] == {"command": "some-other-blender-tool"}
    assert servers["blender-2"]["args"] == FORK_ARGS


def test_update_replaces_old_package_and_keeps_env(tmp_path):
    path = tmp_path / "mcp.json"
    _write(path, {"mcpServers": {"blender-mcp": {
        "command": "uvx", "args": ["blender-mcp"], "env": {"BLENDER_PORT": "9877"}}}})
    client = _client(path)

    state = json_client_state(client)
    assert (state.status, state.entry_key, state.detail) == (OUTDATED, "blender-mcp", "runs the old blender-mcp package")
    configure_json_client(client, UVX, state)

    entry = json.loads(path.read_text())["mcpServers"]["blender-mcp"]
    assert entry == {"command": UVX, "args": FORK_ARGS, "env": {"BLENDER_PORT": "9877"}}
    assert json_client_state(client).status == CONFIGURED


def test_update_moves_pypi_entry_to_this_fork_in_place(tmp_path):
    path = tmp_path / "mcp.json"
    _write(path, {"mcpServers": {"my-blender": {
        "command": "uvx", "args": ["mcp-for-blender"], "env": {"BLENDER_PORT": "9877"}, "disabled": False}}})
    client = _client(path)

    state = json_client_state(client)
    assert (state.status, state.entry_key, state.detail) == (OUTDATED, "my-blender", "runs mcp-for-blender from PyPI")
    result = configure_json_client(client, UVX, state)

    assert result.ok and result.message.startswith('Updated "my-blender"')
    assert json.loads(path.read_text())["mcpServers"] == {"my-blender": {
        "command": UVX, "args": FORK_ARGS, "env": {"BLENDER_PORT": "9877"}, "disabled": False}}
    assert json_client_state(client).status == CONFIGURED


def test_jsonc_is_read_and_invalid_json_left_alone(tmp_path):
    path = tmp_path / "mcp.json"
    _write(path, '{\n  // mine\n  "servers": {"a": {"command": "x // not a comment"},},\n}\n')
    result = configure_json_client(_client(path, "vscode"), UVX, ClientState(NEW))
    assert result.ok and "comments" in result.message
    assert json.loads(path.read_text())["servers"]["a"] == {"command": "x // not a comment"}

    broken = tmp_path / "broken.json"
    _write(broken, "{not json")
    assert json_client_state(_client(broken)).status == UNREADABLE
    assert not configure_json_client(_client(broken), UVX, ClientState(NEW)).ok
    assert broken.read_text() == "{not json"


def test_strip_jsonc_keeps_strings():
    assert json.loads(_strip_jsonc('{"url": "http://x/*y*/", /* c */ "a": [1,],}')) == {
        "url": "http://x/*y*/", "a": [1]}


def _state(*args, command="/x/uvx"):
    return servers_state({"b": {"command": command, "args": list(args)}})


def test_servers_state():
    assert servers_state({}).status == NEW
    assert _state(*FORK_ARGS).status == CONFIGURED
    # The same repo written another way.
    assert _state("--from", GIT_SOURCE + ".git", "mcp-for-blender").status == CONFIGURED
    assert _state("--from=" + GIT_SOURCE + "/", "mcp-for-blender").status == CONFIGURED
    assert _state("blender-mcp-extras", command="uvx").status == NEW


@pytest.mark.parametrize("args, command, detail", [
    (["mcp-for-blender"], "/x/uvx", "runs mcp-for-blender from PyPI"),
    (["mcp-for-blender@latest"], "/x/uvx", "runs mcp-for-blender from PyPI"),
    (["--from", "mcp-for-blender==2.1.0", "mcp-for-blender"], "/x/uvx", "runs mcp-for-blender from PyPI"),
    ([], "mcp-for-blender", "runs mcp-for-blender from PyPI"),  # pipx runs the package's own command
    (["--from", "git+https://github.com/ahujasid/blender-mcp", "mcp-for-blender"], "uvx",
     "runs mcp-for-blender from git+https://github.com/ahujasid/blender-mcp"),
    (["blender-mcp"], "uvx", "runs the old blender-mcp package"),
])
def test_entries_from_elsewhere_are_outdated(args, command, detail):
    state = _state(*args, command=command)
    assert (state.status, state.entry_key, state.detail) == (OUTDATED, "b", detail)


def test_an_entry_already_on_this_fork_wins():
    servers = {"old": {"command": "uvx", "args": ["mcp-for-blender"]}, "new": {"command": "uvx", "args": FORK_ARGS}}
    assert (servers_state(servers).status, servers_state(servers).entry_key) == (CONFIGURED, "new")


def test_pypi_source_keeps_upstream_behaviour(pypi):
    assert _state("mcp-for-blender").status == CONFIGURED
    assert _state("mcp-for-blender@latest").status == CONFIGURED
    assert _state(command="mcp-for-blender").status == CONFIGURED
    assert _state("blender-mcp").status == OUTDATED
    # Going back to upstream rewrites entries that run this fork.
    assert _state(*FORK_ARGS).status == OUTDATED
    assert setup_cli.server_entry("mcpServers", UVX) == {"command": UVX, "args": ["mcp-for-blender"]}
    client = Client("claude-code", "Claude Code", "claude-code", cli_path="/bin/claude")
    [add] = setup_cli._cli_commands(client, UVX, ClientState(NEW))
    assert add[-2:] == [UVX, "mcp-for-blender"]


def test_custom_source_is_written_and_recognised(monkeypatch, tmp_path):
    monkeypatch.setenv(SOURCE_ENV, "/src/my checkout")
    path = tmp_path / "mcp.json"
    configure_json_client(_client(path), UVX, ClientState(NEW))
    assert json.loads(path.read_text())["mcpServers"]["blender"]["args"] == [
        "--from", "/src/my checkout", "mcp-for-blender"]
    assert json_client_state(_client(path)).status == CONFIGURED
    assert _state(*FORK_ARGS).status == OUTDATED


# --- Codex -------------------------------------------------------------------

def test_codex_state_reads_config_toml(tmp_path):
    env = {"CODEX_HOME": str(tmp_path)}
    assert codex_state(env).status == NEW

    (tmp_path / "config.toml").write_text(
        'model = "x"\n[mcp_servers.blender]\ncommand = "uvx"\nargs = ["blender-mcp"]\n'
        '[mcp_servers.blender.env]\nBLENDER_PORT = "9876"\n'
    )
    state = codex_state(env)
    assert (state.status, state.entry_key) == (OUTDATED, "blender")

    (tmp_path / "config.toml").write_text('[mcp_servers."my blender"]\ncommand = "/x/uvx"\nargs = ["mcp-for-blender"]\n')
    state = codex_state(env)
    assert (state.status, state.entry_key, state.detail) == (OUTDATED, "my blender", "runs mcp-for-blender from PyPI")

    (tmp_path / "config.toml").write_text(
        '[mcp_servers.blender]\ncommand = \'C:\\uv\\uvx.exe\'\n'
        f'args = ["--from", "{GIT_SOURCE}", "mcp-for-blender"]\n'
        '[mcp_servers.blender.env]\nBLENDER_MCP_APPS = "1"\n'
    )
    assert codex_state(env).status == CONFIGURED


def test_codex_state_with_pypi_source(tmp_path, pypi):
    env = {"CODEX_HOME": str(tmp_path)}
    (tmp_path / "config.toml").write_text('[mcp_servers.blender]\ncommand = "/x/uvx"\nargs = ["mcp-for-blender"]\n')
    assert codex_state(env).status == CONFIGURED


def test_codex_plugin_counts_as_configured(tmp_path):
    env = {"CODEX_HOME": str(tmp_path)}
    (tmp_path / "plugins" / "cache" / "mcp-for-blender" / "mcp-for-blender").mkdir(parents=True)
    assert codex_state(env).status == CONFIGURED


def test_codex_commands_carry_plugin_env():
    client = Client("codex", "Codex", "codex", cli_path="/bin/codex")
    [add] = setup_cli._cli_commands(client, UVX, ClientState(NEW))
    assert add == ["/bin/codex", "mcp", "add", "blender", "--env", "BLENDER_MCP_APPS=1",
                   "--env", "BLENDER_MCP_OPENAI_FORMS=1", "--", UVX, *FORK_ARGS]
    remove, add = setup_cli._cli_commands(client, UVX, ClientState(OUTDATED, "blender-mcp"))
    assert remove == ["/bin/codex", "mcp", "remove", "blender-mcp"]
    assert add[3] == "blender-mcp"


def test_claude_code_uses_user_scope():
    client = Client("claude-code", "Claude Code", "claude-code", cli_path="/bin/claude")
    [add] = setup_cli._cli_commands(client, UVX, ClientState(NEW))
    assert add == ["/bin/claude", "mcp", "add", "--scope", "user", "blender", "--", UVX, *FORK_ARGS]


def test_claude_code_moves_pypi_entry_to_this_fork(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _write(tmp_path / ".claude.json", {"mcpServers": {"blender": {"type": "stdio", "command": "uvx",
                                                                  "args": ["mcp-for-blender"]}}})
    state = setup_cli.claude_code_state()
    assert (state.status, state.entry_key) == (OUTDATED, "blender")
    client = Client("claude-code", "Claude Code", "claude-code", cli_path="/bin/claude")
    remove, add = setup_cli._cli_commands(client, UVX, state)
    assert remove == ["/bin/claude", "mcp", "remove", "--scope", "user", "blender"]
    assert add == ["/bin/claude", "mcp", "add", "--scope", "user", "blender", "--", UVX, *FORK_ARGS]


# --- detection ---------------------------------------------------------------

def test_detects_clients_by_config_folders(home):
    (home / "Library" / "Application Support" / "Claude").mkdir(parents=True)
    (home / ".cursor").mkdir()
    (home / ".codeium" / "windsurf").mkdir(parents=True)
    (home / ".gemini" / "config").mkdir(parents=True)
    which = {"claude": "/bin/claude"}.get

    clients = {c.key: c for c in detect_clients("darwin", {}, which)}

    assert set(clients) == {"claude-desktop", "claude-code", "cursor", "devin", "antigravity"}
    assert clients["devin"].config_path == home / ".codeium" / "windsurf" / "mcp_config.json"
    assert clients["antigravity"].config_path == home / ".gemini" / "config" / "mcp_config.json"


def test_windows_prefers_microsoft_store_claude_folder(home):
    local = home / "AppData" / "Local"
    msix = local / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude"
    msix.mkdir(parents=True)
    (home / "AppData" / "Roaming" / "Claude").mkdir(parents=True)

    env = {"LOCALAPPDATA": str(local), "APPDATA": str(home / "AppData" / "Roaming")}
    [claude] = [c for c in detect_clients("win32", env, lambda _: None) if c.key == "claude-desktop"]
    assert claude.config_path == msix / "claude_desktop_config.json"


def test_devin_prefers_new_folder(home):
    (home / ".codeium" / "windsurf").mkdir(parents=True)
    (home / ".config" / "devin").mkdir(parents=True)
    [devin] = [c for c in detect_clients("linux", {}, lambda _: None) if c.key == "devin"]
    assert devin.config_path == home / ".config" / "devin" / "mcp_config.json"


def test_opencode_keeps_existing_jsonc(home):
    (home / ".config" / "opencode").mkdir(parents=True)
    (home / ".config" / "opencode" / "opencode.jsonc").write_text("{}")
    [oc] = [c for c in detect_clients("darwin", {}, lambda _: None) if c.key == "opencode"]
    assert oc.config_path.name == "opencode.jsonc"


# --- Blender -----------------------------------------------------------------

def test_addon_module_name():
    assert addon_module_name(Path("/b/4.2/scripts/addons/blender_mcp.py")) == "blender_mcp"
    assert addon_module_name(Path("/b/4.2/extensions/user_default/blender_mcp.py")) == \
        "bl_ext.user_default.blender_mcp"


def test_safe_to_save_prefs_protects_settings_migration(tmp_path):
    # Only version: nothing to migrate from.
    (tmp_path / "4.2").mkdir()
    assert safe_to_save_prefs(tmp_path, (4, 2))
    # A newer version never opened, with an older one to import from: hands off.
    assert not safe_to_save_prefs(tmp_path, (4, 3))
    # Once it has its own prefs, it's fine.
    (tmp_path / "4.3" / "config").mkdir(parents=True)
    (tmp_path / "4.3" / "config" / "userpref.blend").write_bytes(b"")
    assert safe_to_save_prefs(tmp_path, (4, 3))


def test_parse_selection():
    assert _parse_selection("", 3) == [0, 1, 2]
    assert _parse_selection("n", 3) == []
    assert _parse_selection("3, 1 3", 3) == [2, 0]
    assert _parse_selection("4", 3) is None
    assert _parse_selection("x", 3) is None


# --- the command -------------------------------------------------------------

def test_dry_run_changes_nothing(home, monkeypatch, capsys):
    cursor = home / ".cursor" / "mcp.json"
    _write(cursor, {"mcpServers": {"other": {"command": "x"}}})
    before = cursor.read_text()
    monkeypatch.setattr(setup_cli, "find_uvx", lambda: UVX)
    monkeypatch.setattr(setup_cli, "detect_clients", lambda: [Client("cursor", "Cursor", "json", cursor)])

    assert setup_cli.run_setup(dry_run=True, assume_yes=True, skip_addon=True) == 0
    assert cursor.read_text() == before
    assert not cursor.with_name("mcp.json.bak").exists()
    assert "would add" in capsys.readouterr().out


def test_run_setup_configures_and_skips_configured(home, monkeypatch, capsys):
    cursor = home / ".cursor" / "mcp.json"
    vscode = home / "vscode" / "mcp.json"
    _write(vscode, {"servers": {"blender": {"command": "uvx", "args": FORK_ARGS}}})
    vscode_before = vscode.read_text()
    monkeypatch.setattr(setup_cli, "find_uvx", lambda: UVX)
    monkeypatch.setattr(setup_cli, "detect_clients", lambda: [
        Client("cursor", "Cursor", "json", cursor, restart_hint="Restart Cursor."),
        Client("vscode", "VS Code", "json", vscode, style="vscode"),
    ])

    assert setup_cli.run_setup(assume_yes=True, skip_addon=True) == 0
    assert json.loads(cursor.read_text())["mcpServers"]["blender"]["command"] == UVX
    assert vscode.read_text() == vscode_before
    out = capsys.readouterr().out
    assert "already set up" in out and "Restart Cursor." in out
    assert f"Server source: {GIT_SOURCE}" in out


def test_run_setup_moves_pypi_entries_to_this_fork(home, monkeypatch, capsys):
    cursor = home / ".cursor" / "mcp.json"
    _write(cursor, {"mcpServers": {"blender": {"command": "uvx", "args": ["mcp-for-blender"]}}})
    monkeypatch.setattr(setup_cli, "find_uvx", lambda: UVX)
    monkeypatch.setattr(setup_cli, "detect_clients", lambda: [Client("cursor", "Cursor", "json", cursor)])

    assert setup_cli.run_setup(assume_yes=True, skip_addon=True) == 0
    assert json.loads(cursor.read_text())["mcpServers"] == {"blender": {"command": UVX, "args": FORK_ARGS}}
    out = capsys.readouterr().out
    assert "will update (runs mcp-for-blender from PyPI)" in out
    assert 'Updated "blender"' in out


def test_run_setup_needs_uvx(monkeypatch, capsys):
    monkeypatch.setattr(setup_cli, "find_uvx", lambda: None)
    assert setup_cli.run_setup(assume_yes=True) == 1
    assert "Install uv" in capsys.readouterr().out


@pytest.mark.parametrize("message, oem", [
    ("INFO: Es werden keine Aufgaben mit den angegebenen Kriterien ausgeführt.", "cp850"),  # German
    ("BİLGİ: Belirtilen ölçütlerle eşleşen çalışan görev yok.", "cp857"),  # Turkish
])
def test_blender_is_running_survives_localized_tasklist(monkeypatch, message, oem):
    # Emulate Windows: tasklist writes the OEM code page, and subprocess decodes with the
    # encoding it was given, else the ANSI code page (cp1252 here), which cannot read byte 0x81.
    def fake_run(args, capture_output, text, timeout, encoding=None, errors="strict"):
        codec = oem if encoding == "oem" else (encoding or "cp1252")
        stdout = message.encode(oem).decode(codec, errors)
        return setup_cli.subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr(setup_cli.subprocess, "run", fake_run)
    assert setup_cli.blender_is_running("win32") is False
