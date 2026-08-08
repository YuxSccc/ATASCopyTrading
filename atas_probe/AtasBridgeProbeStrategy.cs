using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using ATAS.DataFeedsCore;
using ATAS.Indicators;
using ATAS.Strategies.Chart;

namespace AtasMt5ProbeStrategy
{
    /// <summary>
    /// Read-only ATAS integration probe. It intentionally does not submit, modify,
    /// or cancel trading orders. Its purpose is to document the callbacks and data
    /// available from the installed ATAS version before copy logic is introduced.
    /// </summary>
    [Category("Custom")]
    [DisplayName("AtasMt5BridgeProbe")]
    public sealed class AtasBridgeProbeStrategy : ChartStrategy
    {
        private const string BuildTag = "2026-08-08-probe-v1";
        private ProbeWebSocketClient? _client;
        private string _sessionId = "";
        private long _eventSequence;
        private long _lastQuoteTicks;
        private decimal? _lastCapturedPositionVolume;
        private decimal? _lastCapturedAveragePrice;
        private object? _lastObservedPositionData;
        private bool _disposed;

        [OFT.Attributes.ParameterAttribute]
        [DisplayName("Probe WebSocket URL")]
        public string ServerUrl { get; set; } = "ws://127.0.0.1:8767";

        [OFT.Attributes.ParameterAttribute]
        [DisplayName("Source symbol override")]
        public string SourceSymbolOverride { get; set; } = "";

        [OFT.Attributes.ParameterAttribute]
        [DisplayName("Delivery mode: async or sync")]
        public string DeliveryMode { get; set; } = "async";

        [OFT.Attributes.ParameterAttribute]
        [DisplayName("Synchronous acknowledgement timeout ms")]
        public int AcknowledgementTimeoutMilliseconds { get; set; } = 1000;

        [OFT.Attributes.ParameterAttribute]
        [DisplayName("Capture quotes")]
        public bool CaptureQuotes { get; set; } = true;

        [OFT.Attributes.ParameterAttribute]
        [DisplayName("Minimum quote interval ms")]
        public int QuoteMinimumIntervalMilliseconds { get; set; } = 50;

        [OFT.Attributes.ParameterAttribute]
        [DisplayName("Capture unchanged position callbacks")]
        public bool CaptureUnchangedPositions { get; set; } = true;

        protected override void OnStarted()
        {
            _disposed = false;
            _sessionId = Guid.NewGuid().ToString("N");
            _eventSequence = 0;
            _lastQuoteTicks = 0;
            _lastCapturedPositionVolume = null;
            _lastCapturedAveragePrice = null;
            _lastObservedPositionData = null;
            _client = new ProbeWebSocketClient(ServerUrl);
            StrategyLog.Append(
                $"Started bridge probe build={BuildTag}, session={_sessionId}, source={GetSourceSymbol()}, " +
                $"server={ServerUrl}, delivery={NormalizedDeliveryMode()}\n");
            _ = SendStartupSnapshotAsync();
        }

        protected override void OnPositionChanged(Position position)
        {
            if (_disposed || !IsCurrentChartSecurity(position.Security?.ToString()))
                return;

            if (!CaptureUnchangedPositions
                && _lastCapturedPositionVolume == position.Volume
                && _lastCapturedAveragePrice == position.AveragePrice)
                return;

            _lastCapturedPositionVolume = position.Volume;
            _lastCapturedAveragePrice = position.AveragePrice;
            _lastObservedPositionData = BuildPositionData(position);
            Emit("position_changed", _lastObservedPositionData);
        }

        protected override void OnNewOrder(Order order)
        {
            if (_disposed || !IsCurrentChartSecurity(order.Security?.ToString()))
                return;

            Emit("order_new", BuildOrderData(order));
        }

        protected override void OnOrderChanged(Order order)
        {
            if (_disposed || !IsCurrentChartSecurity(order.Security?.ToString()))
                return;

            Emit("order_changed", BuildOrderData(order));
        }

        protected override void OnBestBidAskChanged(MarketDataArg depth)
        {
            base.OnBestBidAskChanged(depth);

            if (_disposed || !CaptureQuotes || !CanEmitQuote())
                return;

            Emit("quote", new
            {
                update_type = depth.DataType.ToString(),
                update_price = depth.Price,
                update_volume = depth.Volume,
                update_timestamp = depth.Time.ToUniversalTime().ToString("O"),
                best_bid = BestBid == null ? (decimal?)null : BestBid.Price,
                best_ask = BestAsk == null ? (decimal?)null : BestAsk.Price,
            });
        }

        protected override void OnCalculate(int bar, decimal value)
        {
        }

        protected override void OnSuspended() => StopProbe();

        protected override void OnStopped() => StopProbe();

        private async Task SendStartupSnapshotAsync()
        {
            try
            {
                if (_client == null || !await _client.EnsureConnectedAsync())
                {
                    StrategyLog.Append("Probe server is unavailable; startup snapshot was not sent\n");
                    return;
                }

                Emit("lifecycle_started", new
                {
                    build = BuildTag,
                    chart_symbol = Security?.ToString() ?? "",
                    capture_quotes = CaptureQuotes,
                    quote_minimum_interval_ms = Math.Max(0, QuoteMinimumIntervalMilliseconds),
                    capture_unchanged_positions = CaptureUnchangedPositions,
                });

                var orders = Orders
                    .Where(order => IsCurrentChartSecurity(order.Security?.ToString()))
                    .Select(BuildOrderData)
                    .ToArray();
                Emit("state_snapshot", new
                {
                    last_observed_position = _lastObservedPositionData,
                    orders,
                });
            }
            catch (Exception ex)
            {
                StrategyLog.Append($"Probe startup snapshot error: {ex.Message}\n");
            }
        }

        private bool CanEmitQuote()
        {
            var now = Stopwatch.GetTimestamp();
            var minimumTicks = Stopwatch.Frequency * Math.Max(0, QuoteMinimumIntervalMilliseconds) / 1000;
            var previous = Interlocked.Read(ref _lastQuoteTicks);
            if (minimumTicks > 0 && now - previous < minimumTicks)
                return false;

            Interlocked.Exchange(ref _lastQuoteTicks, now);
            return true;
        }

        private void Emit(string eventType, object data)
        {
            var message = new ProbeEnvelope
            {
                SessionId = _sessionId,
                EventId = Interlocked.Increment(ref _eventSequence),
                EventType = eventType,
                SourceSymbol = GetSourceSymbol(),
                AtasTimestampUtc = DateTimeOffset.UtcNow.ToString("O"),
                LocalMonotonicTicks = Stopwatch.GetTimestamp(),
                LocalStopwatchFrequency = Stopwatch.Frequency,
                DeliveryMode = NormalizedDeliveryMode(),
                Data = data,
            };
            _ = SendEventAsync(message);
        }

        private async Task SendEventAsync(ProbeEnvelope message)
        {
            try
            {
                if (_client == null)
                    return;

                var sentAt = Stopwatch.GetTimestamp();
                var acknowledgement = await _client.SendAsync(
                    message,
                    message.DeliveryMode == "sync",
                    AcknowledgementTimeoutMilliseconds);
                if (message.DeliveryMode == "sync")
                {
                    var elapsedMilliseconds = (Stopwatch.GetTimestamp() - sentAt) * 1000.0 / Stopwatch.Frequency;
                    StrategyLog.Append(
                        acknowledgement == null
                            ? $"Probe sync acknowledgement missing event={message.EventId}, elapsed_ms={elapsedMilliseconds:F3}\n"
                            : $"Probe sync acknowledgement event={message.EventId}, elapsed_ms={elapsedMilliseconds:F3}, " +
                              $"server_received={acknowledgement.ServerReceivedUtc}\n");
                }
            }
            catch (Exception ex)
            {
                StrategyLog.Append($"Probe event send error event={message.EventId}: {ex.Message}\n");
            }
        }

        private object BuildPositionData(Position position)
        {
            return new
            {
                security = position.Security?.ToString() ?? "",
                volume = position.Volume,
                average_price = position.AveragePrice,
                in_position = position.IsInPosition,
            };
        }

        private object BuildOrderData(Order order)
        {
            return new
            {
                id = order.Id ?? "",
                ext_id = order.ExtId,
                user_ext_id = order.UserExtId,
                security = order.Security?.ToString() ?? "",
                direction = order.Direction.ToString(),
                order_type = order.Type.ToString(),
                state = order.State.ToString(),
                price = order.Price,
                trigger_price = order.TriggerPrice,
                trigger_price_type = order.TriggerPriceType.ToString(),
                quantity_to_fill = order.QuantityToFill,
                unfilled = order.Unfilled,
                is_attached = order.IsAttached,
                is_in_position = order.IsInPosition,
                oco_group = order.OCOGroup ?? "",
                auto_cancel = order.AutoCancel,
                canceled = order.Canceled,
                time_utc = order.Time.ToUniversalTime().ToString("O"),
                order_latency_ms = Math.Round(order.Latency.TotalMilliseconds, 3),
                comment = order.Comment ?? "",
            };
        }

        private string GetSourceSymbol()
        {
            return string.IsNullOrWhiteSpace(SourceSymbolOverride)
                ? Security?.ToString() ?? ""
                : SourceSymbolOverride.Trim();
        }

        private bool IsCurrentChartSecurity(string? securityId)
        {
            return string.Equals(
                securityId?.Trim(),
                Security?.ToString()?.Trim(),
                StringComparison.OrdinalIgnoreCase);
        }

        private string NormalizedDeliveryMode()
        {
            return string.Equals(DeliveryMode?.Trim(), "sync", StringComparison.OrdinalIgnoreCase)
                ? "sync"
                : "async";
        }

        private void StopProbe()
        {
            if (_disposed)
                return;

            _disposed = true;
            _client?.Dispose();
            _client = null;
            StrategyLog.Append("Bridge probe stopped\n");
        }
    }
}
