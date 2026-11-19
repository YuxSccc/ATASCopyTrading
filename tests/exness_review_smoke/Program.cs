using System;
using System.Globalization;
using System.IO;
using System.Linq;
using ExnessReview;

if (args.Length == 3 && args[0] == "--import")
{
    var imported = ReviewStore.Import(args[1], args[2]);
    Console.WriteLine($"Account {args[1]}: added {imported.Added}, updated {imported.Updated}, skipped {imported.Skipped}, conflicts {imported.Conflicts}.");
    return;
}
if (args.Length > 1 || (args.Length == 1 && args[0].StartsWith("--")))
    throw new ArgumentException("Usage: [sample.csv] or --import <account> <csv>");

var checks = 0;
void Check(bool condition, string description)
{
    if (!condition) throw new InvalidOperationException(description);
    checks++;
}
var sandbox = Directory.CreateTempSubdirectory("exness-review-smoke-");
Environment.SetEnvironmentVariable("EXNESS_REVIEW_STORE_DIR", Path.Combine(sandbox.FullName, "cache"));
try
{
    foreach (var (instrument, source) in new[] {
        ("GC", "XAUUSD"), ("GCZ6@COMEX", "XAUUSD"), ("comex:gcZ26", "XAUUSD"),
        ("NQ", "USTEC"), ("NQ 12-26", "USTEC"), ("NQH2027", "USTEC"),
        ("ES", "US500"), ("ES1!", "US500"), ("ESH7 (CME)", "US500"),
        ("MGC", null), ("MNQZ26", null), ("MESZ6@CME", null), ("NG", null),
        ("GCLOUD", null), ("NQUSD", null), ("ESZ6-NQZ6", null), ("BTCUSD", null)
    })
        Check(Projection.SourceSymbol(instrument) == source, "Unexpected mapping: " + instrument);

    Check(Projection.SourceSymbol("BTCUSDT", "BinanceFutures") == "BTCUSD" &&
        Projection.SourceSymbol("#BTCUSDT@BinanceFutures") == "BTCUSD", "Binance perpetual chart identity");
    Check(Projection.SourceSymbol("BTCUSDT", "Binance") == null &&
        Projection.SourceSymbol("BTCUSDT@Bybit") == null && Projection.SourceSymbol("BTCUSDT") == null &&
        Projection.SourceSymbol("BTCUSDT@BinanceFutures", "Binance") == null, "exclude spot, other venues and unknown venues");

    const string header = "ticket,opening_time_utc,closing_time_utc,type,lots,original_position_size,symbol,opening_price,closing_price,stop_loss,take_profit,commission,swap,profit,equity,margin_level,close_reason";
    const string opened = "101,2026-05-01T09:00:00,,buy,0.01,0.01,XAUUSD,4500,,,,,,,,,";
    const string closed = "101,2026-05-01T09:00:00,2026-05-01T09:01:00,buy,0.01,0.01,XAUUSD,4500,4510,4490,4510,-0.1,0,10,,,tp";
    const string other = "102,2026-05-01T09:00:10,2026-05-01T09:01:00,sell,0.05,0.05,USTECm,24500,24510,,,,-1,-5,,,sl";
    var selected = Path.Combine(sandbox.FullName, "import.csv");
    void Csv(params string[] rows) => File.WriteAllLines(selected, new[] { header }.Concat(rows));
    Csv(opened);
    var report = ReviewStore.Import("Smoke", selected);
    Check(report.Added == 1 && ReviewStore.Read("Smoke").Length == 1, "new open trade");
    var unclosed = ReviewStore.Read("Smoke")[0];
    Check(unclosed.CloseUtc is null && unclosed.OpenUtc.Kind == DateTimeKind.Utc, "timestamps are UTC and missing closure stays absent");
    var openMarks = Projection.Events(new[] {unclosed}, "XAUUSD");
    Check(openMarks.Length == 1 && openMarks[0].Below && !openMarks[0].IsExit && openMarks[0].Label == "开多", "open position draws one entry");
    Csv(closed, other);
    report = ReviewStore.Import("Smoke", selected);
    Check(report.Added == 1 && report.Updated == 1, "pending ticket becomes a closed position");
    var trades = ReviewStore.Read("Smoke");
    Check(trades.Length == 2 && trades.Single(trade => trade.Ticket == "101").Lots == 0.01m, "decimal volume survives persist/reload");
    var apiOpen = opened.Replace("101,", "API-51-open,", StringComparison.Ordinal);
    var apiExit = closed.Replace("101,", "API-51-exit,", StringComparison.Ordinal);
    Csv(opened);
    ReviewStore.Import("Sync", selected);
    Csv(opened.Replace("101,", "API-510-another,", StringComparison.Ordinal));
    ReviewStore.ImportApiSnapshot("Sync", "510", selected, 1);
    Csv(apiOpen);
    Check(ReviewStore.ImportApiSnapshot("Sync", "51", selected, 1).Added == 1, "initial API live position");
    Csv(apiExit);
    var replacement = ReviewStore.ImportApiSnapshot("Sync", "51", selected, 1);
    Check(replacement.Added == 1 && replacement.Removed == 1 && ReviewStore.Read("Sync").Length == 3 &&
        ReviewStore.Read("Sync").Any(t => t.Ticket == "API-510-another") && ReviewStore.Read("Sync").Any(t => t.Ticket == "101"),
        "replace an open API position with final legs, preserve CSV and other API accounts");
    var beforeRepeat = ReviewStore.Snapshot("Sync");
    Check(ReviewStore.ImportApiSnapshot("Sync", "51", selected, 1).Skipped == 1, "repeated snapshot is idempotent");
    Check(ReferenceEquals(beforeRepeat, ReviewStore.Snapshot("Sync")), "unchanged API history keeps existing chart price matches");
    Csv(opened);
    try { ReviewStore.ImportApiSnapshot("Sync", "51", selected, 1); throw new Exception("foreign prefix accepted"); }
    catch (InvalidDataException) { Check(ReviewStore.Read("Sync").Any(t => t.Ticket == "API-51-exit"), "bad snapshot preserves prior state"); }
    Csv();
    try { ReviewStore.ImportApiSnapshot("Sync", "51", selected, 1); throw new Exception("missing API rows accepted"); }
    catch (InvalidDataException) { Check(ReviewStore.Read("Sync").Length == 3, "truncated API snapshot rejected"); }
    var firstApi = opened.Replace("101,", "API-51-125-O,", StringComparison.Ordinal);
    var nextApi = closed.Replace("101,", "API-51-126-C,", StringComparison.Ordinal);
    Csv(firstApi);
    ReviewStore.ImportApiSnapshot("Sync", "51", selected, 1);
    Csv(nextApi);
    var partial = ReviewStore.ImportApiSnapshot("Sync", "51", selected, 1,
        new System.Collections.Generic.HashSet<string> { "125" });
    Check(partial.Removed == 0 && partial.Added == 1 && ReviewStore.Read("Sync").Any(t => t.Ticket == "API-51-125-O"),
        "incomplete source position preserved while other positions update");
    Csv(closed, other);
    Check(ReviewStore.Import("Smoke", selected).Skipped == 2, "full repeated export is idempotent");
    Csv(other);
    Check(ReviewStore.Import("Smoke", selected).Skipped == 1 && ReviewStore.Read("Smoke").Length == 2, "overlap does not delete older history");
    Check(ReviewStore.Read("Other").Length == 0 && ReviewStore.Import("Other", selected).Added == 1, "account separation");
    Csv(closed.Replace(",4510,4490", ",4509,4490", StringComparison.Ordinal));
    Check(ReviewStore.Import("Smoke", selected).Conflicts == 1 && ReviewStore.Read("Smoke").Single(trade => trade.Ticket == "101").ClosePrice == 4510m, "conflicting closed ticket does not replace accepted row");
    Check(ReviewStore.Import("Smoke", selected).Skipped == 1, "persisted conflict is not duplicated");

    var gold = Projection.Events(trades, "XAUUSD");
    var nasdaq = Projection.Events(trades, "USTEC");
    Check(gold.Length == 2 && gold[1].Label == "止盈" && !gold[1].Below && gold[1].IsExit, "long close direction/reason");
    Check(nasdaq.Length == 2 && nasdaq[0].Label == "开空" && !nasdaq[0].Below && nasdaq[1].Label == "止损" && nasdaq[1].Below, "NQ matches USTECm, short entry and cover");
    Check(Projection.Events(trades, "US500").Length == 0, "ES chart cannot show other markets");
    var goldTrade = gold[0].Trade;
    Check(Projection.QuantityLabel(nasdaq[0].Trade).Contains("0.0025 NQ"), "ordinary USTEC notional conversion");
    var amplified = goldTrade with { Symbol = "USTEC_x100", Lots = 1m };
    Check(Projection.Events([amplified], "USTEC").Length == 2 && Projection.QuantityLabel(amplified).Contains("5 NQ"), "amplified USTEC");
    var btc = goldTrade with { Symbol = "BTCUSD", Lots = 0.2m };
    Check(Projection.Events([btc, btc with { Symbol = "BTCUSDT" }], "BTCUSD").Length == 2 &&
        Projection.QuantityLabel(btc).Contains("0.2 BTC") &&
        Projection.QuantityLabel(btc with { Symbol = "BTCUSDc" }).Contains("0.002 BTC"), "BTC fills and unit conversion");
    var custom = new[] { goldTrade with { Ticket = "150", Symbol = "US500", CloseReason = "user" },
        goldTrade with { Ticket = "151", Symbol = "US500z", CloseReason = "user" },
        goldTrade with { Ticket = "152", Symbol = "US5000" }, goldTrade with { Ticket = "153", Symbol = "US500EUR" } };
    var sp = Projection.Events(custom.Reverse(), "US500");
    Check(sp.Length == 4 && sp[0].Trade.Ticket == "150" && sp[1].Trade.Ticket == "151" &&
        sp[2].Utc == sp[3].Utc && sp[2].Label == "平多", "qualified suffixes only; same-time details kept in deterministic order");

    if (args.Length == 1)
    {
        var sample = Path.GetFullPath(args[0]);
        report = ReviewStore.Import("Sample", sample);
        var sampleRows = ReviewStore.Read("Sample");
        var goldEvents = Projection.Events(sampleRows, "XAUUSD");
        Check(sampleRows.Length > 0 && sampleRows.Length == report.Added && report.Conflicts == 0, "sample parses without conflicts");
        Check(ReviewStore.Import("Sample", sample).Skipped == sampleRows.Length, "real export repeat");
        var originalRows = File.ReadLines(sample).ToArray();
        File.WriteAllLines(selected, originalRows.Take(Math.Min(originalRows.Length, 9)));
        Check(ReviewStore.Import("Sample", selected).Skipped == Math.Min(originalRows.Length - 1, 8) &&
            ReviewStore.Read("Sample").Length == sampleRows.Length, "real export overlapping slice");
        Console.WriteLine(string.Create(CultureInfo.InvariantCulture, $"Sample trades: {sampleRows.Length}; GC events: {goldEvents.Length}; same-second grouped exits: {goldEvents.Where(mark => mark.IsExit).GroupBy(mark => (mark.Utc, mark.Label, mark.Below)).Count(group => group.Count() > 1)}"));
    }
    Console.WriteLine($"{checks} Exness review checks passed.");
}
finally
{
    sandbox.Delete(recursive: true);
}
