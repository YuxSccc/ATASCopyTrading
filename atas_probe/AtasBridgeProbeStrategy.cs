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
        private const string BuildTag = "2026-08-12-probe-v2";
        private ProbeWebSocketClient? _client;
        private string _sessionId = "";
        private long _eventSequence;
        private readonly object _outboxLock = new object();
        private Task _outboxTail = Task.CompletedTask;
        private int _outboxBacklog;
        private long _droppedQuoteEvents;
        private long _latestCriticalEventId;
        private long _lastQuoteTicks;
        private long _lastRecoverySnapshotTicks;
        private decimal? _lastCapturedPositionVolume;
        private decimal? _lastCapturedAveragePrice;
        private object? _lastObservedPositionData;
        // ChartStrategy.Orders contains orders submitted by the strategy
        // itself, but does not reliably contain manually placed chart orders.
        // The order callbacks do see those orders, so recovery snapshots must
        // be built from callback-derived state rather than Orders.
        private readonly object _observedOrdersLock = new object();
        private readonly Dictionary<string, ObservedOrder> _observedOrders = new Dictionary<string, ObservedOrder>();
        private bool _disposed;

        private sealed class ObservedOrder
        {
            public ObservedOrder(object data)
            {
                Data = data;
            }

            public object Data { get; }
        }

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

        [OFT.Attributes.ParameterAttribute]
        [DisplayName("Recovery snapshot interval ms")]
        public int RecoverySnapshotIntervalMilliseconds { get; set; } = 1000;

        [OFT.Attributes.ParameterAttribute]
        [DisplayName("Maximum queued quote events")]
        public int MaximumQueuedQuoteEvents { get; set; } = 20;

        protected override void OnStarted()
        {
            _disposed = false;
            _sessionId = Guid.NewGuid().ToString("N");
            _eventSequence = 0;
            _lastQuoteTicks = 0;
            _lastRecoverySnapshotTicks = 0;
            _lastCapturedPositionVolume = null;
            _lastCapturedAveragePrice = null;
            _lastObservedPositionData = null;
            lock (_observedOrdersLock)
                _observedOrders.Clear();
            _outboxBacklog = 0;
            _droppedQuoteEvents = 0;
            _latestCriticalEventId = 0;
            _client = new ProbeWebSocketClient(ServerUrl);

            // ATAS does not guarantee an immediate OnPositionChanged callback
            // when a chart strategy is attached to an already-flat chart.  A
            // null position snapshot is deliberately treated as unsafe by the
            // bridge, so seed the snapshot from the synchronous strategy value.
            // Average price is unavailable for this startup read and is not
            // required for authoritative net-position reconciliation.
            var currentPosition = CurrentPosition;
            _lastCapturedPositionVolume = currentPosition;
            _lastObservedPositionData = new
            {
                security = Security?.ToString() ?? "",
                volume = currentPosition,
                average_price = (decimal?)null,
                in_position = currentPosition != 0m,
                captured_from = "current_position_startup",
            };
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
            MaybeEmitRecoverySnapshot(force: true);
        }

        protected override void OnNewOrder(Order order)
        {
            if (_disposed || !IsCurrentChartSecurity(order.Security?.ToString()))
                return;

            var data = ObserveOrder(order);
            Emit("order_new", data);
            MaybeEmitRecoverySnapshot(force: true);
        }

        protected override void OnOrderChanged(Order order)
        {
            if (_disposed || !IsCurrentChartSecurity(order.Security?.ToString()))
                return;

            var data = ObserveOrder(order);
            Emit("order_changed", data);
            MaybeEmitRecoverySnapshot(force: true);
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
            MaybeEmitRecoverySnapshot();
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

                Emit("state_snapshot", new
                {
                    last_observed_position = _lastObservedPositionData,
                    position_known = _lastObservedPositionData != null,
                    orders = SnapshotObservedOrders(),
                    // ATAS does not provide a reliable initial enumeration
                    // of user-created chart orders. The stream becomes
                    // complete only for orders observed after this strategy
                    // starts; callback-derived snapshots never erase them.
                    orders_complete = false,
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
            lock (_outboxLock)
            {
                if (string.Equals(eventType, "quote", StringComparison.Ordinal)
                    && Volatile.Read(ref _outboxBacklog) >= Math.Max(1, MaximumQueuedQuoteEvents))
                {
                    // Quotes are observational. Dropping them during an
                    // outage is safe because the next state_snapshot is
                    // authoritative, whereas unbounded quote backlog would
                    // delay order/position events after reconnect.
                    Interlocked.Increment(ref _droppedQuoteEvents);
                    return;
                }

                var message = new ProbeEnvelope
                {
                    SessionId = _sessionId,
                    EventId = ++_eventSequence,
                    EventType = eventType,
                    SourceSymbol = GetSourceSymbol(),
                    AtasTimestampUtc = DateTimeOffset.UtcNow.ToString("O"),
                    LocalMonotonicTicks = Stopwatch.GetTimestamp(),
                    LocalStopwatchFrequency = Stopwatch.Frequency,
                    DeliveryMode = NormalizedDeliveryMode(),
                    Data = data,
                };
                if (IsCriticalEvent(eventType))
                    Interlocked.Exchange(ref _latestCriticalEventId, message.EventId);

                // ATAS callbacks can arrive close together. A task per event
                // races for the WebSocket send lock and can invert event IDs.
                // This single tail preserves source order without blocking the
                // callback thread on I/O or acknowledgement latency.
                Interlocked.Increment(ref _outboxBacklog);
                _outboxTail = _outboxTail
                    .ContinueWith(
                        async _ =>
                        {
                            try
                            {
                                await SendEventAsync(message);
                            }
                            finally
                            {
                                Interlocked.Decrement(ref _outboxBacklog);
                            }
                        },
                        CancellationToken.None,
                        TaskContinuationOptions.None,
                        TaskScheduler.Default)
                    .Unwrap();
            }
        }

        private async Task SendEventAsync(ProbeEnvelope message)
        {
            while (!_disposed)
            {
                try
                {
                    if (string.Equals(message.EventType, "quote", StringComparison.Ordinal)
                        && message.EventId < Interlocked.Read(ref _latestCriticalEventId))
                        return;
                    if (_client == null)
                        return;

                    var sentAt = Stopwatch.GetTimestamp();
                    // Acknowledgement is awaited only by this background
                    // outbox. The ATAS callback never waits, but no event is
                    // allowed to overtake or be forgotten after a disconnect.
                    var acknowledgement = await _client.SendAsync(
                        message,
                        true,
                        string.Equals(message.EventType, "quote", StringComparison.Ordinal)
                            ? Math.Min(50, Math.Max(10, AcknowledgementTimeoutMilliseconds))
                            : AcknowledgementTimeoutMilliseconds);
                    if (acknowledgement != null)
                    {
                        if (message.DeliveryMode == "sync")
                        {
                            var elapsedMilliseconds = (Stopwatch.GetTimestamp() - sentAt) * 1000.0 / Stopwatch.Frequency;
                            StrategyLog.Append(
                                $"Probe sync acknowledgement event={message.EventId}, elapsed_ms={elapsedMilliseconds:F3}, " +
                                $"server_received={acknowledgement.ServerReceivedUtc}\n");
                        }
                        return;
                    }
                }
                catch (Exception ex)
                {
                    StrategyLog.Append($"Probe event send error event={message.EventId}: {ex.Message}\n");
                }

                try
                {
                    await Task.Delay(100);
                }
                catch
                {
                    return;
                }
            }
        }

        private void MaybeEmitRecoverySnapshot(bool force = false)
        {
            var now = Stopwatch.GetTimestamp();
            var minimumTicks = Stopwatch.Frequency * Math.Max(100, RecoverySnapshotIntervalMilliseconds) / 1000;
            var previous = Interlocked.Read(ref _lastRecoverySnapshotTicks);
            if (!force && now - previous < minimumTicks)
                return;

            Interlocked.Exchange(ref _lastRecoverySnapshotTicks, now);
            Emit("state_snapshot", new
            {
                last_observed_position = _lastObservedPositionData,
                position_known = _lastObservedPositionData != null,
                orders = SnapshotObservedOrders(),
                orders_complete = false,
                dropped_quote_events = Interlocked.Read(ref _droppedQuoteEvents),
            });
        }

        private object ObserveOrder(Order order)
        {
            var data = BuildOrderData(order);
            var orderId = order.Id?.Trim() ?? "";
            if (orderId.Length == 0)
                return data;

            lock (_observedOrdersLock)
            {
                if (IsActive(order))
                    _observedOrders[orderId] = new ObservedOrder(data);
                else
                    _observedOrders.Remove(orderId);
            }
            return data;
        }

        private object[] SnapshotObservedOrders()
        {
            lock (_observedOrdersLock)
                return _observedOrders.Values.Select(order => order.Data).ToArray();
        }

        private static bool IsActive(Order order)
        {
            // `Active` is the lifecycle value emitted by the installed ATAS
            // connector for pending orders. A cancelled/done order must win
            // even if an older mutable instance still reports a quantity.
            return !order.Canceled
                && order.Unfilled > 0m
                && string.Equals(order.State.ToString(), "Active", StringComparison.OrdinalIgnoreCase);
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

        private static bool IsCriticalEvent(string eventType)
        {
            return string.Equals(eventType, "order_new", StringComparison.Ordinal)
                || string.Equals(eventType, "order_changed", StringComparison.Ordinal)
                || string.Equals(eventType, "position_changed", StringComparison.Ordinal);
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
