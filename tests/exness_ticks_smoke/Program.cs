using System.Globalization;
using System.IO.Compression;
using ExnessReview;
using Microsoft.VisualBasic.FileIO;

CultureInfo.CurrentCulture = CultureInfo.InvariantCulture;
var checks = 0;
void Check(bool ok, string description)
{
    if (!ok) throw new InvalidOperationException(description);
    checks++;
}

var directory = Directory.CreateTempSubdirectory("exness-ticks-smoke-");
var start = new DateTime(2026, 11, 14, 0, 0, 0, DateTimeKind.Utc);
var entryTime = start;
var exitTime = start.AddMinutes(2);
var missedTime = start.AddMinutes(4);
var addedTime = start.AddMinutes(6);
var allTimes = new HashSet<DateTime> { entryTime, exitTime, missedTime, addedTime };
var session = new TickPriceMatches(allTimes, null, start);
var localPrice = new FuturePrint(entryTime, 3300m);
session.AddLocal(new Dictionary<DateTime, FuturePrint> { [entryTime] = localPrice }, preferArchive: false);
Check(session.MissingNative([entryTime]).SequenceEqual([entryTime]), "CME local matches need native confirmation");
var nativePrice = new FuturePrint(entryTime, 3300.2m);
session.ApplyNativeWindow([entryTime], new Dictionary<DateTime, FuturePrint> { [entryTime] = nativePrice }, start);
session.ApplyNativeWindow([missedTime], new Dictionary<DateTime, FuturePrint>(), start);
session.ApplyNativeWindow([exitTime], null, start);
var resumed = new TickPriceMatches(allTimes, session, start.AddSeconds(20));
Check(resumed.MissingLocal(allTimes).SequenceEqual([addedTime]) && resumed.MissingNative(allTimes).SequenceEqual([addedTime]) &&
    resumed.Snapshot()[entryTime].Print == nativePrice, "an added trade reuses completed matches and backs off known misses");
Check(!resumed.RetryDue(start.AddSeconds(59)) && resumed.RetryDue(start.AddMinutes(1)),
    "a failed request may be retried sooner than a successful empty response");
var retryErrors = new TickPriceMatches(allTimes, resumed, start.AddMinutes(1));
Check(retryErrors.MissingNative(allTimes).ToHashSet().SetEquals([addedTime, exitTime]), "retry failed windows and unrequested targets");
var retryMisses = new TickPriceMatches(allTimes, resumed, start.AddMinutes(30));
Check(retryMisses.MissingNative(allTimes).ToHashSet().SetEquals([addedTime, exitTime, missedTime]), "expired misses can recover");
var retained = new TickPriceMatches([entryTime], resumed, start);
Check(retained.Snapshot().Count == 1 && !retained.RetryDue(start.AddHours(1)), "removed targets and their backoff are discarded");
var btcArchive = new TickPriceMatches([entryTime], null, start);
btcArchive.AddLocal(new Dictionary<DateTime, FuturePrint> { [entryTime] = localPrice }, preferArchive: true);
Check(btcArchive.MissingNative([entryTime]).Length == 0, "Binance archive matches need no native check");
var fallback = new TickPriceMatches([entryTime], null, start);
fallback.AddLocal(new Dictionary<DateTime, FuturePrint> { [entryTime] = localPrice }, preferArchive: false);
fallback.ApplyNativeWindow([entryTime], new Dictionary<DateTime, FuturePrint>(), start);
var retryFallback = new TickPriceMatches([entryTime], fallback, start.AddMinutes(31));
Check(retryFallback.MissingLocal([entryTime]).Length == 0 && retryFallback.MissingNative([entryTime]).Length == 1 &&
    retryFallback.Snapshot()[entryTime].Print == localPrice, "CME fallback retained while only native matching is retried");
ReviewBar[] bars = [new(start, start.AddSeconds(59), 100, 90),
    new(start.AddHours(2), start.AddHours(2).AddSeconds(40), 110, 100)];
Check(new[] { 0, 30, 59 }.All(second => ReviewGeometry.Locate(bars, start.AddSeconds(second), TimeSpan.FromMinutes(1), out var bar) && bar == 0),
    "all timestamps in the same minute use the same bar");
Check(!ReviewGeometry.Locate(bars, start.AddHours(1), TimeSpan.FromMinutes(1), out _), "no projection in session gap");
Check(!ReviewGeometry.Locate(bars, start.AddMinutes(-1), null, out _), "no projection before available bars");
Check(ReviewGeometry.Clip(new(-80, 30), new(120, 30), new(0, 0, 80, 80), out var clippedStart, out var clippedEnd)
    && clippedStart.X == 0 && clippedEnd.X == 79 && ReviewGeometry.Near(new(40, 31), clippedStart, clippedEnd),
    "connect a trade across the viewport even if both arrows are outside");
void WriteDay(string symbol, DateTime date, params (DateTime Time, decimal Price)[] prints)
{
    var folder = Directory.CreateDirectory(Path.Combine(directory.FullName, symbol));
    var file = Path.Combine(folder.FullName, $"atas_export_{symbol}_{date:yyyyMMdd}.csv");
    File.WriteAllLines(file, new[] { "time,index,price,volume,side,parent_id" }.Concat(
        prints.Select((print, index) => $"{print.Time:yyyy-MM-ddTHH:mm:ss.fffffff},{index + 1},{print.Price},1,Buy,1")));
}

try
{
    WriteDay("GC", start.AddDays(-1), (start.AddMilliseconds(-125), 3822.1m));
    WriteDay("GC", start, (start.AddSeconds(9), 3824m), (start.AddMilliseconds(3100), 3823.1m),
        (start.AddSeconds(7), 3823.7m), (start.AddMilliseconds(2900), 3822.9m));
    WriteDay("ES", start, (start.AddSeconds(3), 6060m));
    var times = new[] { start, start.AddSeconds(3), start.AddSeconds(7), start.AddSeconds(14), start.AddSeconds(15),
        start.AddDays(1), start.AddSeconds(3) };
    var matches = TickPrices.Lookup(directory.FullName, "GC", times, CancellationToken.None);
    Check(matches.Count == 4, "unexpected matches or missing unique input");
    Check(matches[start].Price == 3822.1m && matches[start].Utc == start.AddMilliseconds(-125), "midnight predecessor");
    Check(matches[times[1]].Price == 3822.9m && matches[times[1]].Utc == start.AddMilliseconds(2900),
        "rejecting lookahead and scanning out-of-order prints");
    Check(matches[times[2]].Utc == times[2] && matches[times[2]].Price == 3823.7m, "exact match and root isolation");
    var apiTime = start.AddMilliseconds(3604);
    Check(TickPrices.Lookup(directory.FullName, "GC", [apiTime], CancellationToken.None)[apiTime].Price == 3823.1m,
        "API subsecond timestamps retain prints earlier in the same second");
    Check(matches[times[3]].Price == 3824m, "5-second bound includes equality");
    Check(!matches.ContainsKey(times[4]), "old price cannot be used when stale");
    Check(!matches.ContainsKey(times[5]), "no fallback for missing day");
    Check(TickPrices.Lookup(directory.FullName, "ES", [times[1]], CancellationToken.None)[times[1]].Price == 6060m,
        "independent symbol directories");
    Check(TickPrices.Lookup(directory.FullName, "NQ", [times[1]], CancellationToken.None).Count == 0,
        "no match for absent root directory");
    var btcDir = Directory.CreateDirectory(Path.Combine(directory.FullName, TickPrices.BinanceFuturesRoot));
    var name = $"BTCUSDT-aggTrades-{start:yyyy-MM-dd}";
    using (var zip = ZipFile.Open(Path.Combine(btcDir.FullName, name + ".zip"), ZipArchiveMode.Create))
    using (var writer = new StreamWriter(zip.CreateEntry(name + ".csv").Open()))
    {
        writer.WriteLine("agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker");
        var ms = new DateTimeOffset(start).ToUnixTimeMilliseconds();
        writer.WriteLine($"1,100.2,1,1,1,{ms + 899},true");
        writer.WriteLine($"2,100.3,1,2,2,{ms + 899},false");
        writer.WriteLine($"3,110.1,1,3,3,{ms + 1001},true");
    }
    var btcTime = start.AddSeconds(1);
    var btc = TickPrices.Lookup(directory.FullName, TickPrices.BinanceFuturesRoot, [btcTime, start.AddSeconds(7)], CancellationToken.None);
    Check(btc.Count == 1 && btc[btcTime] == new FuturePrint(start.AddMilliseconds(899), 100.3m),
        "Binance futures archive uses latest non-future print, same-ms file order and max age");
    Console.WriteLine($"{checks} synthetic checks passed.");
}
finally { directory.Delete(true); }

if (args.Length == 0) return;
if (args.Length != 2) throw new ArgumentException("Optional: <Exness CSV> <Level1 directory>");

using var reader = new TextFieldParser(args[0]) { HasFieldsEnclosedInQuotes = true };
reader.SetDelimiters(",");
var headers = reader.ReadFields() ?? throw new InvalidDataException("Missing CSV header");
int IndexOf(string header) => Array.IndexOf(headers, header) is var index && index >= 0
    ? index : throw new InvalidDataException($"Missing CSV column: {header}");
var symbolIndex = IndexOf("symbol");
var openIndex = IndexOf("opening_time_utc");
var closeIndex = IndexOf("closing_time_utc");
var events = new List<DateTime>();
while (reader.ReadFields() is { } fields)
{
    if (!fields[symbolIndex].Equals("XAUUSD", StringComparison.OrdinalIgnoreCase)) continue;
    foreach (var index in new[] { openIndex, closeIndex })
    {
        if (string.IsNullOrWhiteSpace(fields[index])) continue;
        events.Add(DateTime.Parse(fields[index], CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal));
    }
}
var found = TickPrices.Lookup(args[1], "GC", events, CancellationToken.None);
var matched = events.Where(found.ContainsKey).Order().ToArray();
Check(matched.Length > 0, "no real GC matches to verify");
foreach (var time in matched)
    Check(found[time].Price > 0 && found[time].Utc <= time && time - found[time].Utc <= TimeSpan.FromSeconds(5), "invalid matched real print");
Console.WriteLine($"GC matching: {matched.Length}/{events.Count} markers; {found.Count}/{events.Distinct().Count()} distinct instants.");
Console.WriteLine($"By date: {string.Join(", ", events.GroupBy(time => time.Date).OrderBy(group => group.Key).Select(group => $"{group.Key:MM-dd}:{group.Count(found.ContainsKey)}/{group.Count()}"))}");
var sampleTime = matched[matched.Length / 2];
var sample = found[sampleTime];
Console.WriteLine($"Example: {sampleTime:O}, futures(print)={sample.Utc:O}, GC={sample.Price}, age={(sampleTime - sample.Utc).TotalMilliseconds:F4} ms");
