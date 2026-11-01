[CmdletBinding()]
param(
    [int]$Port = 0
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
$RunDir = Join-Path $Root "run"
$StatePath = Join-Path $RunDir "web-process.json"

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

$state = $null
if (Test-Path -LiteralPath $StatePath) {
    try {
        $state = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
    }
    catch {
        Remove-Item -LiteralPath $StatePath -Force -ErrorAction SilentlyContinue
    }
}

if ($Port -le 0) {
    if ($state -and $state.port) {
        $Port = [int]$state.port
    }
    else {
        $Port = 8765
    }
}

$processIds = @()
if ($state -and $state.pids) {
    $processIds += @($state.pids | ForEach-Object { [int]$_ })
}
$listener = Get-WebListener -ListenPort $Port
if ($listener) {
    $processIds += [int]$listener.OwningProcess
}
$processIds = @($processIds | Sort-Object -Unique)

if ($processIds.Count -eq 0) {
    if (Test-Path -LiteralPath $StatePath) {
        Remove-Item -LiteralPath $StatePath -Force -ErrorAction SilentlyContinue
    }
    Write-Host "SVN Sync Tool Web is not running on port $Port."
    exit 0
}

$stopped = @()
foreach ($processId in $processIds) {
    $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
    if (-not $process) {
        continue
    }
    if (-not (Test-WebProcess -ProcessId $processId)) {
        if (Get-Process -Id $processId -ErrorAction SilentlyContinue) {
            Write-Warning "Skip PID $processId because it is not running svn_sync_web.py."
        }
        continue
    }
    Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    $stopped += $processId
}

Start-Sleep -Seconds 1
$listener = Get-WebListener -ListenPort $Port
if ($listener) {
    if (Test-WebProcess -ProcessId ([int]$listener.OwningProcess)) {
        Stop-Process -Id ([int]$listener.OwningProcess) -Force -ErrorAction SilentlyContinue
        $stopped += [int]$listener.OwningProcess
        Start-Sleep -Seconds 1
        $listener = Get-WebListener -ListenPort $Port
    }
    if ($listener) {
        throw "Port $Port is still in use. Refusing to stop PID $($listener.OwningProcess) because it may not belong to this project."
    }
}

if (Test-Path -LiteralPath $StatePath) {
    Remove-Item -LiteralPath $StatePath -Force -ErrorAction SilentlyContinue
}

$stopped = @($stopped | Sort-Object -Unique)
Write-Host "SVN Sync Tool Web stopped on port $Port."
if ($stopped.Count -gt 0) {
    Write-Host "Stopped PIDs: $($stopped -join ', ')"
}
