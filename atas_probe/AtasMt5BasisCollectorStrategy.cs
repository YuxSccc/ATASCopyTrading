using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.Threading;
using System.Threading.Tasks;
using ATAS.Indicators;
using ATAS.Strategies.Chart;

namespace AtasMt5ProbeStrategy
{
    /// <summary>
    /// Exports closed GC M1 candles for the bridge's in-memory basis
    /// calibration. Add this strategy to a dedicated M1 chart; the trading
    /// chart itself may remain on any timeframe. No trading API is used.
    /// </summary>
    [Category("Custom")]
    [DisplayName("AtasMt5BasisCollector")]
    public sealed class AtasMt5BasisCollectorStrategy : ChartStrategy
    {
        private ProbeWebSocketClient? _client;
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
        [DisplayName("Closed M1 bars to export")]
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
            Emit("basis_collector_ready", new
            {
                chart_type = ChartInfo?.ChartType ?? "",
                time_frame = ChartInfo?.TimeFrame ?? "",
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
            // GetCandle is an ATAS chart-thread API. It must not be called by
            // the background WebSocket sender.
            var endExclusive = Math.Max(0, CurrentBar - 1);
            var start = Math.Max(0, endExclusive - Math.Max(1, HistoryBars));
            var bars = new List<object>();
            for (var bar = start; bar < endExclusive; bar++)
            {
                var candle = GetCandle(bar);
                var localTime = DateTime.SpecifyKind(candle.Time, DateTimeKind.Unspecified);
                var utcTime = localTime.AddMinutes(-AtasChartUtcOffsetMinutes);
                bars.Add(new
                {
                    chart_time = localTime.ToString("O"),
                    time_utc = new DateTimeOffset(utcTime, TimeSpan.Zero).ToString("O"),
                    close = candle.Close,
                });
            }
            Emit("basis_history", new
            {
                chart_type = ChartInfo?.ChartType ?? "",
                time_frame = ChartInfo?.TimeFrame ?? "",
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
