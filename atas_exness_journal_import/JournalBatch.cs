using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Threading.Tasks;
using ATAS.DataFeedsCore;
using ATAS.DataFeedsCore.TradeStatistics.Matching;
using Trade = ExnessReview.Trade;
using OFT.Platform.Core.Managers.Database;
using OFT.Platform.Core.Managers.Database.Registries;
using OFT.Platform.Core.Managers.Statistics;

namespace ExnessJournal;

internal sealed class JournalBatch
{
    // The one-time, preconfigured CSV import was authorized for this specific account.
    internal const string LegacyCsvAccount = "DEMO253F9";
    internal sealed record Market(string Symbol, string SecurityId, decimal DollarsPerPoint, decimal ContractsPerLot = 1m);
    internal static readonly Market Gold = new("XAUUSD", "GCZ6@COMEX", 100m);
    internal static readonly Market Bitcoin = new("BTCUSD", "BTCUSDT@BinanceFutures", 1m);
    internal static readonly Market Nasdaq = new("USTEC", "NQZ6@CME", 20m, 0.05m);
    internal sealed record Report(string Status, string Account, string SecurityId, int ExecutionsAdded,
        int ExecutionsVerified, int ClosedTrades, decimal JournalPnl, decimal CsvPnl);

    private readonly Market _market;
    private readonly string _account;
    private readonly MyTrade[] _fills;
    private readonly List<HistoryMyTrade> _matches;
    private readonly IMyTradesRegistry _executions;
    private readonly IHistoryMyTradeRegistry _history;
    private readonly decimal _csvPnl;

    private JournalBatch(string account, Market market, MyTrade[] fills, List<HistoryMyTrade> matches, ICacheManager cache, decimal csvPnl)
    {
        _market = market;
        _account = account;
        _fills = fills;
        _matches = matches;
        _csvPnl = csvPnl;
        _executions = cache.Registry<IMyTradesRegistry>();
        _history = cache.Registry<IHistoryMyTradeRegistry>();
    }

    internal static async Task<JournalBatch> Prepare(Market market, Trade[] rows, ICacheManager cache, string account)
    {
        if (rows.Length == 0 || rows.Any(t => t.Symbol != market.Symbol || !t.CloseUtc.HasValue || !t.Profit.HasValue))
            throw new InvalidDataException($"批次必须包括已平仓且有盈亏的 {market.Symbol} 记录。");
        if (rows.Select(t => t.Ticket).Distinct(StringComparer.Ordinal).Count() != rows.Length)
            throw new InvalidDataException($"批次中有重复的 {market.Symbol} 票号。");
        if (rows.Any(t => t.Commission.GetValueOrDefault() != 0 || t.Swap.GetValueOrDefault() != 0))
            throw new InvalidDataException("此日志导入器尚未映射非零佣金或隔夜利息。");
        var (portfolio, security) = ResolveMarket(cache, account, market);

        var executions = cache.Registry<IMyTradesRegistry>();
        var fills = rows.SelectMany(Executions)
            .OrderBy(t => t.Time).ThenBy(t => t.Id, StringComparer.Ordinal).ToArray();
        MyTrade[] Executions(Trade row)
        {
            var prefix = $"Exness.{market.Symbol}.{row.Ticket}";
            bool api = row.Ticket.StartsWith("API-", StringComparison.Ordinal);
            // CDB indexes execution IDs globally even though its lookup accepts an account.
            // Reuse pre-upgrade IDs only in the account that already owns the ticket.
            if (api && executions.TryGetMyTrade(account, prefix + ".O", true)?.AccountID != account &&
                executions.TryGetMyTrade(account, prefix + ".C", true)?.AccountID != account)
                prefix = $"Exness.{account}.{market.Symbol}.{row.Ticket}";
            return [Execution(row, true, prefix + ".O", api), Execution(row, false, prefix + ".C", api)];
        }
        MyTrade Execution(Trade row, bool open, string id, bool api)
        {
            var contracts = row.Lots * market.ContractsPerLot;
            return new MyTrade { AccountID = account, Portfolio = portfolio, SecurityId = market.SecurityId,
                Security = security, Id = id, OrderId = id, Route = api ? "Exness API" : "Exness CSV",
                Time = open ? row.OpenUtc : row.CloseUtc!.Value,
                OrderDirection = (row.Side == "buy") == open ? OrderDirections.Buy : OrderDirections.Sell,
                Price = open ? row.OpenPrice : row.ClosePrice!.Value, Volume = contracts, OpenVolume = contracts };
        }

        var matches = await Match(portfolio, security, fills);
        if (matches.Any(h => !h.IsComplete) ||
            matches.Select(h => (h.EnterTradeId,h.ExitTradeId)).Distinct().Count() != matches.Count)
            throw new InvalidDataException("原生配对结果不完整或重复。");
        var legs = matches.SelectMany(h => new[] { (Id: h.EnterTradeId!, Qty: Math.Abs(h.OpenVolume)),
                                                    (Id: h.ExitTradeId!, Qty: Math.Abs(h.CloseVolume)) })
            .GroupBy(leg => leg.Id).ToDictionary(g => g.Key, g => g.Sum(leg => leg.Qty));
        if (legs.Count != fills.Length || fills.Any(t => !legs.TryGetValue(t.Id, out var qty) || qty != t.Volume))
            throw new InvalidDataException("原生配对数量与成交不符。");
        decimal Gross(Trade t) => (t.Side == "buy" ? 1 : -1) * t.Lots *
            market.ContractsPerLot * (t.ClosePrice!.Value - t.OpenPrice) * market.DollarsPerPoint;
        var gross = rows.Sum(Gross);
        var reported = rows.Sum(t => t.Profit!.Value);
        if (Math.Abs(matches.Sum(h => h.PnL) - gross) > 0.000001m ||
            rows.Any(t => Math.Abs(Gross(t) - t.Profit!.Value) > 0.010001m))
            throw new InvalidDataException($"{market.Symbol} 原生配对盈亏、价格换算或 CSV 币值不一致。");

        var result = new JournalBatch(account, market, fills, matches, cache, reported);
        result.CheckExisting();
        return result;
    }

    private static (Portfolio Portfolio, Security Security) ResolveMarket(ICacheManager cache, string account, Market market)
    {
        var portfolio = cache.Registry<IPortfoliosRegistry>().GetById(account);
        var security = cache.Registry<ISecuritiesRegistry>().GetById(market.SecurityId);
        if (portfolio == null || portfolio.AccountID != account || portfolio.IsRealAccount || !account.StartsWith("DEMO", StringComparison.OrdinalIgnoreCase) ||
            security == null || security.SecurityId != market.SecurityId || security.TickSize <= 0 ||
            security.TickCost != market.DollarsPerPoint * security.TickSize)
            throw new InvalidOperationException($"DEMO 账户或 {market.SecurityId} 合约规格与导入设置不符。");
        return (portfolio, security);
    }

    // ATAS registries may keep the exact objects passed to Save/Add. Retain separate
    // expected values so an edit in Journal cannot also change our comparison baseline.
    internal JournalBatch CopyForVerification(ICacheManager cache) =>
        new(_account, _market, _fills.Select(fill => fill.Clone()).ToArray(),
            _matches.Select(match => match.Clone()).ToList(), cache, _csvPnl);

    internal int ClosedTrades => _matches.Count;

    internal bool IsStillComplete(ICacheManager cache)
    {
        ResolveMarket(cache, _account, _market);
        return CheckExisting() && _matches.All(match =>
        {
            var saved = _history.TryGetBySourceTrades(_account, match.EnterTradeId, match.ExitTradeId);
            if (saved == null) return false;
            VerifyMatch(match, saved);
            return true;
        });
    }

    private bool CheckExisting()
    {
        var ids = _fills.Select(t => t.Id).ToHashSet(StringComparer.Ordinal);
        var unrelated = _executions.GetMyTrades(_account).Where(t => t.SecurityId == _market.SecurityId && !ids.Contains(t.Id));
        // A separate flat historical period can coexist in the same account/instrument.
        // Mixing fills during the imported period would change native position matching.
        var earlierNet = 0m;
        foreach (var t in unrelated)
        {
            if (t.Time < _fills[0].Time) earlierNet += t.OrderDirection == OrderDirections.Buy ? t.Volume : -t.Volume;
            else if (t.Time <= _fills[^1].Time) throw new InvalidOperationException($"{_market.SecurityId} 导入时段内另有成交。");
        }
        if (earlierNet != 0) throw new InvalidOperationException($"{_market.SecurityId} 在本批开始时已有持仓。");

        bool settled = true;
        foreach (var fill in _fills)
        {
            var saved = _executions.TryGetMyTrade(_account, fill.Id, true);
            if (saved != null) VerifyExecution(fill, saved);
            settled &= saved?.OpenVolume == 0;
        }
        var expected = _matches.ToDictionary(h => (h.EnterTradeId, h.ExitTradeId));
        foreach (var saved in _history.GetHistoryTrades(_fills[0].Time.Date, _fills[^1].Time.Date.AddDays(1), [_account], [_market.SecurityId]))
        {
            if (!ids.Contains(saved.EnterTradeId ?? "") && !ids.Contains(saved.ExitTradeId ?? "")) continue;
            if (!expected.TryGetValue((saved.EnterTradeId, saved.ExitTradeId), out var match))
                throw new InvalidDataException("已有历史记录与本批的原生配对冲突。");
            VerifyMatch(match, saved);
        }
        return settled;
    }

    internal Report Write(ITradingStatisticsManager stats, Action? onMutation = null)
    {
        CheckExisting();
        int added = 0;
        foreach (var fill in _fills)
        {
            var saved = _executions.TryGetMyTrade(_account, fill.Id, true);
            if (saved != null) { VerifyExecution(fill, saved); continue; }
            onMutation?.Invoke();
            stats.Add(fill);
            added++;
        }
        foreach (var h in _matches)
        {
            var saved = _history.TryGetBySourceTrades(_account, h.EnterTradeId, h.ExitTradeId);
            if (saved == null)
            {
                onMutation?.Invoke();
                _history.Save(h);
            }
            else VerifyMatch(h, saved);
        }
        foreach (var fill in _fills)
        {
            var saved = _executions.TryGetMyTrade(_account, fill.Id, true);
            VerifyExecution(fill, saved);
            if (saved!.OpenVolume == 0) continue;
            onMutation?.Invoke();
            saved.OpenVolume = 0;
            _executions.Save(saved);
        }
        foreach (var h in _matches)
            VerifyMatch(h, _history.TryGetBySourceTrades(_account, h.EnterTradeId, h.ExitTradeId));
        return new Report("complete", _account, _market.SecurityId, added, _fills.Length, _matches.Count,
            _matches.Sum(h => h.PnL), _csvPnl);
    }

    private static async Task<List<HistoryMyTrade>> Match(Portfolio portfolio, Security security, MyTrade[] source)
    {
        var copies = source.Select(t => t.Clone()).ToArray();
        var matches = new List<HistoryMyTrade>();
        using var processor = new TradesMatchingProcessor((_, _, from, to) =>
            Task.FromResult<IReadOnlyCollection<MyTrade>>(copies.Where(t => t.Time >= from && t.Time <= to).ToArray()),
            (portfolio, security), unprocessedTrades: copies, forceRecovery: true);
        var complete = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        processor.NewTrade += matches.Add;
        processor.HistoryCalculationCompleted += () => complete.TrySetResult();
        processor.Start();
        await complete.Task.WaitAsync(TimeSpan.FromSeconds(30));
        return matches;
    }

    private static void VerifyExecution(MyTrade expected, MyTrade? saved)
    {
        if (saved == null || saved.AccountID != expected.AccountID || saved.SecurityId != expected.SecurityId ||
            saved.Price != expected.Price || saved.Volume != expected.Volume || !SameStoredTime(expected.Time, saved.Time) ||
            saved.OrderDirection != expected.OrderDirection || saved.OrderId != expected.OrderId)
            throw new InvalidDataException("成交与 CSV 不一致：" + expected.Id);
    }

    private static void VerifyMatch(HistoryMyTrade expected, HistoryMyTrade? saved)
    {
        if (saved == null || saved.AccountID != expected.AccountID || saved.SecurityId != expected.SecurityId || !saved.IsComplete ||
            !SameStoredTime(expected.OpenTime, saved.OpenTime) || !SameStoredTime(expected.CloseTime, saved.CloseTime) ||
            saved.OpenPrice != expected.OpenPrice || saved.ClosePrice != expected.ClosePrice ||
            saved.OpenVolume != expected.OpenVolume || saved.CloseVolume != expected.CloseVolume || saved.PnL != expected.PnL)
            throw new InvalidDataException($"历史配对与原生计算不一致：{expected.EnterTradeId} / {expected.ExitTradeId}");
    }

    // The native CDB writer persists timestamps to whole seconds; live deal objects can retain fractions.
    private static bool SameStoredTime(DateTime? expected, DateTime? stored) =>
        expected == stored || (expected.HasValue && stored.HasValue &&
            expected.Value.Ticks - expected.Value.Ticks % TimeSpan.TicksPerSecond == stored.Value.Ticks);
}
