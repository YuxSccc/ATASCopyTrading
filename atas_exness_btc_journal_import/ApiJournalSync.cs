using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Runtime.CompilerServices;
using System.Threading.Tasks;
using CommonServiceLocator;
using ExnessReview;
using OFT.Platform.Core.Managers.Connections;
using OFT.Platform.Core.Managers.Database;
using OFT.Platform.Core.Managers.Statistics;
using Utils.Common.Network.Client;

namespace ExnessJournal;

public static class ApiJournalSync
{
    private sealed record VerifiedGroup(Trade[] Source, JournalBatch Expected);
    private sealed class JournalState
    {
        public readonly Dictionary<(string Account, string Security, string FirstTicket), VerifiedGroup> Groups = new();
        public bool RefreshPending;
    }

    // Access is serialized by PendingJournalImport. A new ATAS registry starts with no
    // remembered pairings; a failed flush/reload is retried even if all fills now exist.
    private static readonly ConditionalWeakTable<ICacheManager, JournalState> States = new();

    public static async Task<string> SyncClosedTradesAsync(string csvPath, string sourceAccountId) =>
        (await SyncClosedTradesWithStatusAsync(csvPath, sourceAccountId).ConfigureAwait(false)).Message;

    public static Task<(bool Success, string Message)> SyncClosedTradesWithStatusAsync(string csvPath, string sourceAccountId) =>
        SyncClosedTradesWithStatusAsync(csvPath, sourceAccountId, null);

    public static Task<(bool Success, string Message)> SyncClosedTradesWithStatusAsync(string csvPath, string sourceAccountId, string? chartAccountId) =>
        PendingJournalImport.RunExclusive(() =>
        {
            var services = ServiceLocator.Current;
            return Import(csvPath, sourceAccountId, services.GetInstance<ICacheManager>(),
                services.GetInstance<ITradingStatisticsManager>(), () => SelectDemoAccount(
                    services.GetInstance<IConnectorsManager>(), chartAccountId));
        });

    internal static string SelectDemoAccount(IConnectorsManager manager, string? chartAccountId)
    {
        var accounts = manager.Connectors.Where(c => c.IsConnected)
            .SelectMany(c => c.Portfolios)
            .Where(p => !p.IsRealAccount && p.ConnectionState is null or ConnectionStates.Connected &&
                p.AccountID?.StartsWith("DEMO", StringComparison.OrdinalIgnoreCase) == true)
            .Select(p => p.AccountID).Distinct(StringComparer.Ordinal).Order(StringComparer.Ordinal).ToArray();
        if (chartAccountId != null && accounts.Contains(chartAccountId, StringComparer.Ordinal)) return chartAccountId;
        return accounts.Length switch
        {
            0 => throw new InvalidOperationException("请先连接 ATAS 的 DEMO 账户，再同步交易日志。"),
            1 => accounts[0],
            _ => throw new InvalidOperationException($"当前已连接多个 DEMO 账户，请在图表交易面板选中目标账户后再次同步（{string.Join("、", accounts)}）。")
        };
    }

    internal static async Task<(bool Success, string Message)> Import(string csvPath, string accountId,
        ICacheManager cache, ITradingStatisticsManager stats, Func<string> resolveTarget)
    {
        if (string.IsNullOrWhiteSpace(accountId) || accountId.Any(c => c is < '0' or > '9'))
            throw new ArgumentException("API 交易账号必须为数字。");
        var rows = ReviewStore.Parse(csvPath);
        if (rows.Any(t => !t.Ticket.StartsWith($"API-{accountId}-", StringComparison.Ordinal)) ||
            rows.Select(t => t.Ticket).Distinct(StringComparer.Ordinal).Count() != rows.Count)
            throw new InvalidDataException("API 来源账号或成交票号不一致。");
        var notes = new List<string>();
        var success = true;
        var state = States.GetOrCreateValue(cache);
        string? target = null;
        try
        {
            if (rows.Count == 0) return (true, "没有可同步的交易。");
            foreach (var symbol in rows.GroupBy(t => t.Symbol))
            {
                var market = CsvJournal.MarketFor(symbol.Key);
                int added = 0, verified = 0;
                foreach (var group in OverlappingTrades(symbol))
                {
                    // A native flat-to-flat match must include all overlapping source positions.
                    if (group.Any(t => !t.CloseUtc.HasValue))
                    {
                        notes.Add($"{symbol.Key}：等待重叠持仓全部平仓");
                        continue;
                    }
                    string label = $"{symbol.Key} {group[0].OpenUtc:MM-dd HH:mm} UTC";
                    if (market == null)
                    {
                        notes.Add($"{symbol.Key}：尚无日志合约映射");
                        success = false;
                        continue;
                    }
                    string? issue = group.Any(t => t.Ticket.Contains("-after-", StringComparison.Ordinal) ||
                        t.OriginalLots.HasValue && t.OriginalLots != t.Lots) ? "部分平仓需按原始成交拆分" :
                        group.Any(t => !CsvJournal.Covers(market, t)) ? "需要选择对应期货交割月" : null;
                    if (issue != null)
                    {
                        notes.Add($"{label}：{issue}");
                        success = false;
                        continue;
                    }
                    target ??= resolveTarget();
                    try
                    {
                        var key = (target, market.SecurityId, group[0].Ticket);
                        if (state.Groups.TryGetValue(key, out var previous) &&
                            previous.Source.SequenceEqual(group) && previous.Expected.IsStillComplete(cache))
                        {
                            verified += previous.Expected.ClosedTrades;
                            continue;
                        }
                        var batch = await JournalBatch.Prepare(market, group, cache, target).ConfigureAwait(false);
                        var expected = batch.CopyForVerification(cache);
                        var report = batch.Write(stats, () => state.RefreshPending = true);
                        state.Groups[key] = new VerifiedGroup(group, expected);
                        added += report.ExecutionsAdded;
                        verified += report.ClosedTrades;
                    }
                    catch (Exception error) when (error is InvalidDataException or InvalidOperationException)
                    {
                        notes.Add($"{label}：{error.Message}");
                        success = false;
                    }
                }
                if (verified > 0) notes.Add($"{target} / {market!.SecurityId}：新增 {added} 条成交，已核对 {verified} 笔平仓");
            }
        }
        finally
        {
            if (state.RefreshPending)
            {
                await cache.FlushAsync().ConfigureAwait(false);
                await stats.ReloadStatisticsAsync().ConfigureAwait(false);
                state.RefreshPending = false;
            }
        }
        return (success, string.Join("；", notes.Distinct()));
    }

    private static IEnumerable<Trade[]> OverlappingTrades(IEnumerable<Trade> trades)
    {
        var group = new List<Trade>();
        DateTime end = DateTime.MinValue;
        foreach (var trade in trades.OrderBy(t => t.OpenUtc).ThenBy(t => t.Ticket, StringComparer.Ordinal))
        {
            if (group.Count > 0 && trade.OpenUtc > end)
            {
                yield return group.ToArray();
                group.Clear();
            }
            var close = trade.CloseUtc ?? DateTime.MaxValue;
            if (group.Count == 0 || close > end) end = close;
            group.Add(trade);
        }
        if (group.Count > 0) yield return group.ToArray();
    }
}
