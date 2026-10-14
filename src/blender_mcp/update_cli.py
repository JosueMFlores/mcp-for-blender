"""
`mcp-for-blender update`: bring an existing install's server and addon up to date.

MCP clients launch the server through uvx, which keeps running whatever
version uv cached (or installed with `uv tool install`). So this upgrades any
uv tool install and re-runs itself through uvx with --refresh-package. That
refreshes uv's cache for the clients' next launch, and the re-run installs the
addon bundled with the new server rather than the one we are running.

By default the server comes from this fork's git repo, which has no release
number to compare against, so it always refreshes:
`uvx --refresh-package mcp-for-blender --from <repo> mcp-for-blender update`.
The re-run then moves client entries, which setup pinned to a commit, to the
one the repo is on now.

With BLENDER_MCP_SOURCE=pypi it hands over to upstream's own update, as
`uvx --refresh-package mcp-for-blender mcp-for-blender@latest update`, which
installs upstream's addon; this fork shares upstream's version numbers, so
comparing them would say nothing.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .addon_manager import addon_version_label, update_installed_addons
from .distribution import (
    PACKAGE,
    describe_source,
    fork_command,
    pin_source,
    pip_command,
    server_source,
    split_rev,
    uvx_args,
)
from .setup_cli import (
    OUTDATED,
    STALE,
    blender_is_running,
    client_state,
    configure_client,
    describe_change,
    detect_clients,
    find_uvx,
)

PYPI_URL = f"https://pypi.org/pypi/{PACKAGE}/json"
# Set on the re-run to the version we upgraded from; also stops a re-run loop.
REEXEC_ENV = "BLENDERMCP_UPDATED_FROM"


def version_key(version: str) -> tuple[int, ...]:
    """Comparable key for a release like 2.1.6; pre-release suffixes are ignored."""
    match = re.match(r"\d+(?:\.\d+)*", version.strip())
    return tuple(int(p) for p in match.group(0).split(".")) if match else ()


def current_version() -> str | None:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(PACKAGE)
    except PackageNotFoundError:
        return None


def fetch_latest_version(timeout: float = 10.0) -> tuple[str | None, str]:
    """(latest version on PyPI, error message if it couldn't be read)."""
    import httpx

    # server.py configures INFO logging at import; keep the request line out of the output.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        response = httpx.get(PYPI_URL, timeout=timeout, follow_redirects=True)
        response.raise_for_status()
        return response.json()["info"]["version"], ""
    except Exception as e:
        return None, str(e) or type(e).__name__


def find_uv(uvx: str | None) -> str | None:
    """uv itself, preferring the one installed beside the uvx we found."""
    if uvx:
        sibling = Path(uvx).with_name("uv.exe" if sys.platform == "win32" else "uv")
        if sibling.is_file():
            return str(sibling)
    return shutil.which("uv")


def uv_tool_installed(uv: str) -> bool:
    """True when `uv tool install mcp-for-blender` was used; uvx then prefers that copy."""
    try:
        proc = subprocess.run([uv, "tool", "list"], capture_output=True, text=True, errors="replace", timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return any(line.split()[:1] == [PACKAGE] for line in (proc.stdout or "").splitlines())


def _update_server(uvx: str, run_args: list[str], dry_run: bool) -> int:
    """Refresh the server, then re-run update from it with uvx `run_args` (what
    names the package, then `update`). Returns the re-run's exit code."""
    uv = find_uv(uvx)
    if uv and uv_tool_installed(uv):
        if dry_run:
            print(f"  would run: uv tool upgrade {PACKAGE}")
        else:
            proc = subprocess.run([uv, "tool", "upgrade", PACKAGE])
            if proc.returncode != 0:
                print(f"  `uv tool upgrade {PACKAGE}` failed; see the output above.")
                return 1

    command = [uvx, "--refresh-package", PACKAGE, *run_args]
    if dry_run:
        command.append("--dry-run")
    current = current_version() or "unknown"
    try:
        proc = subprocess.run(command, env={**os.environ, REEXEC_ENV: current})
    except OSError as e:
        print(f"  Couldn't run uvx: {e}")
        return 1
    return proc.returncode


def _update_addon(dry_run: bool) -> tuple[bool, bool]:
    """Print what happened to each installed addon. Returns (any updated, any failed)."""
    print("Blender addon:")
    try:
        results = update_installed_addons(dry_run=dry_run)
    except FileNotFoundError as e:
        print(f"  {e}")
        return False, True
    if not results:
        print(f"  Not installed. Run `{fork_command('setup')}` to install and enable it.")
        return False, False

    verb = "would update" if dry_run else "updated"
    for r in results:
        if r.action == "updated":
            print(f"  ✓ {verb} {r.path} (was {addon_version_label(r.installed)})")
        elif r.action == "current":
            print(f"  {r.path}: already up to date")
        elif r.action == "newer":
            print(f"  {r.path}: {addon_version_label(r.installed)} is newer than this release, left alone")
        else:
            print(f"  ✗ {r.path}: {r.detail}")
    updated = any(r.action == "updated" for r in results)
    if updated and not dry_run:
        print("  (the previous file is kept beside each one as .bak)")
    return updated, any(r.action == "failed" for r in results)


def _update_clients(source: str | None, dry_run: bool) -> tuple[str | None, bool, bool]:
    """Move client entries that run `source` at another commit to the one it
    is on now: setup pinned them, so they'd never update otherwise. Returns
    (the `--from` entries should run, any updated, any failed)."""
    clients = detect_clients()
    if not clients:
        return source, False, False
    target, error = pin_source(source)
    if error:
        print(f"MCP clients: couldn't look up the latest commit ({error}); pinned ones stay on theirs.")
        print()
        return target, False, False
    stale = []
    for client in clients:
        try:
            state = client_state(client, target)
        except Exception:
            continue
        if state.status == STALE:
            stale.append((client, state))
    if not stale:
        return target, False, False

    print(f"MCP clients (to commit {split_rev(target)[1][:12]}):")
    uvx = find_uvx()
    if not uvx:
        print(f"  uvx not found, so they were left alone. Run `{fork_command('setup')}` once it is installed.")
        print()
        return target, False, False
    updated = failed = False
    for client, state in stale:
        if dry_run:
            print(f"  {client.name}: {describe_change(client, uvx, state, target)}")
            continue
        result = configure_client(client, uvx, state, target)
        print(f"  {'✓' if result.ok else '✗'} {client.name}: {result.message}")
        updated, failed = updated or result.ok, failed or not result.ok
    print()
    return target, updated, failed


def _outdated_clients(source: str | None, target: str | None = None) -> list[str]:
    """Lines about the clients still set up to run the pre-rename blender-mcp
    package, or this one from a different source, and how to switch them."""
    names: list[str] = []
    manual: list[str] = []
    for client in detect_clients():
        try:
            state = client_state(client, target)
        except Exception:
            continue
        if state.manual:
            manual += [f"{client.name}: {state.detail}.", f"  {state.manual}"]
        elif state.status == OUTDATED:
            names.append(f"{client.name} ({state.detail})" if state.detail else client.name)
    if not names:
        return manual
    return [f"Not running the server from {describe_source(source)} yet: {', '.join(names)}.",
            f"  Run `{fork_command('setup')}` to switch them over.", *manual]


def run_update(dry_run: bool = False) -> int:
    updated_from = os.environ.get(REEXEC_ENV)
    current = current_version()
    source = server_source()

    if updated_from is None:
        print("MCP for Blender update" + (" (dry run: nothing will be changed)" if dry_run else ""))
        print(f"Server source: {describe_source(source)}")
        print()

    server_updated = False
    pip_hint = None
    if updated_from is not None:
        if source is not None and updated_from == current:
            # Commits on the repo don't always bump the version.
            server_updated = True
            print(f"Server: {'would refresh' if dry_run else 'refreshed'} {current} from {source}")
        else:
            server_updated = updated_from != current
            verb = "would update" if dry_run else "updated"
            print(f"Server: {verb} {updated_from} → {current}")
    elif source is not None:
        # A git or local source has no release number to compare: always refresh.
        print(f"Server: {current or 'unknown version'}, refreshing from {source}")
        uvx = find_uvx()
        if uvx:
            return _update_server(uvx, uvx_args("update"), dry_run)
        # Re-running update wouldn't find uvx either, so update the addon now
        # and leave the server to pip.
        pip_hint = pip_command(source)
        print(f"  uvx not found. If you installed with pip, run: {pip_hint}")
    else:
        uvx = find_uvx()
        if uvx:
            # PyPI has upstream's package: its own update knows its releases
            # and installs its addon, where we'd install this fork's.
            print(f"Server: {current or 'unknown version'}, handing over to the latest release on PyPI")
            code = _update_server(uvx, [f"{PACKAGE}@latest", "update"], dry_run)
            # Upstream's update can't tell which clients still run this fork.
            lines = _outdated_clients(source)
            if lines:
                print()
                print("\n".join(lines))
            return code
        latest, error = fetch_latest_version()
        if latest is None:
            print(f"Server: {current or 'unknown version'} (couldn't check PyPI for a newer one: {error})")
        elif current and version_key(current) >= version_key(latest):
            print(f"Server: {current}, the latest release")
        else:
            print(f"Server: {current or 'unknown version'} → {latest}")
            print(f"  uvx not found. If you installed with pip, run: {pip_command(None)}")
            print(f"  then run `{PACKAGE} update` again to update the addon.")
            return 1
    print()

    addon_updated, addon_failed = _update_addon(dry_run)
    print()

    target, clients_updated, clients_failed = source, False, False
    if updated_from is not None:
        target, clients_updated, clients_failed = _update_clients(source, dry_run)

    lines = _outdated_clients(source, target)
    if lines:
        print("\n".join(lines))
        print()

    if dry_run:
        return 0

    print("Next:")
    if pip_hint:
        print(f"  • Update the server with `{pip_hint}`, then restart your MCP clients.")
    if server_updated or clients_updated:
        print("  • Restart your MCP clients (Claude Desktop, Cursor, ...) so they start the new server.")
    if addon_updated:
        if blender_is_running():
            print("  • Blender is open: restart it, or Preferences → Add-ons → disable and re-enable")
            print("    'Interface: MCP for Blender', to load the new addon.")
        else:
            print("  • Open Blender. The new addon loads on launch.")
    if not server_updated and not clients_updated and not addon_updated and not pip_hint:
        print("  • Nothing to do: everything is up to date.")
    return 1 if addon_failed or clients_failed else 0
