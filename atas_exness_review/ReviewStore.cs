using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Threading;
using Microsoft.VisualBasic.FileIO;

namespace ExnessReview;

public sealed record Trade(string Ticket, DateTime OpenUtc, DateTime? CloseUtc, string Side,
    decimal Lots, decimal? OriginalLots, string Symbol, decimal OpenPrice, decimal? ClosePrice,
    decimal? Profit, string CloseReason, decimal? StopLoss, decimal? TakeProfit,
    decimal? Commission, decimal? Swap);

public sealed record ImportReport(int Added, int Updated, int Skipped, int Conflicts);
public sealed record ApiImportReport(int Added, int Updated, int Skipped, int Removed);

public static class ReviewStore
{
    private const int Schema = 1;
    private static readonly ConcurrentDictionary<string, Slot> Slots = new(StringComparer.OrdinalIgnoreCase);
    private static readonly CultureInfo Invariant = CultureInfo.InvariantCulture;
    private static readonly string[] TimeFormats =
        { "yyyy-MM-dd'T'HH:mm:ssK", "yyyy-MM-dd'T'HH:mm:ss.FFFFFFFK",
          "yyyy-MM-dd HH:mm:ssK", "yyyy-MM-dd HH:mm:ss.FFFFFFFK" };

    private sealed class Slot
    {
        public readonly object Gate = new();
        public IReadOnlyList<Trade>? Published;
    }

    private sealed record Archive(int Version, string Account, Trade[] Trades, Trade[] Conflicts);

    public static string AccountFile(string account)
    {
        var alias = NormalizeAlias(account);
        var root = Environment.GetEnvironmentVariable("EXNESS_REVIEW_STORE_DIR");
        if (string.IsNullOrWhiteSpace(root))
            root = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "AtasExnessReview");
        var digest = SHA256.HashData(Encoding.UTF8.GetBytes(alias.ToUpperInvariant()));
        return Path.GetFullPath(Path.Combine(root, Convert.ToHexString(digest) + ".json"));
    }

    private static string NormalizeAlias(string account)
    {
        if (string.IsNullOrWhiteSpace(account))
            throw new ArgumentException("请指定复盘账户别名。", nameof(account));
        return account.Trim().Normalize();
    }

    public static Trade[] Read(string account) => Snapshot(account).ToArray();

    public static IReadOnlyList<Trade> Snapshot(string account)
    {
        var path = AccountFile(account);
        var slot = Slots.GetOrAdd(path, _ => new Slot());
        var published = Volatile.Read(ref slot.Published);
        if (published != null) return published;
        lock (slot.Gate)
        {
            if (slot.Published != null) return slot.Published;
            var trades = Array.AsReadOnly(Load(path, NormalizeAlias(account)).Trades);
            Volatile.Write(ref slot.Published, trades);
            return trades;
        }
    }

    public static ApiImportReport ImportApiSnapshot(string account, string sourceAccountId, string path, int expectedRows,
        IReadOnlySet<string>? unmatchedPositionIds = null)
    {
        if (string.IsNullOrEmpty(sourceAccountId) || sourceAccountId.Length > 20 || !sourceAccountId.All(char.IsAsciiDigit))
            throw new InvalidDataException("API 同步结果缺少有效的来源账户。");
        var prefix = $"API-{sourceAccountId}-";
        bool KeepPrevious(string ticket)
        {
            if (unmatchedPositionIds == null || unmatchedPositionIds.Count == 0) return false;
            var end = ticket.IndexOf('-', prefix.Length);
            return end > prefix.Length && unmatchedPositionIds.Contains(ticket[prefix.Length..end]);
        }
        var rows = Parse(path);
        if (rows.Count != expectedRows)
            throw new InvalidDataException("API 成交文件行数与同步结果不符。");
        var incoming = new Dictionary<string, Trade>(StringComparer.Ordinal);
        foreach (var trade in rows)
        {
            if (!trade.Ticket.StartsWith(prefix, StringComparison.Ordinal) || trade.Ticket.Length == prefix.Length)
                throw new InvalidDataException("API 成交文件包含其他来源的票号。");
            if (!incoming.TryAdd(trade.Ticket, trade))
                throw new InvalidDataException("API 成交文件包含重复票号。");
        }
        foreach (var ticket in incoming.Keys.Where(KeepPrevious).ToArray()) incoming.Remove(ticket);

        var alias = NormalizeAlias(account);
        var storagePath = AccountFile(alias);
        var slot = Slots.GetOrAdd(storagePath, _ => new Slot());
        lock (slot.Gate)
        {
            Directory.CreateDirectory(Path.GetDirectoryName(storagePath)!);
            using var importLock = new FileStream(storagePath + ".lock", FileMode.OpenOrCreate, FileAccess.Write, FileShare.None);
            var archive = Load(storagePath, alias);
            var otherTrades = new List<Trade>();
            var previous = new Dictionary<string, Trade>(StringComparer.Ordinal);
            foreach (var trade in archive.Trades)
            {
                if (trade.Ticket.StartsWith(prefix, StringComparison.Ordinal) && !KeepPrevious(trade.Ticket)) previous.Add(trade.Ticket, trade);
                else otherTrades.Add(trade);
            }
            int added = 0, updated = 0, skipped = 0;
            foreach (var (ticket, trade) in incoming)
            {
                if (!previous.Remove(ticket, out var old)) added++;
                else if (trade == old) skipped++;
                else updated++;
            }
            var removed = previous.Count;
            if (added + updated + removed > 0)
            {
                archive = archive with { Trades = otherTrades.Concat(incoming.Values)
                    .OrderBy(t => t.OpenUtc).ThenBy(t => t.Ticket, StringComparer.Ordinal).ToArray() };
                Save(storagePath, archive);
            }
            if (slot.Published == null || !slot.Published.SequenceEqual(archive.Trades))
                Volatile.Write(ref slot.Published, Array.AsReadOnly(archive.Trades));
            return new ApiImportReport(added, updated, skipped, removed);
        }
    }

    public static ImportReport Import(string account, string path)
    {
        var rows = Parse(path);
        var alias = NormalizeAlias(account);
        var storagePath = AccountFile(alias);
        var slot = Slots.GetOrAdd(storagePath, _ => new Slot());
        lock (slot.Gate)
        {
            Directory.CreateDirectory(Path.GetDirectoryName(storagePath)!);
            using var importLock = new FileStream(storagePath + ".lock", FileMode.OpenOrCreate, FileAccess.Write, FileShare.None);
            var archive = Load(storagePath, alias);
            var indexed = archive.Trades.ToDictionary(trade => trade.Ticket, StringComparer.Ordinal);
            var conflicts = archive.Conflicts.ToHashSet();
            int added = 0, updated = 0, skipped = 0, conflicting = 0;
            foreach (var incoming in rows)
            {
                if (!indexed.TryGetValue(incoming.Ticket, out var existing))
                {
                    indexed.Add(incoming.Ticket, incoming);
                    added++;
                }
                else if (incoming == existing || conflicts.Contains(incoming))
                    skipped++;
                else if (SameOpening(existing, incoming) && existing.CloseUtc == null && incoming.CloseUtc != null)
                {
                    indexed[incoming.Ticket] = incoming;
                    updated++;
                }
                else if (SameOpening(existing, incoming) && existing.CloseUtc != null && incoming.CloseUtc == null)
                    skipped++;
                else
                {
                    conflicts.Add(incoming);
                    conflicting++;
                }
            }

            if (added + updated + conflicting > 0)
            {
                archive = new Archive(Schema, archive.Account,
                    indexed.Values.OrderBy(trade => trade.OpenUtc).ThenBy(trade => trade.Ticket, StringComparer.Ordinal).ToArray(),
                    conflicts.ToArray());
                Save(storagePath, archive);
            }
            Volatile.Write(ref slot.Published, Array.AsReadOnly(archive.Trades));
            return new ImportReport(added, updated, skipped, conflicting);
        }
    }

    private static bool SameOpening(Trade first, Trade second) =>
        first.OpenUtc == second.OpenUtc && first.OpenPrice == second.OpenPrice &&
        first.Symbol == second.Symbol && first.Side == second.Side &&
        (first.OriginalLots ?? first.Lots) == (second.OriginalLots ?? second.Lots);

    private static Archive Load(string path, string account)
    {
        if (!File.Exists(path)) return new Archive(Schema, account, Array.Empty<Trade>(), Array.Empty<Trade>());
        using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read | FileShare.Delete);
        var archive = JsonSerializer.Deserialize<Archive>(stream);
        if (archive is null || archive.Version != Schema || archive.Trades is null || archive.Conflicts is null ||
            !string.Equals(archive.Account, account, StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("本地成交库的格式、版本或账户别名不匹配：" + path);
        return archive;
    }

    private static void Save(string path, Archive archive)
    {
        var temp = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
        try
        {
            using (var stream = new FileStream(temp, FileMode.CreateNew, FileAccess.Write, FileShare.None))
            {
                JsonSerializer.Serialize(stream, archive);
                stream.Flush(true);
            }
            File.Move(temp, path, true);
        }
        finally { if (File.Exists(temp)) File.Delete(temp); }
    }

    internal static IReadOnlyList<Trade> Parse(string path)
    {
        using var reader = new TextFieldParser(path, new UTF8Encoding(false, true), true)
            { TextFieldType = FieldType.Delimited, HasFieldsEnclosedInQuotes = true, TrimWhiteSpace = true };
        reader.SetDelimiters(",");
        var header = reader.ReadFields() ?? throw new InvalidDataException("CSV 没有表头。");
        var columns = new Dictionary<string, int>(StringComparer.OrdinalIgnoreCase);
        for (var index = 0; index < header.Length; index++)
            if (!columns.TryAdd(header[index].Trim(), index))
                throw new InvalidDataException("CSV 表头有重复列：" + header[index]);
        foreach (var required in new[] { "ticket", "opening_time_utc", "closing_time_utc", "type", "lots",
                     "symbol", "opening_price", "closing_price", "profit" })
            if (!columns.ContainsKey(required)) throw new InvalidDataException("CSV 缺少列：" + required);

        var rows = new List<Trade>();
        while (!reader.EndOfData)
        {
            var row = reader.LineNumber;
            var fields = reader.ReadFields();
            if (fields is null) break;
            InvalidDataException Invalid(string reason) => new($"CSV 第 {row} 行：{reason}");
            if (fields.Length != header.Length) throw Invalid("列数与表头不一致。");
            string Field(string name) => columns.TryGetValue(name, out var index) ? fields[index].Trim() : "";
            string Required(string name) => Field(name) is { Length: > 0 } value ? value : throw Invalid(name + " 不能为空。");
            decimal? Number(string name, bool required = false)
            {
                var value = required ? Required(name) : Field(name);
                if (value.Length == 0) return null;
                return decimal.TryParse(value, NumberStyles.AllowLeadingSign | NumberStyles.AllowDecimalPoint,
                    Invariant, out var result) ? result : throw Invalid(name + " 不是有效数字。");
            }
            DateTime? Time(string name, bool required = false)
            {
                var value = required ? Required(name) : Field(name);
                if (value.Length == 0) return null;
                return DateTimeOffset.TryParseExact(value, TimeFormats, Invariant,
                    DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal, out var result)
                    ? result.UtcDateTime : throw Invalid(name + " 不是有效时间。");
            }

            var side = Required("type").ToLowerInvariant();
            if (side is not ("buy" or "sell")) throw Invalid("type 必须为 buy 或 sell。");
            var lots = Number("lots", true)!.Value;
            if (lots <= 0) throw Invalid("lots 必须大于零。");
            var open = Time("opening_time_utc", true)!.Value;
            var close = Time("closing_time_utc");
            if (close < open) throw Invalid("平仓时间早于开仓时间。");
            var closingPrice = Number("closing_price");
            if (close.HasValue != closingPrice.HasValue) throw Invalid("平仓时间与平仓价必须同时存在或同时为空。");
            rows.Add(new Trade(Required("ticket"), open, close, side, lots, Number("original_position_size"),
                Required("symbol").ToUpperInvariant(), Number("opening_price", true)!.Value, closingPrice,
                Number("profit"), Field("close_reason").ToLowerInvariant(), Number("stop_loss"),
                Number("take_profit"), Number("commission"), Number("swap")));
        }
        return rows;
    }
}
