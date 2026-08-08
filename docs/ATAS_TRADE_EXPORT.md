# ATAS Trade Export

`AtasTradeExport` is a standalone ATAS indicator. It writes two compact CSV datasets:

- Footprint price-level summaries.
- Level1 trades.

## Build And Install

```powershell
cd \\wsl.localhost\Ubuntu-22.04\home\yux\github\atastomt5\atas_trade_export
$env:ATAS_HOME = "D:\ATAS Platform"
dotnet build -c Release
copy .\bin\Release\netstandard2.1\AtasTradeExportIndicator.dll "$env:APPDATA\ATAS\Indicators\AtasTradeExportIndicator.dll" -Force
```

Restart ATAS, then add:

```text
Custom -> AtasTradeExport
```

## Remove

```powershell
Remove-Item "$env:APPDATA\ATAS\Indicators\AtasTradeExportIndicator.dll" -Force
```

Restart ATAS.

## Parameters

- `Instrument override`: stable label used in file names.
- `File prefix`: file-name prefix.
- `Footprint directory`: default `%USERPROFILE%\Documents\ATASExports\Footprint`.
- `Level1 directory`: default `%USERPROFILE%\Documents\ATASExports\Level1`.
- `Export footprint`: writes loaded chart footprint price levels. Default `false`.
- `Export online Level1 trades`: writes real-time `OnNewTrade` prints.
- `Start sync`: starts historical synchronization when set to `true`.
- `Use explicit date range`: uses `Start date` and `End date` instead of loaded chart range. Default `true`.
- `Start date yyyy-MM-dd`: inclusive start date. Also accepts `yyyy-MM-ddTHH:mm:ss`.
- `End date yyyy-MM-dd`: exclusive end date when time is provided; if only a date is provided, the whole day is included.
- `Skip existing daily files`: skips a date if its Level1 daily CSV already exists and has data.
- `Request historical Level1 trades`: requests historical cumulative trades with no volume filter, then writes child ticks if ATAS provides them.
- `Max historical bars, 0 = all`: time range for footprint export and historical Level1 request.
- `Historical Level1 chunk hours`: request size for historical Level1. Default `24`. Smaller chunks are slower but avoid provider/API limits on large single requests.
- `Write fallback if historical ticks missing`: writes aggregate fallback rows to a separate CSV when ATAS doesn't expose child ticks.
- `Debug log enabled`: writes lifecycle and historical request diagnostics to the Level1 directory.
- `Status file enabled`: writes sync progress rows to the Level1 directory.

## Level1 CSV

Path:

```text
%USERPROFILE%\Documents\ATASExports\Level1\atas_export_GC_YYYYMMDD.csv
```

Columns:

```csv
time,index,price,volume,side,parent_id
```

Example:

```csv
2026-06-07T12:00:00.1234567Z,1,4490.1,10,Buy,42
```

`parent_id` groups ticks that ATAS reports as belonging to the same cumulative trade / market-order sweep. Online raw trades do not have a parent and leave this column empty.

Daily files are skipped when `Skip existing daily files = true`. Delete a daily CSV if you want to re-fetch that date.

## Level1 Fallback CSV

Path:

```text
%USERPROFILE%\Documents\ATASExports\Level1\atas_export_GC_level1_fallback_YYYYMMDD.csv
```

Columns:

```csv
time,index,first_price,last_price,volume,side,parent_id,tick_count
```

Fallback rows mean ATAS returned historical cumulative trades but did not expose the child ticks through this API path. Do not treat fallback rows as complete Level1 tick data.

## Footprint CSV

Path:

```text
%USERPROFILE%\Documents\ATASExports\Footprint\atas_export_GC_YYYYMMDD_HHMMSS_mmm_footprint.csv
```

Columns:

```csv
time,bar_index,price,bid,ask,between,volume,ticks
```

## Incremental Loading

ATAS may load historical data in chunks. The indicator tracks the earliest and latest loaded candle times it has already requested.

When ATAS later expands the loaded range, the indicator automatically requests only the missing leading or trailing time range for historical Level1 trades.

Historical Level1 requests are split into chunks. This matters because ATAS/data-provider APIs may silently cap a large request and return only the newest part of the requested range.

Debug, status, and footprint files include a session id in the filename. Level1 trade files are named by date so repeated syncs can skip existing days.

## Suggested Explicit Sync Setup

For a historical Level1 export:

```text
Export footprint = false
Export online Level1 trades = false
Request historical Level1 trades = true
Use explicit date range = true
Start date yyyy-MM-dd = 2026-04-01
End date yyyy-MM-dd = 2026-04-17
Historical Level1 chunk hours = 24
Skip existing daily files = true
Start sync = true
```

The standard ATAS parameter panel doesn't expose a true push button here, so `Start sync` acts as the trigger. Re-add the indicator or toggle `Start sync` from `false` to `true` to start a new run.

## Troubleshooting

Debug log path:

```text
%USERPROFILE%\Documents\ATASExports\Level1\atas_export_GC_YYYYMMDD_HHMMSS_mmm_debug.log
```

Status file path:

```text
%USERPROFILE%\Documents\ATASExports\Level1\atas_export_GC_YYYYMMDD_HHMMSS_mmm_status.csv
```

Status columns:

```csv
time,phase,done,progress_percent,queued_ranges,in_flight,requests_started,requests_completed,level1_rows,historical_ticks,historical_fallbacks,write_errors,requested_start,requested_end
```

The export is finished when the last status row has:

```csv
done=true,queued_ranges=0,in_flight=false
```

`progress_percent` can be used by an external UI to draw a progress bar.

PowerShell:

```powershell
Get-Content "$env:USERPROFILE\Documents\ATASExports\Level1\*_status.csv" -Tail 5
```

Error log path:

```text
%USERPROFILE%\Documents\ATASExports\Level1\atas_export_GC_YYYYMMDD_HHMMSS_mmm_errors.log
```

Do not open an actively written daily CSV in Excel. Excel can lock the file exclusively. Use PowerShell `Get-Content`, a text editor that does not lock files, or copy the CSV first.

Useful checks:

1. Confirm ATAS loaded the new DLL by finding a newly created debug log after adding the indicator.
2. Check `OnFinishRecalculate` lines. They show `CurrentBar`, `fromBar`, `start`, and `end`.
3. Check `Queued historical Level1 ranges` and `RequestForCumulativeTrades` lines. If these are missing, the indicator did not request historical Level1 data.
4. Check `OnCumulativeTradesResponse` lines:
   - `cumulativeTrades=0` means ATAS returned no cumulative trades for that requested range.
   - `ticks=0` with `fallbacks>0` means ATAS returned aggregate cumulative trades but not child ticks.
   - `ticks>0` means the main Level1 CSV should have rows.
