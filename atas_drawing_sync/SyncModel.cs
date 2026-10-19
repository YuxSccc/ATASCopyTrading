using System;
using System.Collections.Generic;
using System.Drawing;
using System.Linq;
using System.Text.RegularExpressions;

namespace CrossInstrumentDrawings;

public enum ShapeKind { Rectangle, TrendLine }
public enum LineExtent { Segment, Ray, Infinite }

public sealed record DrawingShape(string Id, ShapeKind Kind, DateTime FirstTime, decimal FirstPrice,
    DateTime SecondTime, decimal SecondPrice, Color Color, Color FillColor, float Width, string Dash,
    bool ExtendLeft, bool ExtendRight, LineExtent Extent);

public sealed record DrawingSnapshot(Guid Owner, string Instrument, string Contract, DateTimeOffset CapturedAt,
    IReadOnlyList<DrawingShape> Shapes, int TotalObjects, int SkippedObjects);

public static class InstrumentIdentity
{
    public static string Root(string name)
    {
        var text = name.ToUpperInvariant();
        var match = Regex.Match(text, @"(?<![A-Z])(MGC|GC)(?=$|[^A-Z0-9]|[FGHJKMNQUVXZ]\d)");
        return match.Success ? match.Groups[1].Value : string.Empty;
    }

    public static string Contract(string name, int currentYear)
    {
        var match = Regex.Match(name.ToUpperInvariant(), @"(?<![A-Z])(?:MGC|GC)\s*([FGHJKMNQUVXZ])\s*(\d{1,4})(?!\d)");
        if (!match.Success) return string.Empty;
        var yearText = match.Groups[2].Value;
        var year = int.Parse(yearText, System.Globalization.CultureInfo.InvariantCulture);
        if (yearText.Length == 1)
        {
            year += currentYear / 10 * 10;
            if (year < currentYear - 5) year += 10;
            if (year > currentYear + 5) year -= 10;
        }
        else if (yearText.Length == 2) year += 2000;
        if (year < 2000 || year > 2200) return string.Empty;
        return match.Groups[1].Value + year;
    }

    public static bool ContractsMatch(string source, string target)
        => source.Length == 0 || target.Length == 0 || source == target;
}

/// <summary>One source per channel; complete replacement snapshots propagate deletions.</summary>
public sealed class DrawingBus
{
    public static DrawingBus Shared { get; } = new();
    private readonly object _gate = new();
    private readonly Dictionary<string, DrawingSnapshot> _channels = new(StringComparer.Ordinal);
    public static readonly TimeSpan Lease = TimeSpan.FromSeconds(3);

    public bool Publish(string channel, DrawingSnapshot snapshot)
    {
        lock (_gate)
        {
            if (_channels.TryGetValue(channel, out var previous) && previous.Owner != snapshot.Owner &&
                snapshot.CapturedAt - previous.CapturedAt < Lease) return false;
            _channels[channel] = snapshot;
            return true;
        }
    }

    public DrawingSnapshot? Read(string channel, DateTimeOffset now)
    {
        lock (_gate)
            return _channels.TryGetValue(channel, out var snapshot) && now - snapshot.CapturedAt < Lease ? snapshot : null;
    }

    public void RemoveOwner(Guid owner)
    {
        lock (_gate)
            foreach (var key in _channels.Where(x => x.Value.Owner == owner).Select(x => x.Key).ToArray())
                _channels.Remove(key);
    }
}

public static class TimeProjection
{
    /// <summary>Interpolate TIME anchors, never transfer MGC bar indices to GC.</summary>
    public static bool TryBar(IReadOnlyList<DateTime> times, DateTime time, out double bar)
    {
        bar = 0;
        if (times.Count == 0 || time < times[0]) return false;
        var last = times.Count - 1;
        if (time > times[last])
        {
            for (var i = last - 1; i >= Math.Max(0, last - 100); i--)
            {
                var period = times[last].Ticks - times[i].Ticks;
                if (period <= 0) continue;
                bar = last + (double)(time.Ticks - times[last].Ticks) * (last - i) / period;
                return true;
            }
            return false;
        }
        int left = 0, right = last;
        while (left < right)
        {
            var mid = (left + right + 1) / 2;
            if (times[mid] <= time) left = mid; else right = mid - 1;
        }
        bar = left;
        if (left < last && times[left + 1] > times[left])
            bar += (double)(time.Ticks - times[left].Ticks) / (times[left + 1].Ticks - times[left].Ticks);
        return true;
    }

    public static bool ClipLine(double x1, double y1, double x2, double y2, Rectangle bounds,
        LineExtent extent, out PointF first, out PointF second)
    {
        first = second = default;
        if (!double.IsFinite(x1 + y1 + x2 + y2)) return false;
        var dx = x2 - x1;
        var dy = y2 - y1;
        if (Math.Abs(dx) + Math.Abs(dy) < 1e-9) return false;
        var low = extent == LineExtent.Infinite ? double.NegativeInfinity : 0;
        var high = extent == LineExtent.Segment ? 1 : double.PositiveInfinity;
        bool Edge(double p, double q)
        {
            if (Math.Abs(p) < 1e-12) return q >= 0;
            var r = q / p;
            if (p < 0) low = Math.Max(low, r); else high = Math.Min(high, r);
            return low <= high;
        }
        if (!Edge(-dx, x1 - bounds.Left) || !Edge(dx, bounds.Right - x1) ||
            !Edge(-dy, y1 - bounds.Top) || !Edge(dy, bounds.Bottom - y1)) return false;
        first = new PointF((float)(x1 + low * dx), (float)(y1 + low * dy));
        second = new PointF((float)(x1 + high * dx), (float)(y1 + high * dy));
        return true;
    }
}
