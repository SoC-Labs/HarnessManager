<#
.SYNOPSIS
Install Harness Manager for this user (Windows).

.DESCRIPTION
Makes a private venv in %LOCALAPPDATA%\harness-manager\venv, installs Harness
Manager and pyverify into it, and puts one command, harness-manager, in
%LOCALAPPDATA%\harness-manager\bin, which it adds to your user PATH. Re-running
it upgrades in place. Nothing needs Administrator.

It runs in Windows PowerShell 5.1 and in PowerShell 7. It uses uv when uv is on
PATH, else a Python 3.10 or newer (the py launcher, python or python3).

Environment: HARNESS_MANAGER_HOME (install root) and HARNESS_MANAGER_BIN_DIR
(where the command goes) override the defaults.

.PARAMETER From
What to install: a checkout (default: the one holding this script), a wheel
file, or a git URL (cloned, depth 1).

.PARAMETER Ref
The branch or tag to clone, with a git URL.

.PARAMETER WithApp
Also install the 'app' extra: pywebview, for a native window (WebView2).

.PARAMETER WithSerial
Also install the 'serial' extra: pyserial, for the Debug USB serial ports (the
MCC and the FPGA UARTs).

.PARAMETER Python
The Python to build the venv with (3.10 or newer).

.PARAMETER NoUv
Use venv and pip even when uv is on PATH.

.PARAMETER Force
Replace an existing harness-manager command in a custom HARNESS_MANAGER_BIN_DIR.

.PARAMETER Uninstall
Stop the service, remove the venv and the command. Your settings and backups in
%USERPROFILE%\.config\harness-manager stay.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -WithSerial
#>
[CmdletBinding()]
param(
    [string]$From = "",
    [string]$Ref = "",
    [switch]$WithApp,
    [switch]$WithSerial,
    [string]$Python = "",
    [switch]$NoUv,
    [switch]$Force,
    [switch]$Uninstall
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$MinPy = [version]'3.10'
$OnWindows = [System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT
$Checkout = Split-Path -Parent $PSScriptRoot

if ($env:HARNESS_MANAGER_HOME) {
    $Root = $env:HARNESS_MANAGER_HOME
} elseif ($env:LOCALAPPDATA) {
    $Root = Join-Path $env:LOCALAPPDATA 'harness-manager'
} else {
    # PowerShell on Linux or macOS (used to test this script); scripts/install.sh is the
    # installer there.
    $Root = Join-Path (Join-Path (Join-Path $HOME '.local') 'share') 'harness-manager'
}
$DefaultBin = Join-Path $Root 'bin'
if ($env:HARNESS_MANAGER_BIN_DIR) { $BinDir = $env:HARNESS_MANAGER_BIN_DIR } else { $BinDir = $DefaultBin }
$Venv = Join-Path $Root 'venv'
if ($env:HARNESS_MANAGER_STATE_DIR) {
    $StateDir = $env:HARNESS_MANAGER_STATE_DIR
} else {
    $StateDir = Join-Path (Join-Path $HOME '.config') 'harness-manager'
}
if ($OnWindows) {
    $VenvPy = Join-Path (Join-Path $Venv 'Scripts') 'python.exe'
    $VenvHm = Join-Path (Join-Path $Venv 'Scripts') 'harness-manager.exe'
    $Command = Join-Path $BinDir 'harness-manager.exe'
} else {
    $VenvPy = Join-Path (Join-Path $Venv 'bin') 'python'
    $VenvHm = Join-Path (Join-Path $Venv 'bin') 'harness-manager'
    $Command = Join-Path $BinDir 'harness-manager'
}

function Say([string]$Text) { Write-Host $Text }
function Note([string]$Text) { Write-Host "install.ps1: $Text" -ForegroundColor Yellow }
function Fail([string]$Text) {
    Write-Host "install.ps1: error: $Text" -ForegroundColor Red
    exit 1
}

# Run a native command. Windows PowerShell 5.1 turns a native command's stderr into
# error records, which ErrorActionPreference=Stop makes fatal, so relax it for the call.
# -Capture returns stdout in Output and stderr, without PowerShell's decoration, in Error.
function Invoke-Native {
    param([string]$Exe, [string[]]$Arguments, [switch]$Capture)
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        if ($Capture) {
            $all = @(& $Exe @Arguments 2>&1)
            $code = $LASTEXITCODE
            $out = ($all | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] } |
                    ForEach-Object { "$_" }) -join "`n"
            $err = ($all | Where-Object { $_ -is [System.Management.Automation.ErrorRecord] } |
                    ForEach-Object { $_.ToString() }) -join "`n"
        } else {
            & $Exe @Arguments | Out-Host
            $code = $LASTEXITCODE
            $out = ''
            $err = ''
        }
        return @{ Code = $code; Output = $out; Error = $err }
    } finally {
        $ErrorActionPreference = $old
    }
}

function Test-Python([string]$Exe, [string[]]$Pre = @()) {
    # No double quotes in the code: Windows PowerShell 5.1 does not escape them for native commands.
    $code = 'import sys; v = sys.version_info; print(str(v[0]) + chr(46) + str(v[1])); print(sys.executable)'
    $r = Invoke-Native $Exe ($Pre + @('-c', $code)) -Capture
    if ($r.Code -ne 0) { return $null }
    $lines = @($r.Output -split "`r?`n" | Where-Object { $_ -ne '' })
    if ($lines.Count -lt 2) { return $null }
    try { $v = [version]$lines[0] } catch { return $null }
    if ($v -lt $MinPy) { return $null }
    return $lines[1]
}

function Find-Python([string]$Wanted) {
    if ($Wanted) {
        $exe = Test-Python $Wanted
        if (-not $exe) { Fail "$Wanted is not Python $MinPy or newer" }
        return $exe
    }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($minor in @('3.13', '3.12', '3.11', '3.10')) {
            $exe = Test-Python 'py' @("-$minor")
            if ($exe) { return $exe }
        }
    }
    foreach ($name in @('python', 'python3', 'python3.13', 'python3.12', 'python3.11', 'python3.10')) {
        if (Get-Command $name -ErrorAction SilentlyContinue) {
            $exe = Test-Python $name
            if ($exe) { return $exe }
        }
    }
    return $null
}

# Stop the Harness Manager service (and the demo one) before the venv changes under it.
function Stop-HarnessService {
    if (-not (Test-Path $VenvHm)) { return }
    foreach ($flag in @('', '--demo')) {
        $stopArgs = @('daemon', 'stop')
        if ($flag) { $stopArgs += $flag }
        $r = Invoke-Native $VenvHm $stopArgs -Capture
        if ($r.Code -ne 0 -and $r.Code -ne 8) {   # 8 = ALREADY: it was not running
            Write-Host $r.Error
            Fail ("the Harness Manager service did not stop (harness-manager daemon stop " +
                  "$flag exited $($r.Code)). If a job is running, wait for it; " +
                  "or stop it with --force. Then run this again.")
        }
    }
}

function Get-UserPath {
    $p = [Environment]::GetEnvironmentVariable('Path', 'User')
    if ($null -eq $p) { return '' }
    return $p
}

function Test-OnPath([string]$PathList, [string]$Dir) {
    $want = $Dir.TrimEnd('\', '/')
    foreach ($item in ($PathList -split ';')) {
        if ($item.TrimEnd('\', '/') -ieq $want) { return $true }
    }
    return $false
}

if ($Uninstall) {
    Stop-HarnessService
    if (Test-Path $Command) {
        if ($BinDir -eq $DefaultBin -or $Force) {
            Remove-Item -Force $Command
            Say "removed  $Command"
        } else {
            Note "left $Command alone (a custom HARNESS_MANAGER_BIN_DIR; -Force removes it)"
        }
    }
    if (Test-Path $Venv) { Remove-Item -Recurse -Force $Venv; Say "removed  $Venv" }
    if ($OnWindows -and $BinDir -eq $DefaultBin) {
        $userPath = Get-UserPath
        if (Test-OnPath $userPath $BinDir) {
            $kept = @($userPath -split ';' | Where-Object { $_ -and ($_.TrimEnd('\', '/') -ine $BinDir.TrimEnd('\', '/')) })
            [Environment]::SetEnvironmentVariable('Path', ($kept -join ';'), 'User')
            Say "removed  $BinDir from your user PATH"
        }
    }
    foreach ($dir in @($DefaultBin, $Root)) {
        if ((Test-Path $dir) -and -not (Get-ChildItem -Force $dir)) { Remove-Item -Force $dir }
    }
    Say "kept     $StateDir (settings, SD backups, the content store)."
    Say "         Delete it yourself if you want it gone."
    Say "Harness Manager is uninstalled."
    exit 0
}

# -- what to install --------------------------------------------------------------------
$Work = Join-Path ([System.IO.Path]::GetTempPath()) ("harness-manager-install-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $Work | Out-Null
try {
    $links = @()
    if (-not $From) { $From = $Checkout }
    $isGit = ($From -match '^(https?|ssh|git|file)://') -or ($From -match '^[^@/\\]+@[^:]+:') -or
             (($From -match '\.git$') -and -not (Test-Path $From -PathType Container))
    if ($isGit) {
        if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Fail "git is needed to install from $From" }
        $what = $From
        if ($Ref) { $what = "$From@$Ref" }
        Say "cloning  $what"
        $cloneArgs = @('clone', '--quiet', '--depth', '1')
        if ($Ref) { $cloneArgs += @('--branch', $Ref) }
        $pkg = Join-Path $Work 'src'
        $r = Invoke-Native 'git' ($cloneArgs + @($From, $pkg))
        if ($r.Code -ne 0) { Fail "could not clone $From (for the private repo, check your GitHub SSH key)" }
        $links += (Join-Path $pkg 'vendor')
    } elseif (Test-Path $From -PathType Container) {
        $what = (Resolve-Path $From).Path
        $pyproject = Join-Path $what 'pyproject.toml'
        if (-not ((Test-Path $pyproject) -and (Select-String -Quiet -Pattern '^name = "harness-manager"' -Path $pyproject))) {
            Fail "$what is not a Harness Manager checkout (no pyproject.toml naming harness-manager)"
        }
        # Build from a clean copy, so pip writes nothing into the checkout.
        $pkg = Join-Path $Work 'src'
        New-Item -ItemType Directory -Path $pkg | Out-Null
        $skip = @('.git', '.venv', 'build', 'dist', 'tests', '.pytest_cache', '.ruff_cache')
        Get-ChildItem -Force $what | Where-Object { $skip -notcontains $_.Name } |
            ForEach-Object { Copy-Item -Recurse -Force $_.FullName $pkg }
        $links += (Join-Path $pkg 'vendor')
    } elseif ((Test-Path $From -PathType Leaf) -and ($From -like '*.whl')) {
        $pkg = (Resolve-Path $From).Path
        $what = $pkg
        $links += (Split-Path -Parent $pkg)
        $links += (Join-Path $Checkout 'vendor')
    } else {
        Fail "-From $From is not a checkout, a wheel file or a git URL"
    }
    $links = @($links | Where-Object { Test-Path $_ -PathType Container })

    $pyverifyWheel = $null
    foreach ($dir in $links) {
        $found = @(Get-ChildItem -Path $dir -Filter 'mps3_pyverify-*.whl' -ErrorAction SilentlyContinue)
        if ($found.Count -gt 0) { $pyverifyWheel = $found[-1].FullName; break }
    }
    if (-not $pyverifyWheel) { Fail "no pyverify wheel (vendor\mps3_pyverify-*.whl) next to $what" }

    $extras = @()
    if ($WithApp) { $extras += 'app' }
    if ($WithSerial) { $extras += 'serial' }
    $spec = $pkg
    if ($extras.Count -gt 0) { $spec = "$pkg[" + ($extras -join ',') + "]" }
    $findLinks = @()
    foreach ($dir in $links) { $findLinks += @('--find-links', $dir) }

    # -- the venv -----------------------------------------------------------------------
    $uv = $null
    if (-not $NoUv) {
        $cmd = Get-Command uv -ErrorAction SilentlyContinue
        if ($cmd) { $uv = $cmd.Path }
    }
    if ((Test-Path $VenvPy) -and (Test-Python $VenvPy)) {
        Stop-HarnessService
        Say "upgrade  $Venv"
    } else {
        if (Test-Path $Venv) { Remove-Item -Recurse -Force $Venv }
        New-Item -ItemType Directory -Force -Path $Root | Out-Null
        # The newest local Python >= 3.10 (or -Python).
        $base = Find-Python $Python
        if ($uv) {
            # With no local Python >= 3.10, uv downloads one.
            $want = '3.12'
            if ($base) { $want = $base }
            $r = Invoke-Native $uv @('venv', '--quiet', '--python', $want, $Venv)
            if ($r.Code -ne 0) { Fail "uv could not make a venv with Python >= $MinPy" }
        } else {
            if (-not $base) {
                Fail ("Harness Manager needs Python $MinPy or newer, and none was found. " +
                      "Install one from python.org (tick 'Add python.exe to PATH') or with " +
                      "'winget install Python.Python.3.12', or install uv, or name it with -Python.")
            }
            $r = Invoke-Native $base @('-m', 'venv', $Venv)
            if ($r.Code -ne 0) { Fail "$base could not make a venv" }
        }
        $pv = Invoke-Native $VenvPy @('-c', 'import platform; print(platform.python_version())') -Capture
        Say "venv     $Venv ($($pv.Output.Trim()))"
    }

    # -- install ------------------------------------------------------------------------
    $shown = $what
    if ($extras.Count -gt 0) { $shown = "$what [" + ($extras -join ' ') + "]" }
    Say "install  $shown"
    if ($uv) {
        $pipBase = @('pip', 'install', '--quiet', '--python', $VenvPy)
        # pyverify keeps its version number across commits: always reinstall the vendored one.
        $r = Invoke-Native $uv ($pipBase + @('--reinstall-package', 'mps3-pyverify', '--no-deps', $pyverifyWheel))
        if ($r.Code -ne 0) { Fail "could not install $pyverifyWheel" }
        # --upgrade-package, not --upgrade: an upgrade of everything could swap the vendored
        # pyverify for a same-named package from the index.
        $r = Invoke-Native $uv ($pipBase + @('--upgrade-package', 'harness-manager', '--reinstall-package', 'harness-manager') + $findLinks + @($spec))
        if ($r.Code -ne 0) { Fail "could not install Harness Manager (uv exited $($r.Code))" }
    } else {
        $pipBase = @('-m', 'pip', '--disable-pip-version-check', 'install', '--quiet')
        $r = Invoke-Native $VenvPy @('-m', 'pip', '--version') -Capture
        if ($r.Code -ne 0) { Invoke-Native $VenvPy @('-m', 'ensurepip', '--upgrade') -Capture | Out-Null }
        $r = Invoke-Native $VenvPy ($pipBase + @('--upgrade', 'pip'))
        if ($r.Code -ne 0) { Note "could not upgrade pip; carrying on" }
        $r = Invoke-Native $VenvPy ($pipBase + @('--force-reinstall', '--no-deps', $pyverifyWheel))
        if ($r.Code -ne 0) { Fail "could not install $pyverifyWheel" }
        $r = Invoke-Native $VenvPy ($pipBase + @('--upgrade') + $findLinks + @($spec))
        if ($r.Code -ne 0) { Fail "could not install Harness Manager (pip exited $($r.Code))" }
        if ($pkg -like '*.whl') {
            # pip leaves a wheel of the same version alone; the file may still be newer.
            $r = Invoke-Native $VenvPy ($pipBase + @('--force-reinstall', '--no-deps', $pkg))
            if ($r.Code -ne 0) { Fail "could not reinstall $pkg" }
        }
    }
    $r = Invoke-Native $VenvHm @('version') -Capture
    if ($r.Code -ne 0) { Write-Host $r.Error; Fail "the installed harness-manager does not run" }
    $version = $r.Output.Trim()

    # -- the command on PATH ------------------------------------------------------------
    # A copy of the venv's launcher: it holds the venv's absolute Python path, so it
    # runs from anywhere (pipx does the same on Windows).
    New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
    if ((Test-Path $Command) -and $BinDir -ne $DefaultBin -and -not $Force) {
        $same = (Get-FileHash $Command).Hash -eq (Get-FileHash $VenvHm).Hash
        if (-not $same) {
            Fail "$Command exists and this script did not write it. Move it away, or re-run with -Force."
        }
    }
    try {
        Copy-Item -Force $VenvHm $Command
    } catch {
        Fail "could not write $Command ($($_.Exception.Message)). Close any running harness-manager, then run this again."
    }
    if ($OnWindows) {
        $userPath = Get-UserPath
        if (-not (Test-OnPath $userPath $BinDir)) {
            $newPath = $BinDir
            if ($userPath) { $newPath = "$userPath;$BinDir" }
            [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
            Say "PATH     added $BinDir to your user PATH (open a new terminal to use it)"
        }
        if (-not (Test-OnPath $env:Path $BinDir)) { $env:Path = "$env:Path;$BinDir" }
    }

    Say "command  $Command"
    Say ""
    Say "Harness Manager $version is installed."
    Say ""
    Say "Next:"
    Say "  harness-manager app --demo      the app with demo boards, no hardware needed"
    Say "  harness-manager ui --demo       the same in a browser tab"
    Say "  harness-manager info 192.168.10.101    a real board on your network"
    Say "Guide: docs\USER_GUIDE.md in the Harness Manager repo"
} finally {
    Remove-Item -Recurse -Force $Work -ErrorAction SilentlyContinue
}
