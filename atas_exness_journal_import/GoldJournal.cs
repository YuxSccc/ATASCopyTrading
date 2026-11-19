using System;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Threading.Tasks;
using ExnessReview;
using OFT.Platform.Core.Managers.Database;
using OFT.Platform.Core.Managers.Statistics;

namespace ExnessJournal;

internal static class GoldJournal
{
    internal static async Task<JournalBatch.Report> Import(string path, ICacheManager cache, ITradingStatisticsManager stats)
    {
        using var file = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read);
        if (Convert.ToHexString(SHA256.HashData(file)) != "558347DD9EA85A68EEC6716EE5978D495F2640DD8E8956AA0132199FD4E8F0B8")
            throw new InvalidDataException("此导入仅适用于已确认的 Exness 黄金 CSV。");
        var rows = ReviewStore.Parse(path).Where(t => t.Symbol == JournalBatch.Gold.Symbol).ToArray();
        var batch = await JournalBatch.Prepare(JournalBatch.Gold, rows, cache, JournalBatch.LegacyCsvAccount);
        var result = batch.Write(stats);
        await cache.FlushAsync();
        await stats.ReloadStatisticsAsync();
        return result;
    }
}
