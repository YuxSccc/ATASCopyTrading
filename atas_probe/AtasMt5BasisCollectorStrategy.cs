using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using ATAS.Indicators;
using ATAS.Strategies.Chart;

namespace AtasMt5ProbeStrategy
{
    /// <summary>
    /// Exports a synthetic closed GC M1 series for in-memory basis
    /// calibration. It aggregates ATAS' recent trade cache, so it can run on
    /// the existing trading chart regardless of that chart's candle period.
    /// No trading API is used.
    /// </summary>
    [Category("Custom")]
    [DisplayName("AtasMt5BasisCollector")]
    public sealed class AtasMt5BasisCollectorStrategy : ChartStrategy
    {
        private ProbeWebSocketClient? _client;
        private ITradesCache? _trades;
        private string _sessionId = "";
        private long _eventSequence;
        private long _lastExportTicks;
        private readonly object _outboxLock = new object();
        private Task _outboxTail = Task.CompletedTask;
        private bool _disposed;

        [OFT.Attributes.ParameterAttribute]
        [DisplayName("Basis collector WebSocket URL")]
        public string ServerUrl { get; set; } = "ws://127.0.0.1:8767";

        [OFT.Attributes.ParameterAttribute]
        [DisplayName("Source symbol override")]
        public string SourceSymbolOverride { get; set; } = "GC";

        [OFT.Attributes.ParameterAttribute]
        [DisplayName("ATAS chart UTC offset minutes")]
        public int AtasChartUtcOffsetMinutes { get; set; } = 480;

        [OFT.Attributes.ParameterAttribute]
        [DisplayName("Closed M1 minutes to export")]
        public int HistoryBars { get; set; } = 240;

        [OFT.Attributes.ParameterAttribute]
        [DisplayName("History export interval seconds")]
        public int ExportIntervalSeconds { get; set; } = 60;

        protected override void OnStarted()
        {
            _disposed = false;
            _sessionId = Guid.NewGuid().ToString("N");
            _eventSequence = 0;
            _lastExportTicks = 0;
            _client = new ProbeWebSocketClient(ServerUrl);
            _trades = GetTradesCache(TimeSpan.FromMinutes(Math.Max(1, HistoryBars)));
            Emit("basis_collector_ready", new
            {
                chart_type = ChartInfo?.ChartType ?? "",
                time_frame = ChartInfo?.TimeFrame ?? "",
                collector_mode = "trades_cache",
                requested_bars = Math.Max(1, HistoryBars),
                atas_chart_utc_offset_minutes = AtasChartUtcOffsetMinutes,
            });
            StrategyLog.Append($"Basis collector started session={_sessionId}, source={GetSourceSymbol()}\n");
        }

        protected override void OnCalculate(int bar, decimal value)
        {
            if (_disposed || !CanExport())
                return;
            EmitHistory();
        }

        protected override void OnSuspended() => StopCollector();

        protected override void OnStopped() => StopCollector();

        private bool CanExport()
        {
            var now = Stopwatch.GetTimestamp();
            var intervalTicks = Stopwatch.Frequency * Math.Max(1, ExportIntervalSeconds);
            var previous = Interlocked.Read(ref _lastExportTicks);
            if (previous != 0 && now - previous < intervalTicks)
                return false;
            Interlocked.Exchange(ref _lastExportTicks, now);
            return true;
        }

        private void EmitHistory()
        {
            // CachedItems is obtained and enumerated on ATAS' chart thread.
            // The cache contains recent raw trades independently of whether
            // this chart itself is M1, H1, or another candle period.
            var nowUtcMinute = DateTimeOffset.UtcNow.UtcDateTime
                .AddSeconds(-DateTimeOffset.UtcNow.UtcDateTime.Second)
                .AddMilliseconds(-DateTimeOffset.UtcNow.UtcDateTime.Millisecond);
            var minuteCloses = new SortedDictionary<DateTime, decimal>();
            var tradeCount = 0;
            foreach (var trade in _trades?.CachedItems ?? Enumerable.Empty<MarketDataArg>())
            {
                var localTime = DateTime.SpecifyKind(trade.Time, DateTimeKind.Unspecified);
                var utcTime = localTime.AddMinutes(-AtasChartUtcOffsetMinutes);
                var minute = new DateTime(utcTime.Year, utcTime.Month, utcTime.Day, utcTime.Hour, utcTime.Minute, 0, DateTimeKind.Utc);
                if (minute >= nowUtcMinute)
                    continue;
                minuteCloses[minute] = trade.Price;
                tradeCount++;
            }
            var bars = new List<object>();
            foreach (var item in minuteCloses.TakeLast(Math.Max(1, HistoryBars)))
            {
                bars.Add(new
                {
                    time_utc = new DateTimeOffset(item.Key, TimeSpan.Zero).ToString("O"),
                    close = item.Value,
                });
            }
            Emit("basis_history", new
            {
                chart_type = ChartInfo?.ChartType ?? "",
                time_frame = ChartInfo?.TimeFrame ?? "",
                collector_mode = "trades_cache",
                trade_count = tradeCount,
                bars,
            });
        }

        private void Emit(string eventType, object data)
        {
            if (_disposed)
                return;
            var message = new ProbeEnvelope
            {
                SessionId = _sessionId,
                EventId = Interlocked.Increment(ref _eventSequence),
                EventType = eventType,
                SourceSymbol = GetSourceSymbol(),
                AtasTimestampUtc = DateTimeOffset.UtcNow.ToString("O"),
                LocalMonotonicTicks = Stopwatch.GetTimestamp(),
                LocalStopwatchFrequency = Stopwatch.Frequency,
                DeliveryMode = "async",
                Data = data,
            };
            lock (_outboxLock)
            {
                _outboxTail = _outboxTail
                    .ContinueWith(_ => SendAsync(message), CancellationToken.None, TaskContinuationOptions.None, TaskScheduler.Default)
                    .Unwrap();
            }
        }

        private async Task SendAsync(ProbeEnvelope message)
        {
            try
            {
                if (_client != null)
                    await _client.SendAsync(message, false, 1000);
            }
            catch (Exception error)
            {
                StrategyLog.Append($"Basis collector send error: {error.Message}\n");
            }
        }

        private string GetSourceSymbol() => string.IsNullOrWhiteSpace(SourceSymbolOverride)
            ? Security?.ToString() ?? "GC"
            : SourceSymbolOverride.Trim();

        private void StopCollector()
        {
            if (_disposed)
                return;
            _disposed = true;
            _client?.Dispose();
            _client = null;
            StrategyLog.Append("Basis collector stopped\n");
        }
    }
}
