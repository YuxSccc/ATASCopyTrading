using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.RegularExpressions;

namespace ExnessReview;

public sealed record Mark(DateTime Utc, string Label, bool Below, Trade Trade, bool IsExit);

public static partial class Projection
{
    [GeneratedRegex(@"^(?:(?:COMEX|CME|CME_MINI|GLOBEX|NYMEX)[:./])?[/@#]?(?<root>GC|NQ|ES)(?:(?:\s*[FGHJKMNQUVXZ]\s*\d{1,4})|(?:[12]!)|(?:\s+\d{1,2}[-/]\d{2,4})|(?:\s*\(?(?:CONTINUOUS|CONT)\)?))?(?:\s*(?:[@:.](?:CME|COMEX|CME_MINI|GLOBEX|NYMEX)|\((?:CME|COMEX|CME_MINI|GLOBEX|NYMEX)\)))?$", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant)]
    private static partial Regex ContractPattern();

    public static string? SourceSymbol(string chartInstrument, string? exchange = null)
    {
        if (string.IsNullOrWhiteSpace(chartInstrument)) return null;
        var code = chartInstrument.Trim().TrimStart('#');
        var parts = code.Split('@');
        if (parts[0].Equals("BTCUSDT", StringComparison.OrdinalIgnoreCase))
        {
            static bool BinanceFutures(string? venue) =>
                string.Equals(venue?.Replace(" ", ""), "BinanceFutures", StringComparison.OrdinalIgnoreCase);
            bool matched = parts.Length == 1 ? BinanceFutures(exchange)
                : parts.Length == 2 && BinanceFutures(parts[1]) &&
                  (string.IsNullOrWhiteSpace(exchange) || BinanceFutures(exchange));
            return matched ? "BTCUSD" : null;
        }
        var root = ContractPattern().Match(chartInstrument.Trim()).Groups["root"].Value.ToUpperInvariant();
        return root switch { "GC" => "XAUUSD", "NQ" => "USTEC", "ES" => "US500", _ => null };
    }

    public static Mark[] Events(IEnumerable<Trade> trades, string sourceSymbol)
    {
        var marks = new List<Mark>();
        foreach (var trade in trades)
        {
            if (!MatchesSource(trade.Symbol, sourceSymbol)) continue;
            var buy = string.Equals(trade.Side, "buy", StringComparison.OrdinalIgnoreCase);
            marks.Add(new Mark(trade.OpenUtc, buy ? "开多" : "开空", buy, trade, false));
            if (trade.CloseUtc is not { } closed) continue;
            var label = trade.CloseReason.Trim().ToLowerInvariant() switch
            {
                "sl" or "stop loss" or "stop_loss" => "止损",
                "tp" or "take profit" or "take_profit" => "止盈",
                "so" or "stop_out" or "stop out" => "强平",
                _ => buy ? "平多" : "平空"
            };
            marks.Add(new Mark(closed, label, !buy, trade, true));
        }
        return marks.OrderBy(mark => mark.Utc).ThenBy(mark => mark.IsExit)
            .ThenBy(mark => mark.Trade.Ticket, StringComparer.Ordinal).ToArray();
    }

    private static bool MatchesSource(string symbol, string source)
    {
        if (source is not ("XAUUSD" or "USTEC" or "US500" or "BTCUSD")) return false;
        var value = symbol.Trim();
        if (source == "USTEC" && value.Equals("USTEC_x100", StringComparison.OrdinalIgnoreCase)) return true;
        if (!value.StartsWith(source, StringComparison.OrdinalIgnoreCase)) return false;
        var suffix = value[source.Length..].ToUpperInvariant();
        return suffix is "" or "M" or "C" or "R" or "Z" or "A" or "B" or ".M" or ".C" or ".R" or ".Z" or ".A" or ".B";
    }

    public static string QuantityLabel(Trade trade)
    {
        var symbol = trade.Symbol.Trim().ToUpperInvariant();
        var lots = trade.Lots;
        var original = $"{lots:G29} 手";
        if (MatchesSource(symbol, "BTCUSD"))
        {
            var btc = symbol.EndsWith('C') ? lots / 100m : lots;
            return $"{original}（{btc:G29} BTC）";
        }
        if (!MatchesSource(symbol, "USTEC") || symbol is "USTECC" or "USTEC.C") return original;
        var contracts = symbol == "USTEC_X100" ? lots * 5m : lots / 20m;
        return $"{original}（等值 {contracts:G29} NQ）";
    }
}
