using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text;
using ATAS.Indicators;

namespace AtasTradeExportIndicator
{
    [Category("Custom")]
    [DisplayName("AtasTradeExport")]
    public sealed class AtasTradeExportIndicator : Indicator
    {
        private const string Level1Header = "time,index,price,volume,side,parent_id";
        private const string Level1FallbackHeader = "time,index,first_price,last_price,volume,side,parent_id,tick_count";
        private const string FootprintHeader = "time,bar_index,price,bid,ask,between,volume,ticks";
        private readonly object _fileLock = new object();
        private readonly string _sessionId = DateTime.UtcNow.ToString("yyyyMMdd_HHmmss_fff", CultureInfo.InvariantCulture);
        private readonly HashSet<string> _footprintKeys = new HashSet<string>();
        private readonly Queue<HistoricalRange> _pendingHistoricalLevel1Ranges = new Queue<HistoricalRange>();
        private long _level1Seq;
        private long _parentSeq;
        private long _finishRecalculateCount;
        private long _cumulativeResponseCount;
        private long _historicalLevel1RequestCount;
        private long _historicalLevel1CompletedRequestCount;
        private long _historicalLevel1TickCount;
        private long _historicalLevel1FallbackCount;
        private long _writeErrorCount;
        private DateTime? _requestedLevel1Start;
        private DateTime? _requestedLevel1End;
        private bool _historicalLevel1RequestInFlight;
        private bool _syncStarted;

        public AtasTradeExportIndicator()
            : base(true)
        {
        }

        [ATAS.Indicators.ParameterAttribute]
        [DisplayName("Instrument override")]
        public string InstrumentOverride { get; set; } = "";

        [ATAS.Indicators.ParameterAttribute]
        [DisplayName("File prefix")]
        public string FilePrefix { get; set; } = "atas_export";

        [ATAS.Indicators.ParameterAttribute]
        [DisplayName("Footprint directory")]
        public string FootprintDirectory { get; set; } = "";

        [ATAS.Indicators.ParameterAttribute]
        [DisplayName("Level1 directory")]
        public string Level1Directory { get; set; } = "";

        [ATAS.Indicators.ParameterAttribute]
        [DisplayName("Export footprint")]
        public bool ExportFootprint { get; set; } = false;

        [ATAS.Indicators.ParameterAttribute]
        [DisplayName("Export online Level1 trades")]
        public bool ExportOnlineLevel1Trades { get; set; } = true;

        [ATAS.Indicators.ParameterAttribute]
        [DisplayName("Start sync")]
        public bool StartSync { get; set; } = false;

        [ATAS.Indicators.ParameterAttribute]
        [DisplayName("Use explicit date range")]
        public bool UseExplicitDateRange { get; set; } = true;

        [ATAS.Indicators.ParameterAttribute]
        [DisplayName("Start date yyyy-MM-dd")]
        public string StartDate { get; set; } = "";

        [ATAS.Indicators.ParameterAttribute]
        [DisplayName("End date yyyy-MM-dd")]
        public string EndDate { get; set; } = "";

        [ATAS.Indicators.ParameterAttribute]
        [DisplayName("Skip existing daily files")]
        public bool SkipExistingDailyFiles { get; set; } = true;

        [ATAS.Indicators.ParameterAttribute]
        [DisplayName("Request historical Level1 trades")]
        public bool RequestHistoricalLevel1Trades { get; set; } = true;

        [ATAS.Indicators.ParameterAttribute]
        [DisplayName("Max historical bars, 0 = all")]
        public int MaxHistoricalBars { get; set; } = 0;

        [ATAS.Indicators.ParameterAttribute]
        [DisplayName("Historical Level1 chunk hours")]
        public int HistoricalLevel1ChunkHours { get; set; } = 24;

        [ATAS.Indicators.ParameterAttribute]
        [DisplayName("Write fallback if historical ticks missing")]
        public bool WriteFallbackIfHistoricalTicksMissing { get; set; } = true;

        [ATAS.Indicators.ParameterAttribute]
        [DisplayName("Debug log enabled")]
        public bool DebugLogEnabled { get; set; } = true;

        [ATAS.Indicators.ParameterAttribute]
        [DisplayName("Status file enabled")]
        public bool StatusFileEnabled { get; set; } = true;

        protected override void OnCalculate(int bar, decimal value)
        {
            TryStartExplicitSync();
        }

        protected override void OnNewTrade(MarketDataArg trade)
        {
            if (!ExportOnlineLevel1Trades)
                return;

            WriteLevel1Trade(trade, null);
        }

        protected override void OnNewTrades(IEnumerable<MarketDataArg> trades)
        {
            if (!ExportOnlineLevel1Trades)
                return;

            foreach (var trade in trades)
                WriteLevel1Trade(trade, null);
        }

        protected override void OnCumulativeTradesResponse(
            CumulativeTradesRequest request,
            IEnumerable<CumulativeTrade> cumulativeTrades)
        {
            _historicalLevel1RequestInFlight = false;

            var tradeCount = 0;
            var tickCount = 0;
            var fallbackCount = 0;
            try
            {
                foreach (var cumulativeTrade in cumulativeTrades)
                {
                    tradeCount++;
                    var parentId = NextParentId();
                    if (cumulativeTrade.Ticks != null && cumulativeTrade.Ticks.Count > 0)
                    {
                        tickCount += cumulativeTrade.Ticks.Count;
                        for (var i = 0; i < cumulativeTrade.Ticks.Count; i++)
                            WriteLevel1Trade(cumulativeTrade.Ticks[i], parentId);

                        continue;
                    }

                    if (WriteFallbackIfHistoricalTicksMissing)
                    {
                        fallbackCount++;
                        WriteHistoricalLevel1Fallback(cumulativeTrade, parentId);
                    }
                }
            }
            catch (Exception ex)
            {
                LogWriteError($"OnCumulativeTradesResponse processing error: {ex}");
            }

            LogDebug(
                $"OnCumulativeTradesResponse #{++_cumulativeResponseCount}: " +
                $"cumulativeTrades={tradeCount}, ticks={tickCount}, fallbacks={fallbackCount}, " +
                $"queuedRanges={_pendingHistoricalLevel1Ranges.Count}, level1Rows={_level1Seq}");
            _historicalLevel1CompletedRequestCount++;
            _historicalLevel1TickCount += tickCount;
            _historicalLevel1FallbackCount += fallbackCount;
            WriteStatus("historical_response");
            TryRequestNextHistoricalLevel1Range();
        }

        protected override void OnFinishRecalculate()
        {
            if (CurrentBar <= 0)
            {
                LogDebug($"OnFinishRecalculate #{++_finishRecalculateCount}: CurrentBar={CurrentBar}, ignored");
                TryStartExplicitSync();
                return;
            }

            var fromBar = MaxHistoricalBars <= 0
                ? 0
                : Math.Max(0, CurrentBar - MaxHistoricalBars);
            var start = GetCandle(fromBar)?.Time;
            var end = GetCandle(CurrentBar - 1)?.LastTime;

            LogDebug(
                $"OnFinishRecalculate #{++_finishRecalculateCount}: " +
                $"CurrentBar={CurrentBar}, fromBar={fromBar}, start={start:O}, end={end:O}, " +
                $"ExportFootprint={ExportFootprint}, RequestHistoricalLevel1Trades={RequestHistoricalLevel1Trades}");
            WriteStatus("finish_recalculate");

            if (ExportFootprint)
                ExportFootprintLevels(fromBar);

            if (UseExplicitDateRange)
            {
                TryStartExplicitSync();
            }
            else if (RequestHistoricalLevel1Trades && StartSync)
            {
                if (start.HasValue && end.HasValue)
                    RequestMissingHistoricalLevel1Ranges(start.Value, end.Value);
                else
                    LogDebug("Historical Level1 request skipped because start/end candle time is missing");
            }
        }

        private void TryStartExplicitSync()
        {
            if (_syncStarted || !StartSync || !RequestHistoricalLevel1Trades || !UseExplicitDateRange)
                return;

            if (!TryParseDateRange(out var start, out var end, out var error))
            {
                LogDebug($"Explicit sync not started: {error}");
                WriteStatus("explicit_sync_invalid_range");
                return;
            }

            _syncStarted = true;
            LogDebug($"Explicit sync started: start={start:O}, end={end:O}, skipExistingDailyFiles={SkipExistingDailyFiles}");
            RequestMissingHistoricalLevel1Ranges(start, end);
            WriteStatus("explicit_sync_started");
        }

        private bool TryParseDateRange(out DateTime start, out DateTime end, out string error)
        {
            start = default;
            end = default;
            error = "";

            if (!TryParseDate(StartDate, isEnd: false, out start))
            {
                error = $"invalid Start date: {StartDate}";
                return false;
            }

            if (!TryParseDate(EndDate, isEnd: true, out end))
            {
                error = $"invalid End date: {EndDate}";
                return false;
            }

            if (end <= start)
            {
                error = $"End must be greater than Start: start={start:O}, end={end:O}";
                return false;
            }

            return true;
        }

        private static bool TryParseDate(string value, bool isEnd, out DateTime result)
        {
            result = default;
            if (string.IsNullOrWhiteSpace(value))
                return false;

            var formats = new[]
            {
                "yyyy-MM-dd",
                "yyyy-MM-dd HH:mm:ss",
                "yyyy-MM-ddTHH:mm:ss",
                "yyyy-MM-ddTHH:mm:ss.fffffff"
            };

            if (!DateTime.TryParseExact(
                    value.Trim(),
                    formats,
                    CultureInfo.InvariantCulture,
                    DateTimeStyles.AssumeLocal,
                    out result))
            {
                return false;
            }

            if (isEnd && value.Trim().Length == "yyyy-MM-dd".Length)
                result = result.Date.AddDays(1);

            return true;
        }

        private void RequestMissingHistoricalLevel1Ranges(DateTime loadedStart, DateTime loadedEnd)
        {
            if (!_requestedLevel1Start.HasValue || !_requestedLevel1End.HasValue)
            {
                RequestHistoricalLevel1Range(loadedStart, loadedEnd);
                _requestedLevel1Start = loadedStart;
                _requestedLevel1End = loadedEnd;
                return;
            }

            if (loadedStart < _requestedLevel1Start.Value)
            {
                RequestHistoricalLevel1Range(loadedStart, _requestedLevel1Start.Value);
                _requestedLevel1Start = loadedStart;
            }

            if (loadedEnd > _requestedLevel1End.Value)
            {
                RequestHistoricalLevel1Range(_requestedLevel1End.Value, loadedEnd);
                _requestedLevel1End = loadedEnd;
            }
        }

        private void RequestHistoricalLevel1Range(DateTime start, DateTime end)
        {
            if (end <= start)
                return;

            var chunkHours = Math.Max(1, HistoricalLevel1ChunkHours);
            var dayCursor = start.Date;
            while (dayCursor < end)
            {
                var dayStart = dayCursor < start ? start : dayCursor;
                var dayEnd = dayCursor.AddDays(1) > end ? end : dayCursor.AddDays(1);
                var outputDate = dayStart.Date;

                if (SkipExistingDailyFiles && Level1DailyFileExists(outputDate))
                {
                    LogDebug($"Skipping existing daily Level1 file: date={outputDate:yyyy-MM-dd}, path={GetLevel1OutputPath(outputDate)}");
                    dayCursor = dayCursor.AddDays(1);
                    continue;
                }

                var cursor = dayStart;
                while (cursor < dayEnd)
                {
                    var next = cursor.AddHours(chunkHours);
                    if (next > dayEnd)
                        next = dayEnd;

                    _pendingHistoricalLevel1Ranges.Enqueue(new HistoricalRange(cursor, next));
                    cursor = next;
                }

                dayCursor = dayCursor.AddDays(1);
            }

            LogDebug(
                $"Queued historical Level1 ranges: start={start:O}, end={end:O}, " +
                $"chunkHours={chunkHours}, queuedRanges={_pendingHistoricalLevel1Ranges.Count}, " +
                $"inFlight={_historicalLevel1RequestInFlight}");
            TryRequestNextHistoricalLevel1Range();
        }

        private void TryRequestNextHistoricalLevel1Range()
        {
            if (_historicalLevel1RequestInFlight || _pendingHistoricalLevel1Ranges.Count == 0)
                return;

            var range = _pendingHistoricalLevel1Ranges.Dequeue();
            var request = new CumulativeTradesRequest(range.Start, range.End, 0, 0);
            _historicalLevel1RequestInFlight = true;
            _historicalLevel1RequestCount++;
            LogDebug(
                $"RequestForCumulativeTrades: start={range.Start:O}, end={range.End:O}, " +
                $"remainingQueuedRanges={_pendingHistoricalLevel1Ranges.Count}");
            WriteStatus("historical_request_started");
            RequestForCumulativeTrades(request);
        }

        private void ExportFootprintLevels(int fromBar)
        {
            for (var bar = fromBar; bar < CurrentBar; bar++)
            {
                var candle = GetCandle(bar);
                if (candle == null)
                    continue;

                var levels = candle.GetAllPriceLevels();
                if (levels == null)
                    continue;

                foreach (var level in levels)
                {
                    if (level == null)
                        continue;

                    var key = $"{candle.Time:O}|{level.Price.ToString(CultureInfo.InvariantCulture)}";
                    if (!_footprintKeys.Add(key))
                        continue;

                    WriteFootprintLevel(bar, candle, level);
                }
            }
        }

        private void WriteLevel1Trade(MarketDataArg trade, long? parentId)
        {
            var line = string.Join(",",
                Csv(trade.Time.ToString("O")),
                (++_level1Seq).ToString(CultureInfo.InvariantCulture),
                Dec(trade.Price),
                Dec(trade.Volume),
                Csv(trade.Direction.ToString()),
                parentId?.ToString(CultureInfo.InvariantCulture) ?? "");

            WriteCsvLine(GetLevel1OutputPath(trade.Time.Date), Level1Header, line);
        }

        private void WriteHistoricalLevel1Fallback(CumulativeTrade trade, long parentId)
        {
            var line = string.Join(",",
                Csv(trade.Time.ToString("O")),
                (++_level1Seq).ToString(CultureInfo.InvariantCulture),
                Dec(trade.FirstPrice),
                Dec(trade.Lastprice),
                Dec(trade.Volume),
                Csv(trade.Direction.ToString()),
                parentId.ToString(CultureInfo.InvariantCulture),
                (trade.Ticks?.Count ?? 0).ToString(CultureInfo.InvariantCulture));

            WriteCsvLine(GetLevel1FallbackOutputPath(trade.Time.Date), Level1FallbackHeader, line);
        }

        private void WriteFootprintLevel(int bar, IndicatorCandle candle, PriceVolumeInfo level)
        {
            var line = string.Join(",",
                Csv(candle.Time.ToString("O")),
                bar.ToString(CultureInfo.InvariantCulture),
                Dec(level.Price),
                Dec(level.Bid),
                Dec(level.Ask),
                Dec(level.Between),
                Dec(level.Volume),
                level.Ticks.ToString(CultureInfo.InvariantCulture));

            WriteCsvLine(GetFootprintOutputPath(), FootprintHeader, line);
        }

        private void WriteCsvLine(string path, string header, string line)
        {
            lock (_fileLock)
            {
                try
                {
                    Directory.CreateDirectory(Path.GetDirectoryName(path) ?? ".");
                    var writeHeader = !File.Exists(path) || new FileInfo(path).Length == 0;
                    using (var stream = new FileStream(path, FileMode.Append, FileAccess.Write, FileShare.ReadWrite))
                    using (var writer = new StreamWriter(stream, Encoding.UTF8))
                    {
                        if (writeHeader)
                            writer.WriteLine(header);

                        writer.WriteLine(line);
                    }
                }
                catch (Exception ex)
                {
                    _writeErrorCount++;
                    LogWriteError($"CSV write error path={path}: {ex.Message}");
                }
            }
        }

        private string GetFootprintOutputPath()
        {
            var directory = ResolveDirectory(
                FootprintDirectory,
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments), "ATASExports", "Footprint"));

            var fileName = $"{SanitizeFileName(FilePrefix)}_{SanitizeFileName(GetInstrumentName())}_{_sessionId}_footprint.csv";
            return Path.Combine(directory, fileName);
        }

        private string GetLevel1OutputPath(DateTime date)
        {
            var directory = ResolveDirectory(
                Level1Directory,
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments), "ATASExports", "Level1"));

            var fileName = $"{SanitizeFileName(FilePrefix)}_{SanitizeFileName(GetInstrumentName())}_{date:yyyyMMdd}.csv";
            return Path.Combine(directory, fileName);
        }

        private string GetLevel1FallbackOutputPath(DateTime date)
        {
            var directory = ResolveDirectory(
                Level1Directory,
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments), "ATASExports", "Level1"));

            var fileName = $"{SanitizeFileName(FilePrefix)}_{SanitizeFileName(GetInstrumentName())}_{date:yyyyMMdd}_fallback.csv";
            return Path.Combine(directory, fileName);
        }

        private bool Level1DailyFileExists(DateTime date)
        {
            var path = GetLevel1OutputPath(date);
            return File.Exists(path) && new FileInfo(path).Length > Level1Header.Length + Environment.NewLine.Length;
        }

        private string GetStatusOutputPath()
        {
            var directory = ResolveDirectory(
                Level1Directory,
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments), "ATASExports", "Level1"));

            var fileName = $"{SanitizeFileName(FilePrefix)}_{SanitizeFileName(GetInstrumentName())}_{_sessionId}_status.csv";
            return Path.Combine(directory, fileName);
        }

        private static string ResolveDirectory(string configuredDirectory, string defaultDirectory)
        {
            return string.IsNullOrWhiteSpace(configuredDirectory)
                ? defaultDirectory
                : Environment.ExpandEnvironmentVariables(configuredDirectory.Trim());
        }

        private string GetInstrumentName()
        {
            if (!string.IsNullOrWhiteSpace(InstrumentOverride))
                return InstrumentOverride.Trim();

            return Instrument ?? "unknown";
        }

        private long NextParentId() => ++_parentSeq;

        private void LogDebug(string message)
        {
            if (!DebugLogEnabled)
                return;

            var directory = ResolveDirectory(
                Level1Directory,
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments), "ATASExports", "Level1"));
            var path = Path.Combine(
                directory,
                $"{SanitizeFileName(FilePrefix)}_{SanitizeFileName(GetInstrumentName())}_{_sessionId}_debug.log");

            lock (_fileLock)
            {
                AppendTextShared(path, $"{DateTimeOffset.UtcNow:O} {message}{Environment.NewLine}");
            }
        }

        private void LogWriteError(string message)
        {
            var directory = ResolveDirectory(
                Level1Directory,
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments), "ATASExports", "Level1"));
            var path = Path.Combine(
                directory,
                $"{SanitizeFileName(FilePrefix)}_{SanitizeFileName(GetInstrumentName())}_{_sessionId}_errors.log");

            try
            {
                AppendTextShared(path, $"{DateTimeOffset.UtcNow:O} {message}{Environment.NewLine}");
            }
            catch
            {
                // Avoid throwing back into ATAS historical data callbacks.
            }
        }

        private static void AppendTextShared(string path, string text)
        {
            Directory.CreateDirectory(Path.GetDirectoryName(path) ?? ".");
            using (var stream = new FileStream(path, FileMode.Append, FileAccess.Write, FileShare.ReadWrite))
            using (var writer = new StreamWriter(stream, Encoding.UTF8))
            {
                writer.Write(text);
            }
        }

        private void WriteStatus(string phase)
        {
            if (!StatusFileEnabled)
                return;

            var done = RequestHistoricalLevel1Trades
                && !_historicalLevel1RequestInFlight
                && _pendingHistoricalLevel1Ranges.Count == 0
                && _requestedLevel1Start.HasValue
                && _requestedLevel1End.HasValue;
            var totalRequests = _historicalLevel1CompletedRequestCount
                + _pendingHistoricalLevel1Ranges.Count
                + (_historicalLevel1RequestInFlight ? 1 : 0);
            var progressPercent = totalRequests <= 0
                ? (done ? 100m : 0m)
                : Math.Round(_historicalLevel1CompletedRequestCount * 100m / totalRequests, 2);

            var line = string.Join(",",
                Csv(DateTimeOffset.UtcNow.ToString("O")),
                Csv(phase),
                done ? "true" : "false",
                Dec(progressPercent),
                _pendingHistoricalLevel1Ranges.Count.ToString(CultureInfo.InvariantCulture),
                _historicalLevel1RequestInFlight ? "true" : "false",
                _historicalLevel1RequestCount.ToString(CultureInfo.InvariantCulture),
                _historicalLevel1CompletedRequestCount.ToString(CultureInfo.InvariantCulture),
                _level1Seq.ToString(CultureInfo.InvariantCulture),
                _historicalLevel1TickCount.ToString(CultureInfo.InvariantCulture),
                _historicalLevel1FallbackCount.ToString(CultureInfo.InvariantCulture),
                _writeErrorCount.ToString(CultureInfo.InvariantCulture),
                Csv(_requestedLevel1Start?.ToString("O") ?? ""),
                Csv(_requestedLevel1End?.ToString("O") ?? ""));

            WriteCsvLine(
                GetStatusOutputPath(),
                "time,phase,done,progress_percent,queued_ranges,in_flight,requests_started,requests_completed,level1_rows,historical_ticks,historical_fallbacks,write_errors,requested_start,requested_end",
                line);
        }

        private static string Dec(decimal value) => value.ToString(CultureInfo.InvariantCulture);

        private static string Csv(string value)
        {
            if (string.IsNullOrEmpty(value))
                return "";

            var escaped = value.Replace("\"", "\"\"");
            return escaped.IndexOfAny(new[] { ',', '"', '\r', '\n' }) >= 0
                ? $"\"{escaped}\""
                : escaped;
        }

        private static string SanitizeFileName(string value)
        {
            var invalid = Path.GetInvalidFileNameChars();
            var chars = value.Select(ch => invalid.Contains(ch) ? '_' : ch).ToArray();
            var sanitized = new string(chars).Trim();
            return string.IsNullOrWhiteSpace(sanitized) ? "atas_export" : sanitized;
        }

        private readonly struct HistoricalRange
        {
            public HistoricalRange(DateTime start, DateTime end)
            {
                Start = start;
                End = end;
            }

            public DateTime Start { get; }
            public DateTime End { get; }
        }
    }
}
