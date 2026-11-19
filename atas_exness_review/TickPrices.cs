using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Threading;

namespace ExnessReview;

public sealed record FuturePrint(DateTime Utc, decimal Price);

public static class TickPrices
{
    private const long MaxAge = 5 * TimeSpan.TicksPerSecond;
    public const string BinanceFuturesRoot = "BTCUSDT@BinanceFutures";

    public static Dictionary<DateTime, FuturePrint> Lookup(string parentDirectory, string root,
        IEnumerable<DateTime> utcTimes, CancellationToken cancel)
    {
        cancel.ThrowIfCancellationRequested();
        var result = new Dictionary<DateTime, FuturePrint>();
        root = root.Trim();
        root = root.Equals(BinanceFuturesRoot, StringComparison.OrdinalIgnoreCase) ? BinanceFuturesRoot : root.ToUpperInvariant();
        if (root is not ("GC" or "NQ" or "ES" or BinanceFuturesRoot) || string.IsNullOrWhiteSpace(parentDirectory)) return result;
        var originalTimes = utcTimes.Distinct().Select(original =>
        {
            var ticks = original.Kind == DateTimeKind.Local ? original.ToUniversalTime().Ticks : original.Ticks;
            return (original, ticks);
        }).ToArray();
        var days = new SortedDictionary<DateTime, List<long>>();
        foreach (var second in originalTimes.Select(item => item.ticks).Distinct().Order())
        {
            var day = new DateTime(second, DateTimeKind.Utc).Date;
            Include(day, second);
            if (day.Ticks > 0 && second - day.Ticks < MaxAge) Include(day.AddDays(-1), second);
        }
        var best = new Dictionary<long, FuturePrint>();
        foreach (var (day, seconds) in days)
        {
            cancel.ThrowIfCancellationRequested();
            if (root == BinanceFuturesRoot)
            {
                ReadBinanceDay(Path.Combine(parentDirectory, root), day, seconds, best, cancel);
                continue;
            }
            var path = Path.Combine(parentDirectory, root,
                $"atas_export_{root}_{day.ToString("yyyyMMdd", CultureInfo.InvariantCulture)}.csv");
            if (!File.Exists(path)) continue;
            ReadDay(path, seconds, best, cancel);
        }
        foreach (var (original, second) in originalTimes)
            if (best.TryGetValue(second, out var match)) result.Add(original, match);
        return result;

        void Include(DateTime date, long second)
        {
            if (!days.TryGetValue(date, out var list)) days[date] = list = new List<long>();
            list.Add(second);
        }
    }

    private static void ReadDay(string path, List<long> seconds, Dictionary<long, FuturePrint> best,
        CancellationToken cancel)
    {
        using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete,
            64 * 1024, FileOptions.SequentialScan);
        using var reader = new StreamReader(stream);
        var header = reader.ReadLine();
        if (header == null) return;
        if (!header.StartsWith("time,index,price,", StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException($"非 ATAS Level1 逐笔 CSV：{Path.GetFileName(path)}");
        var earliest = seconds[0] - MaxAge;
        var latest = seconds[^1];
        var lineCount = 0;
        while (reader.ReadLine() is { } line)
        {
            if ((lineCount++ & 1023) == 0) cancel.ThrowIfCancellationRequested();
            var fields = line.AsSpan();
            var firstComma = fields.IndexOf(',');
            if (firstComma < 0 || !DateTime.TryParseExact(fields[..firstComma], "yyyy-MM-dd'T'HH:mm:ss.FFFFFFFK",
                    CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal,
                    out var utc)) continue;
            var ticks = utc.Ticks;
            if (ticks < earliest || ticks > latest) continue;
            var index = seconds.BinarySearch(ticks);
            if (index < 0) index = ~index;
            if (index >= seconds.Count || seconds[index] - ticks > MaxAge) continue;
            fields = fields[(firstComma + 1)..];
            var indexEnd = fields.IndexOf(',');
            if (indexEnd < 0) continue;
            fields = fields[(indexEnd + 1)..];
            var priceEnd = fields.IndexOf(',');
            if (priceEnd <= 0 || !decimal.TryParse(fields[..priceEnd], NumberStyles.AllowLeadingSign |
                    NumberStyles.AllowDecimalPoint, CultureInfo.InvariantCulture, out var price) || price <= 0) continue;
            Record(seconds, index, utc, price, best);
        }
    }

    private static void ReadBinanceDay(string directory, DateTime day, List<long> seconds,
        Dictionary<long, FuturePrint> best, CancellationToken cancel)
    {
        var name = "BTCUSDT-aggTrades-" + day.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture);
        var path = Path.Combine(directory, name + ".zip");
        if (!File.Exists(path)) return;
        using var zip = ZipFile.OpenRead(path);
        var entry = zip.GetEntry(name + ".csv") ?? throw new InvalidDataException($"币安归档缺少 {name}.csv");
        using var reader = new StreamReader(entry.Open());
        var header = reader.ReadLine()?.Split(',') ?? [];
        int timeColumn = Array.IndexOf(header, "transact_time"), priceColumn = Array.IndexOf(header, "price");
        if (timeColumn < 0 || priceColumn < 0 || header.Length > 16)
            throw new InvalidDataException($"无法识别币安成交归档 {name}");
        Span<Range> fields = stackalloc Range[16];
        int index = 0, count = 0;
        while (reader.ReadLine() is { } line)
        {
            if ((count++ & 1023) == 0) cancel.ThrowIfCancellationRequested();
            var row = line.AsSpan();
            if (row.Split(fields, ',') <= Math.Max(timeColumn, priceColumn) ||
                !long.TryParse(row[fields[timeColumn]], NumberStyles.None, CultureInfo.InvariantCulture, out var ms)) continue;
            var utc = DateTimeOffset.FromUnixTimeMilliseconds(ms).UtcDateTime;
            while (index < seconds.Count && seconds[index] < utc.Ticks) index++;
            if (index == seconds.Count) break;
            if (seconds[index] - utc.Ticks > MaxAge ||
                !decimal.TryParse(row[fields[priceColumn]], NumberStyles.AllowDecimalPoint, CultureInfo.InvariantCulture, out var price) || price <= 0) continue;
            Record(seconds, index, utc, price, best);
        }
    }

    private static void Record(List<long> seconds, int index, DateTime utc, decimal price, Dictionary<long, FuturePrint> best)
    {
        for (; index < seconds.Count && seconds[index] - utc.Ticks <= MaxAge; index++)
        {
            var target = seconds[index];
            if (!best.TryGetValue(target, out var prior) || utc >= prior.Utc)
                best[target] = new FuturePrint(utc, price);
        }
    }
}
