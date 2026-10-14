# MCP for Blender installer for Windows (JosueMFlores/mcp-for-blender fork).
#
#   powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/JosueMFlores/mcp-for-blender/main/install.ps1 | iex"
#
# Installs uv (with its official installer) if it's missing, then runs
# `uvx --from git+https://github.com/JosueMFlores/mcp-for-blender mcp-for-blender setup`,
# which configures your MCP clients and the Blender addon.
#
# BLENDER_MCP_SOURCE picks what to install: unset for this fork, "pypi" for
# the upstream package on PyPI, or any other uvx --from spec (a git URL, a
# local checkout).

# Wrapped in a function so `irm | iex` leaves nothing behind in the session,
# and errors return instead of closing the user's PowerShell window.
function Install-McpForBlender {
    $ErrorActionPreference = 'Stop'

    function Find-Uvx {
        $cmd = Get-Command uvx -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
        # Where uv's installer puts it, before a new shell has it on PATH.
        foreach ($dir in @($env:UV_INSTALL_DIR, $env:XDG_BIN_HOME, "$env:USERPROFILE\.local\bin", "$env:USERPROFILE\.cargo\bin")) {
            if ($dir -and (Test-Path (Join-Path $dir 'uvx.exe'))) { return (Join-Path $dir 'uvx.exe') }
        }
        return $null
    }

    Write-Host 'MCP for Blender installer'

    $source = if ($env:BLENDER_MCP_SOURCE) { $env:BLENDER_MCP_SOURCE } else { 'git+https://github.com/JosueMFlores/mcp-for-blender' }
    if ($source -eq 'pypi') {
        $package = @('mcp-for-blender@latest')
    } else {
        $package = @('--from', $source, 'mcp-for-blender')
    }
    # uv clones git sources with git.
    if ($source -like 'git+*' -and -not (Get-Command git -ErrorAction SilentlyContinue)) {
        Write-Host 'MCP for Blender installs from GitHub, which needs git. Install it (https://git-scm.com/download/win, or: winget install Git.Git) and run this again.' -ForegroundColor Red
        return
    }

    $uvx = Find-Uvx
    if (-not $uvx) {
        Write-Host 'Installing uv, which runs MCP for Blender (https://docs.astral.sh/uv/)...'
        powershell -ExecutionPolicy ByPass -c 'irm https://astral.sh/uv/install.ps1 | iex'
        $uvx = Find-Uvx
        if (-not $uvx) {
            Write-Host "uv installed, but uvx was not found. Open a new terminal and run: uvx $($package -join ' ') setup" -ForegroundColor Red
            return
        }
    }

    # Keep uvx's folder on PATH for setup, which records uvx's absolute path.
    $env:Path = "$(Split-Path $uvx);$env:Path"
    # uvx shows nothing while it downloads the package, which can take a while.
    Write-Host 'Downloading MCP for Blender. The first run can take a minute...'
    # --refresh-package: fetch the newest commit (or PyPI release) now, rather
    # than a copy uv cached on an earlier run.
    & $uvx --refresh-package mcp-for-blender @package setup @args
}

Install-McpForBlender @args
