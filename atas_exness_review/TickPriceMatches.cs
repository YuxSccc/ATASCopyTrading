using System;
using System.Collections.Generic;
using System.Linq;

namespace ExnessReview;

internal sealed record PricePoint(FuturePrint Print, bool FromChart, bool NeedsNativeCheck);

// Used only on the UI dispatcher. Renderers receive a separate snapshot.
internal sealed class TickPriceMatches
{
    private readonly Dictionary<DateTime, PricePoint> _prices = new();
    private readonly Dictionary<DateTime, DateTime> _retryAfter = new();

    internal TickPriceMatches(HashSet<DateTime> targets, TickPriceMatches? previous, DateTime now)
    {
        if (previous == null) return;
        foreach (var time in targets)
        {
            if (previous._prices.TryGetValue(time, out var price)) _prices.Add(time, price);
            if (previous._retryAfter.TryGetValue(time, out var retry) && retry > now) _retryAfter.Add(time, retry);
        }
    }

    internal Dictionary<DateTime, PricePoint> Snapshot() => new(_prices);
    internal bool RetryDue(DateTime now) => _retryAfter.Values.Any(retry => retry <= now);

    internal DateTime[] MissingLocal(IEnumerable<DateTime> targets) =>
        targets.Where(time => !_prices.ContainsKey(time) && !_retryAfter.ContainsKey(time)).ToArray();

    internal DateTime[] MissingNative(IEnumerable<DateTime> targets) =>
        targets.Where(time => !_retryAfter.ContainsKey(time) && (!_prices.TryGetValue(time, out var price) || price.NeedsNativeCheck)).ToArray();

    internal void AddLocal(IReadOnlyDictionary<DateTime, FuturePrint> matches, bool preferArchive)
    {
        foreach (var (time, print) in matches)
            _prices[time] = new PricePoint(print, FromChart: false, NeedsNativeCheck: !preferArchive);
    }

    internal void ApplyNativeWindow(DateTime[] times, IReadOnlyDictionary<DateTime, FuturePrint>? matches, DateTime now)
    {
        foreach (var time in times)
        {
            if (matches != null && matches.TryGetValue(time, out var print))
                _prices[time] = new PricePoint(print, FromChart: true, NeedsNativeCheck: false);
            else
                _retryAfter[time] = now.AddMinutes(matches == null ? 1 : 30);
        }
    }
}
