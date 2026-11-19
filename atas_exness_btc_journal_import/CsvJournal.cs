using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Threading.Tasks;
using ExnessReview;
using OFT.Platform.Core.Managers.Database;
using OFT.Platform.Core.Managers.Statistics;

namespace ExnessJournal;

internal static class CsvJournal
{
    internal sealed record Report(string Status, string Account, JournalBatch.Report[] Markets);

    internal static JournalBatch.Market? MarketFor(string symbol) => symbol switch
    {
        "BTCUSD" => JournalBatch.Bitcoin,
        "XAUUSD" => JournalBatch.Gold,
        "USTEC" => JournalBatch.Nasdaq,
        _ => null
    };

    internal static bool Covers(JournalBatch.Market market, Trade trade) =>
        market == JournalBatch.Bitcoin || (trade.OpenUtc >= new DateTime(2026, 11, 1) &&
            trade.CloseUtc < new DateTime(2026, 12, 1));

    internal static async Task<Report> Import(string path, ICacheManager cache, ITradingStatisticsManager stats)
    {
        var rows = ReviewStore.Parse(path);
        if (rows.Count == 0) throw new InvalidDataException("CSV 没有成交。");
        var batches = new List<JournalBatch>();
        foreach (var group in rows.GroupBy(t => t.Symbol))
        {
            var market = MarketFor(group.Key) ?? throw new InvalidDataException($"该日志导入器尚未映射 {group.Key}。");
            if (group.Any(t => !Covers(market, t)))
                throw new InvalidDataException($"仅已指定 2026 年 11 月的 {group.Key} 映射至 {market.SecurityId}；其他月份需选择相应交割月。");
            batches.Add(await JournalBatch.Prepare(market, group.ToArray(), cache, JournalBatch.LegacyCsvAccount));
        }
        var reports = batches.Select(batch => batch.Write(stats)).ToArray();
        await cache.FlushAsync();
        await stats.ReloadStatisticsAsync();
        return new Report("complete", JournalBatch.LegacyCsvAccount, reports);
    }
}
