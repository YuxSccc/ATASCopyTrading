using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading;
using System.Windows;
using System.Windows.Threading;
using ATAS.Indicators;

namespace ExnessReview;

public sealed partial class ReviewIndicator
{
    private sealed record TickFlight(CumulativeTradesRequest Request, DateTime[] Targets, long Generation);

    private const int MaxNativeWindows = 256;
    private readonly Queue<DateTime[]> _nativeWindows = new();
    private Action<DateTime[], IReadOnlyDictionary<DateTime, FuturePrint>?>? _nativeWindowFinished;
    private Action? _nativeCompleted;
    private TickFlight? _nativeFlight;
    private DispatcherTimer? _nativeTimeout;
    private long _nativeGeneration;
    private (string Code, string Exchange) _nativeChart;
    private string _nativeAccount = "";

    private void CancelNativeLookup()
    {
        Interlocked.Increment(ref _nativeGeneration);
        _nativeWindows.Clear();
        _nativeCompleted = null;
        _nativeWindowFinished = null;
    }

    private void StartNativeLookup(DateTime[] times,
        Action<DateTime[], IReadOnlyDictionary<DateTime, FuturePrint>?> windowFinished, Action completed)
    {
        _nativeWindowFinished = windowFinished;
        _nativeCompleted = completed;
        _nativeChart = CurrentChart;
        _nativeAccount = AccountAlias;
        var windows = new List<List<DateTime>>();
        foreach (var time in times.Distinct().OrderByDescending(time => time))
        {
            if (windows.Count == 0 || windows[^1][^1] - time > TimeSpan.FromSeconds(15)
                || windows[^1][0] - time > TimeSpan.FromSeconds(55))
            {
                if (windows.Count == MaxNativeWindows) break;
                windows.Add(new List<DateTime>());
            }
            windows[^1].Add(time);
        }
        foreach (var window in windows)
        {
            window.Reverse();
            _nativeWindows.Enqueue(window.ToArray());
        }
        if (_nativeWindows.Count == 0) FinishNative();
        else if (Volatile.Read(ref _nativeFlight) == null)
        {
            var generation = _nativeGeneration;
            Application.Current?.Dispatcher.BeginInvoke(DispatcherPriority.Background, new Action(() =>
            {
                if (generation == _nativeGeneration) SendNextNative();
            }));
        }
        else ArmNativeTimeout();
    }

    private void SendNextNative()
    {
        if (_nativeCompleted == null || Volatile.Read(ref _disposed) != 0) return;
        if (_nativeAccount != AccountAlias || _nativeChart != CurrentChart)
        {
            CancelNativeLookup();
            return;
        }
        if (!_nativeWindows.TryDequeue(out var targets)) { FinishNative(); return; }
        var request = new CumulativeTradesRequest(targets[0].AddSeconds(-5), targets[^1].AddSeconds(1), 0, 0);
        Volatile.Write(ref _nativeFlight, new TickFlight(request, targets, _nativeGeneration));
        ArmNativeTimeout();
        try { RequestForCumulativeTrades(request); }
        catch (Exception)
        {
            Volatile.Write(ref _nativeFlight, null);
            _nativeWindowFinished?.Invoke(targets, null);
            FinishNative();
        }
    }

    private void ArmNativeTimeout()
    {
        if (_nativeTimeout == null)
        {
            // An uncached historical request can fetch an entire exchange session.
            _nativeTimeout = new DispatcherTimer { Interval = TimeSpan.FromSeconds(90) };
            _nativeTimeout.Tick += (_, _) =>
            {
                var previous = Interlocked.Exchange(ref _nativeFlight, null);
                if (previous != null && previous.Generation != _nativeGeneration) SendNextNative();
                else
                {
                    if (previous != null) _nativeWindowFinished?.Invoke(previous.Targets, null);
                    FinishNative();
                }
                if (_nativeFlight == null) _nativeTimeout.Stop();
            };
        }
        _nativeTimeout.Stop();
        _nativeTimeout.Start();
    }

    private void FinishNative()
    {
        _nativeTimeout?.Stop();
        _nativeWindows.Clear();
        var completed = _nativeCompleted;
        _nativeCompleted = null;
        _nativeWindowFinished = null;
        if (Volatile.Read(ref _disposed) == 0 && _nativeAccount == AccountAlias && _nativeChart == CurrentChart)
            completed?.Invoke();
    }

    protected override void OnCumulativeTradesResponse(CumulativeTradesRequest request, IEnumerable<CumulativeTrade> cumulativeTrades)
    {
        var flight = Volatile.Read(ref _nativeFlight);
        if (flight == null || !ReferenceEquals(flight.Request, request) || Volatile.Read(ref _disposed) != 0) return;
        var matches = new Dictionary<DateTime, FuturePrint>();
        try
        {
            foreach (var aggregate in cumulativeTrades)
            {
                if (flight.Generation != Volatile.Read(ref _nativeGeneration)) break;
                if (aggregate.Ticks == null) continue;
                foreach (var tick in aggregate.Ticks)
                {
                    if (tick.Price <= 0) continue;
                    var utc = tick.Time.Kind == DateTimeKind.Local ? tick.Time.ToUniversalTime() : DateTime.SpecifyKind(tick.Time, DateTimeKind.Utc);
                    var first = Array.BinarySearch(flight.Targets, utc);
                    if (first < 0) first = ~first;
                    for (var index = first; index < flight.Targets.Length; index++)
                    {
                        var target = flight.Targets[index];
                        if (target - utc > TimeSpan.FromSeconds(5)) break;
                        if (!matches.TryGetValue(target, out var previous) || utc >= previous.Utc)
                            matches[target] = new FuturePrint(utc, tick.Price);
                    }
                }
            }
        }
        catch (Exception) { matches = null; }

        Application.Current?.Dispatcher.BeginInvoke(DispatcherPriority.Background, new Action(() =>
        {
            if (!ReferenceEquals(flight, Volatile.Read(ref _nativeFlight))) return;
            Volatile.Write(ref _nativeFlight, null);
            _nativeTimeout?.Stop();
            if (_nativeCompleted == null || Volatile.Read(ref _disposed) != 0) return;
            if (flight.Generation == _nativeGeneration)
                _nativeWindowFinished?.Invoke(flight.Targets, matches);
            SendNextNative();
        }));
    }
}
