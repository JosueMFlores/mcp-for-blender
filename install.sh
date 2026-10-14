#!/bin/sh
# MCP for Blender installer for macOS and Linux (JosueMFlores/mcp-for-blender fork).
#
#   curl -LsSf https://raw.githubusercontent.com/JosueMFlores/mcp-for-blender/main/install.sh | sh
#
# Installs uv (with its official installer) if it's missing, then runs
# `uvx --from git+https://github.com/JosueMFlores/mcp-for-blender mcp-for-blender setup`,
# which configures your MCP clients and the Blender addon. Arguments are
# passed to setup:
#
#   curl -LsSf .../install.sh | sh -s -- --dry-run
#
# BLENDER_MCP_SOURCE picks what to install: unset for this fork, "pypi" for
# the upstream package on PyPI, or any other uvx --from spec (a git URL, a
# local checkout).
set -eu

SOURCE="${BLENDER_MCP_SOURCE:-git+https://github.com/JosueMFlores/mcp-for-blender}"
case "$SOURCE" in
    [Pp][Yy][Pp][Ii]) set -- mcp-for-blender@latest setup "$@" ;;
    *) set -- --from "$SOURCE" mcp-for-blender setup "$@" ;;
esac

find_uvx() {
    if command -v uvx >/dev/null 2>&1; then
        command -v uvx
        return 0
    fi
    # Where uv's installer puts it, before a new shell has it on PATH.
    for dir in "${UV_INSTALL_DIR:-}" "${XDG_BIN_HOME:-}" "$HOME/.local/bin" "$HOME/.cargo/bin"; do
        if [ -n "$dir" ] && [ -x "$dir/uvx" ]; then
            echo "$dir/uvx"
            return 0
        fi
    done
    return 1
}

echo "MCP for Blender installer"

# uv clones git sources with git.
case "$SOURCE" in
    git+*)
        if ! command -v git >/dev/null 2>&1; then
            echo "MCP for Blender installs from GitHub, which needs git. Install git" >&2
            echo "(https://git-scm.com/downloads; on macOS: xcode-select --install) and run this again." >&2
            exit 1
        fi
        ;;
esac

if ! UVX=$(find_uvx); then
    echo "Installing uv, which runs MCP for Blender (https://docs.astral.sh/uv/)..."
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- https://astral.sh/uv/install.sh | sh
    else
        echo "Neither curl nor wget is available; install uv by hand: https://docs.astral.sh/uv/getting-started/installation/" >&2
        exit 1
    fi
    if ! UVX=$(find_uvx); then
        echo "uv installed, but uvx wasn't found. Open a new terminal and run: uvx $*" >&2
        exit 1
    fi
fi

# Keep uvx's folder on PATH for setup, which records uvx's absolute path.
PATH="$(dirname "$UVX"):$PATH"
export PATH

# uvx shows nothing while it downloads the package, which can take a while.
echo "Downloading MCP for Blender. The first run can take a minute..."

# --refresh-package: fetch the newest commit (or PyPI release) now, rather
# than a copy uv cached on an earlier run.
# Piped into sh, this script is stdin, so setup reads its answers from the
# terminal instead. With no terminal at all, it configures everything it finds.
if (: </dev/tty) 2>/dev/null; then
    exec "$UVX" --refresh-package mcp-for-blender "$@" </dev/tty
else
    exec "$UVX" --refresh-package mcp-for-blender "$@" --yes
fi
