# Integration Checklist

## ATAS checks

Use ATAS simulated account first.

1. Build the C# strategy DLL on Windows, where ATAS is installed:

```powershell
cd atas_strategy
$env:ATAS_HOME = "C:\Program Files (x86)\ATAS Platform"
dotnet build -c Release
```

2. Install the DLL into:

```text
%APPDATA%\ATAS\Strategies\
```

The DLL path after build should be similar to:

```text
atas_strategy\bin\Release\netstandard2.1\AtasMt5BridgeStrategy.dll
```

3. Restart ATAS.
4. Open the GC chart and add `Custom -> AtasMt5PositionBridge`.
5. Set `Bridge WebSocket URL`.

If the bridge server runs inside WSL, first try:

```text
ws://127.0.0.1:8766
```

If Windows cannot reach WSL localhost forwarding, run this in WSL:

```bash
hostname -I
```

Then use the first WSL IP:

```text
ws://<WSL_IP>:8766
```

6. Place a simulated market buy of 1 contract.
7. Record the event payload logged by the strategy.

Required fields to confirm:

- actual `Security.ToString()` for GC
- actual `Security.ToString()` for NQ
- whether `Position.Volume` is signed net contracts
- whether `OnPositionChanged` fires after market orders
- whether `OnPositionChanged` fires after limit fills
- whether ATAS strategy API exposes working orders before fill
- whether ATAS strategy API exposes SL/TP or bracket orders

## MT5 checks

Use a demo account first.

Do not commit MT5 login/password to this repository. Use a private local config file or environment variables when live execution is wired.

Important environment note:

- WSL can run the dry-run bridge server and receive ATAS events.
- Python `MetaTrader5` usually needs to run on Windows next to the MT5 terminal.
- For real MT5 execution, plan to run the bridge under Windows Python, or implement an MT5-side EA/WebSocket execution adapter later.

Recommended Windows setup:

1. Open MT5 on Windows.
2. Log in to the intended demo account inside the MT5 terminal.
3. Confirm the terminal shows connected quotes for the target symbols.
4. Install Python packages in Windows PowerShell:

```powershell
py -m pip install MetaTrader5 websockets pytest
```

5. From Windows PowerShell, run the read-only MT5 check from this repo:

```powershell
cd \\wsl.localhost\Ubuntu-22.04\home\yux\github\atastomt5
$env:PYTHONPATH = "src"
py -m atas_mt5_bridge.mt5.check --symbols XAUUSD USTEC --magic 880501
```

This command must not send orders. It only initializes the Python MetaTrader5 package,
reads terminal/account info, reads symbol properties, and reads current positions that
match the bridge Magic Number.

1. Confirm the real broker symbols:

```text
XAUUSD, XAUUSD.cn, USTEC, NAS100, or broker-specific names
```

2. Record symbol properties:

- `volume_min`
- `volume_step`
- `volume_max`
- `trade_mode`
- `filling_mode`
- `digits`
- `point`
- `trade_stops_level`
- `trade_contract_size`

3. Confirm account type:

- netting
- hedging

4. Confirm Python `MetaTrader5` can:

- initialize against the intended terminal
- read account info
- read positions
- read symbol info
- attach Magic Number and comment

## First live integration path

1. Keep `mode = dry_run`.
2. Connect ATAS strategy to Local Bridge.
3. Confirm ATAS events arrive.
4. Confirm bridge planned orders match expectations.
5. Run the bridge on Windows Python in `dry_run` mode and point ATAS to that Windows server:

```powershell
cd \\wsl.localhost\Ubuntu-22.04\home\yux\github\atastomt5
$env:PYTHONPATH = "src"
py -m atas_mt5_bridge.bridge.server --config config.example.json --host 127.0.0.1 --port 8766
```

6. Confirm ATAS events still arrive while the server is running under Windows Python.
7. Create a private local config file, keep tiny lot sizing, and switch only that file to `"mode": "live"`.
8. Start live mode against the MT5 demo terminal:

```powershell
py -m atas_mt5_bridge.bridge.server --config config.local.live.json --host 127.0.0.1 --port 8766
```

9. Test:

```text
ATAS GC +1 -> MT5 XAUUSD +0.05
ATAS GC 0  -> MT5 XAUUSD 0
ATAS GC -1 -> MT5 XAUUSD -0.05
```

10. Test duplicate event replay does not duplicate order.
11. Test bridge restart does not replay old events.
12. Test kill switch.
13. Test flatten-only.
