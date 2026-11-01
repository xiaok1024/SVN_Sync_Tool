[CmdletBinding()]
param(
    [int]$Port = 8765,
    [switch]$Lan,
    [switch]$Reload,
    [string]$Config = ""
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
$RunDir = Join-Path $Root "run"
$StatePath = Join-Path $RunDir "web-process.json"
$Python = Join-Path $Root ".venv-web\Scripts\python.exe"

function Get-WebListener {
    param([int]$ListenPort)

    return Get-NetTCPConnection -State Listen -LocalPort $ListenPort -ErrorAction SilentlyContinue |
        Select-Object -First 1
}

function Test-WebProcess {
    param([int]$ProcessId)

    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
    return [bool]($process -and $process.CommandLine -match "svn_sync_web\.py")
}

if (Test-Path -LiteralPath $StatePath) {
    try {
        $oldState = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
        $oldPort = if ($oldState.port) { [int]$oldState.port } else { $Port }
        $oldListener = Get-WebListener -ListenPort $oldPort
        if ($oldListener -and (Test-WebProcess -ProcessId ([int]$oldListener.OwningProcess))) {
            Write-Host "SVN Sync Tool Web is already running."
            Write-Host "Port: $oldPort"
            Write-Host "PID:  $($oldListener.OwningProcess)"
            Write-Host "Stop it with: .\stop-web.bat"
            exit 0
        }
        Remove-Item -LiteralPath $StatePath -Force -ErrorAction SilentlyContinue
    }
    catch {
        Remove-Item -LiteralPath $StatePath -Force -ErrorAction SilentlyContinue
    }
}

$listener = Get-WebListener -ListenPort $Port
if ($listener) {
    if (Test-WebProcess -ProcessId ([int]$listener.OwningProcess)) {
        Write-Host "SVN Sync Tool Web is already running on port $Port (PID $($listener.OwningProcess))."
        exit 0
    }
    throw "Port $Port is already occupied by PID $($listener.OwningProcess)."
}

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Python environment not found: $Python. Create .venv-web and install requirements-web.txt first."
}

if ($Config) {
    $Config = (Resolve-Path -LiteralPath $Config).Path
    $env:SVN_SYNC_WEB_SOURCE_CONFIG = $Config
}

New-Item -ItemType Directory -Force -Path $RunDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$stdoutPath = Join-Path $RunDir "web-$stamp.out.log"
$stderrPath = Join-Path $RunDir "web-$stamp.err.log"

$arguments = @("svn_sync_web.py", "--port", $Port.ToString())
if ($Lan) {
    $arguments += "--lan"
}
if ($Reload) {
    $arguments += "--reload"
}

$process = Start-Process -FilePath $Python `
    -ArgumentList $arguments `
    -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdoutPath `
    -RedirectStandardError $stderrPath `
    -PassThru

$deadline = (Get-Date).AddSeconds(15)
$listener = $null
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 300
    $process.Refresh()
    if ($process.HasExited) {
        break
    }
    $listener = Get-WebListener -ListenPort $Port
    if ($listener) {
        break
    }
}

if (-not $listener) {
    if (-not $process.HasExited) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    }
    Write-Host "Startup failed. Recent output:"
    if (Test-Path -LiteralPath $stdoutPath) {
        Get-Content -LiteralPath $stdoutPath -Tail 40
    }
    if (Test-Path -LiteralPath $stderrPath) {
        Get-Content -LiteralPath $stderrPath -Tail 40
    }
    throw "SVN Sync Tool Web did not listen on port $Port within 15 seconds."
}

$processIds = @($process.Id, [int]$listener.OwningProcess) |
    Sort-Object -Unique

$state = [ordered]@{
    port = $Port
    pids = $processIds
    launched_pid = $process.Id
    listener_pid = [int]$listener.OwningProcess
    started_at = (Get-Date).ToString("o")
    stdout = $stdoutPath
    stderr = $stderrPath
}
$state | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $StatePath -Encoding ascii

Write-Host "SVN Sync Tool Web started."
Write-Host "URL:  http://127.0.0.1:$Port/"
Write-Host "PID:  $($listener.OwningProcess)"
Write-Host "Logs: $RunDir"
