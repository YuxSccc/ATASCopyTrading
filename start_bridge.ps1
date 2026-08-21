param(
    # The normal shortcut starts the currently configured demo/live bridge.
    # Use -ObserveOnly when you want to start the transport without allowing
    # the Python coordinator to enqueue trading commands.
    [switch]$ObserveOnly,

    # The test-control HTTP endpoint is harmless unless an ATAS test driver is
    # explicitly enabled and a test run is requested. Keep it on by default so
    # the hot test API remains available after a restart.
    [switch]$DisableTestControl
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $root

function Test-ListeningPort {
    param([int]$Port)

    if (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue) {
        # The service binds loopback, but Windows may report the listener as
        # 0.0.0.0/:: depending on the Python socket implementation. Matching
        # only LocalAddress=127.0.0.1 would miss an already-running service.
        $connections = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
        if ($connections.Count -gt 0) {
            return $true
        }
    }
    $line = netstat.exe -ano -p tcp | Select-String (":$Port\s+.*LISTENING") | Select-Object -First 1
    return $null -ne $line
}

# Avoid accidentally starting a second coordinator against the same SQLite
# state database and HTTP ports. Stop the existing process explicitly before
# starting this script again.
foreach ($port in @(8767, 8768)) {
    if (Test-ListeningPort -Port $port) {
        Write-Host "ATAS/MT5 bridge is already listening on 127.0.0.1:$port." -ForegroundColor Yellow
        Write-Host "Stop the existing bridge process, then run this script again." -ForegroundColor Yellow
        exit 0
    }
}

$pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
if ($null -eq $pythonCommand) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
}
if ($null -eq $pythonCommand) {
    throw "Python was not found on PATH. Install Python 3.10+ or activate the project environment first."
}
$python = $pythonCommand.Source

$env:PYTHONPATH = Join-Path $root "src"
$env:PYTHONUNBUFFERED = "1"

# These are the current mappings requested for the two live pairs:
#   1 GC -> 0.05 XAUUSD
#   1 NQ -> 0.5 USTEC
$arguments = @(
    "-m", "atas_mt5_bridge.v2.live_service",
    "--pair", "gc_xauusd=GC:XAUUSD:26081001:0.05",
    "--pair", "nq_ustec=NQ:USTEC:26081001:0.5",
    "--state-db", "bridge-state-demo\bridge.db",
    "--journal", "bridge-state-demo\operations.jsonl",
    "--output-dir", "probe-data-demo",
    "--websocket-port", "8767",
    "--mt5-http-port", "8768",
    "--basis-window-seconds", "14400",
    "--basis-half-life-seconds", "900",
    "--basis-sample-interval-seconds", "60",
    "--basis-minimum-samples", "170",
    "--quote-max-age-ms", "3000",
    "--max-current-basis-deviation", "2.00",
    "--max-basis-dispersion", "1.00"
)

if (-not $ObserveOnly) {
    $arguments += "--enable-execution"
}
if (-not $DisableTestControl) {
    $arguments += @("--enable-test-control", "--test-http-port", "8769")
}

$mode = if ($ObserveOnly) { "OBSERVE-ONLY" } else { "EXECUTION ENABLED" }
Write-Host "Starting ATAS/MT5 bridge ($mode)..." -ForegroundColor Cyan
Write-Host "Root: $root"
Write-Host "Pairs: GC -> XAUUSD (0.05), NQ -> USTEC (0.5)"
Write-Host "HTTP: 8768   WebSocket: 8767   Test API: $(if ($DisableTestControl) { 'disabled' } else { '8769' })"
Write-Host "Press Ctrl+C to stop the Python service. Existing MT5 positions are not closed by stopping this process."

& $python @arguments
exit $LASTEXITCODE
