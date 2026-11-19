using System;
using System.Collections.Generic;
using System.Drawing;

namespace ExnessReview;

internal readonly record struct ReviewBar(DateTime Start, DateTime Last, decimal High, decimal Low);

internal static class ReviewGeometry
{
    internal static bool Locate(IReadOnlyList<ReviewBar> bars, DateTime when, TimeSpan? period, out int index)
    {
        int left = 0, right = bars.Count;
        while (left < right)
        {
            var middle = left + (right - left) / 2;
            if (bars[middle].Start <= when) left = middle + 1; else right = middle;
        }
        index = left - 1;
        if (index < 0 || (index > 0 && bars[index - 1].Start == bars[index].Start)) return false;
        var bar = bars[index];
        if (!period.HasValue) return when <= bar.Last;
        var end = bar.Start + period.Value;
        if (index + 1 < bars.Count && bars[index + 1].Start < end) end = bars[index + 1].Start;
        return when < end;
    }

    internal static bool Near(Point mouse, Point start, Point end, double tolerance = 4)
    {
        var dx = (double)end.X - start.X;
        var dy = (double)end.Y - start.Y;
        var length = dx * dx + dy * dy;
        var fraction = length == 0 ? 0 : Math.Clamp(((mouse.X - start.X) * dx + (mouse.Y - start.Y) * dy) / length, 0, 1);
        var horizontal = start.X + fraction * dx - mouse.X;
        var vertical = start.Y + fraction * dy - mouse.Y;
        return horizontal * horizontal + vertical * vertical <= tolerance * tolerance;
    }

    internal static bool Clip(Point start, Point end, Rectangle bounds, out Point clippedStart, out Point clippedEnd)
    {
        double first = 0, last = 1, dx = (double)end.X - start.X, dy = (double)end.Y - start.Y;
        bool Edge(double delta, double distance)
        {
            if (delta == 0) return distance >= 0;
            if (delta < 0) first = Math.Max(first, distance / delta);
            else last = Math.Min(last, distance / delta);
            return first <= last;
        }
        clippedStart = clippedEnd = default;
        if (!Edge(-dx, start.X - (double)bounds.Left) || !Edge(dx, bounds.Right - 1d - start.X) ||
            !Edge(-dy, start.Y - (double)bounds.Top) || !Edge(dy, bounds.Bottom - 1d - start.Y)) return false;
        Point At(double fraction) => new((int)Math.Round(start.X + dx * fraction), (int)Math.Round(start.Y + dy * fraction));
        clippedStart = At(first);
        clippedEnd = At(last);
        return true;
    }
}
