# ATAS Strategy

ATAS integration is done through a C# Chart Strategy DLL.

The skeleton project is in:

```text
atas_strategy/
```

The strategy class is:

```text
AtasMt5BridgeStrategy.AtasPositionBridgeStrategy
```

ATAS should show it as:

```text
Custom -> AtasMt5PositionBridge
```

## What it currently does

- Runs inside ATAS.
- Listens to `OnPositionChanged(Position position)`.
- Emits a JSON message to the Local Bridge WebSocket server.
- Does not place MT5 orders.
- Does not copy ATAS pending orders yet.

Message shape:

```json
{
  "type": "atas_position",
  "event": {
    "seq": 1,
    "source_symbol": "COMEX Gold August 2026",
    "target_net_contracts": 1,
    "avg_price": 2400.5,
    "timestamp": "2026-06-03T10:00:00.0000000+00:00"
  }
}
```

## Build on Windows

ATAS SDK DLLs are installed with ATAS. Set `ATAS_HOME` to that folder before building.

```powershell
cd atas_strategy
$env:ATAS_HOME = "C:\Program Files (x86)\ATAS Platform"
dotnet build -c Release
```

If your ATAS is installed elsewhere, adjust `ATAS_HOME`.

## Install into ATAS

Copy:

```text
atas_strategy\bin\Release\netstandard2.1\AtasMt5BridgeStrategy.dll
```

to:

```text
%APPDATA%\ATAS\Strategies\
```

Restart ATAS, open a chart, then add:

```text
Custom -> AtasMt5PositionBridge
```

## Run the dry-run bridge server

From the repo:

```bash
PYTHONPATH=src python -m atas_mt5_bridge.bridge.server \
  --config config.example.json \
  --host 0.0.0.0 \
  --port 8766
```

Set the ATAS strategy parameter:

```text
Bridge WebSocket URL = ws://127.0.0.1:8766
```

If ATAS on Windows cannot reach the WSL server by localhost, use WSL's IP:

```bash
hostname -I
```

Then set:

```text
Bridge WebSocket URL = ws://<WSL_IP>:8766
```
