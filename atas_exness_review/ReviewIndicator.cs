using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.Drawing;
using System.Drawing.Drawing2D;
using Point = System.Drawing.Point;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Threading;
using ATAS.Indicators;
using OFT.Attributes.Editors;
using OFT.Rendering.Context;
using OFT.Rendering.Control;
using OFT.Rendering.Tools;
using Rectangle = System.Drawing.Rectangle;

namespace ExnessReview;

[DisplayName("Exness Trade Review")]
[Category("Custom")]
[Description("在 GC、NQ、ES 和 Binance BTCUSDT 永续图上显示 Exness 开平仓。悬停箭头或连线查看成交。")]
public sealed partial class ReviewIndicator : Indicator
{
    private sealed record View(string Account, (string Code, string Exchange) Chart, string Caption, Mark[] Marks,
        IReadOnlyDictionary<DateTime, PricePoint> Prices);
    private sealed record SyncButton(Rectangle Bounds);
    private sealed record Anchor(Mark Mark, PricePoint Price, Point Tip);
    private readonly RenderFont _font = new("Microsoft YaHei", 9);
    private readonly List<ReviewBar> _bars = new();
    private readonly object _gate = new();
    private volatile string _account = "Exness";
    private volatile View _view = new("", ("", ""), "", [], new Dictionary<DateTime, PricePoint>());
    private volatile SyncButton _syncButton = new(Rectangle.Empty);
    private volatile string _syncCaption = "同步 Exness API";
    private static readonly SemaphoreSlim SyncGate = new(1, 1);
    private IReadOnlyList<Trade>? _snapshot;
    private TickPriceMatches? _matches;
    private string _lastDirectory = "";
    private CancellationTokenSource? _lookup;
    private volatile string _tickDirectory = @"E:\ATASExports\Level1";

    private (string Code, string Exchange) CurrentChart
    {
        get
        {
            var info = InstrumentInfo;
            return (info?.Instrument ?? "", info?.Exchange ?? "");
        }
    }

    [Display(Name = "逐笔目录", GroupName = "行情", Description = "可选的本地行情目录，用于按开平仓时间匹配图表价格。选择 GC、NQ、ES 或 BTCUSDT@BinanceFutures 子目录的上级目录。留空只使用当前图表合约的逐笔。")]
    public string TickDirectory { get => _tickDirectory; set => _tickDirectory = value?.Trim() ?? ""; }

    private DispatcherTimer? _timer;
    private int _disposed;
    private bool _busy;
    private bool _pressed;
    private int _arrowSize = 18;
    private int _lineWidth = 3;

    [Display(Name = "箭头大小（像素）", GroupName = "显示", Order = 10)]
    [NumericEditor(NumericEditorTypes.TrackBar, 4, 48)]
    [Range(4, 48)]
    public int ArrowSize { get => _arrowSize; set { _arrowSize = Math.Clamp(value, 4, 48); RedrawChart(); } }

    [Display(Name = "连线粗细（像素）", GroupName = "显示", Order = 20)]
    [NumericEditor(NumericEditorTypes.TrackBar, 1, 12)]
    [Range(1, 12)]
    public int LineWidth { get => _lineWidth; set { _lineWidth = Math.Clamp(value, 1, 12); RedrawChart(); } }

    [Display(Name = "账户别名", GroupName = "复盘", Description = "使用相同别名的图表共享本地复盘记录。默认别名为 Exness。")]
    public string AccountAlias { get => _account; set => _account = string.IsNullOrWhiteSpace(value) ? "Exness" : value.Trim(); }

    public ReviewIndicator() : base(true)
    {
        DenyToChangePanel = true;
        EnableCustomDrawing = true;
        DrawAbovePrice = true;
        SubscribeToDrawingEvents(DrawingLayouts.Final);
        ((ValueDataSeries)DataSeries[0]).VisualType = VisualMode.Hide;
    }

    protected override void OnInitialize()
    {
        base.OnInitialize();
        Application.Current?.Dispatcher.BeginInvoke(new Action(() =>
        {
            if (Volatile.Read(ref _disposed) != 0 || _timer != null) return;
            _timer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(1) };
            _timer.Tick += Refresh;
            _timer.Start();
            Refresh(null, EventArgs.Empty);
        }));
    }

    private void Refresh(object? sender, EventArgs args) => RefreshPrices(retryExpired: false);

    private void RefreshPrices(bool retryExpired)
    {
        if (Volatile.Read(ref _disposed) != 0) return;
        var account = AccountAlias;
        var chart = CurrentChart;
        try
        {
            var snapshot = ReviewStore.Snapshot(account);
            var directory = TickDirectory;
            var sameSource = _snapshot != null && _view.Account == account && _view.Chart == chart && _lastDirectory == directory;
            var now = DateTime.UtcNow;
            var retry = retryExpired && sameSource && _nativeCompleted == null && _matches?.RetryDue(now) == true;
            if (sameSource && ReferenceEquals(snapshot, _snapshot) && !retry) return;
            var symbol = Projection.SourceSymbol(chart.Code, chart.Exchange);
            var marks = symbol == null ? Array.Empty<Mark>() : Projection.Events(snapshot, symbol);
            if (sameSource && !retry && marks.AsSpan().SequenceEqual(_view.Marks))
            {
                _snapshot = snapshot;
                return;
            }
            _lookup?.Cancel();
            _lookup?.Dispose();
            _lookup = new CancellationTokenSource();
            CancelNativeLookup();
            var times = marks.Select(mark => mark.Utc).ToHashSet();
            _matches = new TickPriceMatches(times, sameSource ? _matches : null, now);
            _view = new View(account, chart, symbol == null ? "仅 GC / NQ / ES / Binance BTCUSDT 永续" : marks.Length == 0 ? $"{symbol} · 0 笔" : $"{symbol} · 匹配逐笔中", marks,
                _matches.Snapshot());
            _snapshot = snapshot;
            _lastDirectory = directory;
            if (marks.Length != 0) _ = ResolvePrices(_view, symbol!, directory, times, _matches, _lookup.Token);
        }
        catch (Exception error)
        {
            if (_view.Caption == error.Message && _view.Account == account && _view.Chart == chart) return;
            _snapshot = null;
            _view = new View(account, chart, error.Message, [], new Dictionary<DateTime, PricePoint>());
        }
        RedrawChart();
    }

    private async Task ResolvePrices(View initial, string symbol, string directory, HashSet<DateTime> times,
        TickPriceMatches matches, CancellationToken cancel)
    {
        var root = symbol switch { "XAUUSD" => "GC", "USTEC" => "NQ", "US500" => "ES", "BTCUSD" => TickPrices.BinanceFuturesRoot,
            _ => throw new ArgumentOutOfRangeException(nameof(symbol)) };
        string fileIssue = "";
        bool Active() => !cancel.IsCancellationRequested && Volatile.Read(ref _disposed) == 0 &&
            AccountAlias == initial.Account && CurrentChart == initial.Chart && TickDirectory == directory;
        void Publish(bool pending)
        {
            var prices = matches.Snapshot();
            var count = initial.Marks.Count(mark => prices.ContainsKey(mark.Utc));
            _view = initial with { Prices = prices,
                Caption = $"{symbol} · 逐笔 {count}/{initial.Marks.Length}" + (pending ? " · 核对合约…" : "") + fileIssue };
            RedrawChart();
        }
        try
        {
            var localTimes = matches.MissingLocal(times);
            if (localTimes.Length > 0 && directory.Length > 0)
            {
                var local = await Task.Run(() => TickPrices.Lookup(directory, root, localTimes, cancel), cancel);
                if (!Active()) return;
                matches.AddLocal(local, preferArchive: root == TickPrices.BinanceFuturesRoot);
            }
        }
        catch (OperationCanceledException) { return; }
        catch (Exception error) { fileIssue = " · " + error.Message; }
        if (!Active()) return;
        var nativeTimes = matches.MissingNative(times);
        Publish(nativeTimes.Length != 0);
        if (nativeTimes.Length == 0) return;
        try
        {
            StartNativeLookup(nativeTimes, (window, native) =>
            {
                if (Active()) matches.ApplyNativeWindow(window, native, DateTime.UtcNow);
            }, () => { if (Active()) Publish(false); });
        }
        catch (Exception) { if (Active()) Publish(false); }
    }

    protected override void OnCalculate(int bar, decimal value)
    {
        var candle = GetCandle(bar);
        if (candle == null) return;
        lock (_gate)
        {
            if (bar == 0) _bars.Clear();
            var sample = new ReviewBar(candle.Time, candle.LastTime, candle.High, candle.Low);
            if (bar == _bars.Count) _bars.Add(sample);
            else if (bar >= 0 && bar < _bars.Count) _bars[bar] = sample;
        }
    }

    protected override void OnRender(RenderContext context, DrawingLayouts layout)
    {
        var chart = ChartInfo;
        if (chart == null || Container == null) return;
        var region = Container.Region;
        var view = _view;
        var points = new List<Anchor>();
        if (view.Account == AccountAlias && view.Chart == CurrentChart)
        {
            var period = Period(chart.ChartType, chart.TimeFrame);
            lock (_gate)
            {
                foreach (var mark in view.Marks)
                {
                    if (!view.Prices.TryGetValue(mark.Utc, out var price) ||
                        !ReviewGeometry.Locate(_bars, mark.Utc, period, out var index)) continue;
                    if (!price.FromChart && (!ReviewGeometry.Locate(_bars, price.Print.Utc, period, out var quoteBar) ||
                        price.Print.Price < _bars[quoteBar].Low || price.Print.Price > _bars[quoteBar].High)) continue;
                    var horizontal = chart.GetXByBar(index, false);
                    points.Add(new Anchor(mark, price, new Point(horizontal, chart.GetYByPrice(price.Print.Price, false))));
                }
            }
        }

        var mouseInfo = chart.MouseLocationInfo;
        var mouse = mouseInfo.LastPosition;
        var hover = !mouseInfo.IsMouseLeave && region.Contains(mouse);
        var nearLines = new List<Anchor>();
        var nearArrows = new List<Anchor>();
        var lineWidth = LineWidth;
        var arrowHeight = ArrowSize;
        var halfWidth = (int)Math.Round(arrowHeight * 0.55);
        var longPen = new RenderPen(Color.FromArgb(190, 15, 150, 135), lineWidth, DashStyle.Dash);
        var shortPen = new RenderPen(Color.FromArgb(190, 215, 90, 61), lineWidth, DashStyle.Dash);
        foreach (var ticket in points.GroupBy(point => point.Mark.Trade.Ticket))
        {
            var entry = ticket.FirstOrDefault(point => !point.Mark.IsExit);
            var exit = ticket.FirstOrDefault(point => point.Mark.IsExit);
            if (entry == null || exit == null || !ReviewGeometry.Clip(entry.Tip, exit.Tip, region, out var start, out var end)) continue;
            context.DrawLine(entry.Mark.Below ? longPen : shortPen, start.X, start.Y, end.X, end.Y);
            if (hover && ReviewGeometry.Near(mouse, start, end, 3 + lineWidth * 0.5)) nearLines.Add(exit);
        }
        foreach (var point in points)
        {
            var tip = point.Tip;
            if (!region.Contains(tip)) continue;
            var direction = point.Mark.Below ? 1 : -1;
            var baseY = tip.Y + direction * arrowHeight;
            var color = point.Mark.Below ? Color.FromArgb(0, 165, 139) : Color.FromArgb(224, 80, 56);
            context.FillPolygon(color, [tip, new Point(tip.X - halfWidth, baseY), new Point(tip.X + halfWidth, baseY)]);
            if (hover && new Rectangle(tip.X - halfWidth - 2, Math.Min(tip.Y, baseY) - 2, 2 * halfWidth + 5, arrowHeight + 5).Contains(mouse))
                nearArrows.Add(point);
        }
        var selected = nearArrows.Count != 0 ? nearArrows : nearLines;
        if (selected.Count > 0) Tooltip(context, region, mouse, selected);
        var sync = DrawLabel(_syncCaption, region.Left + 8);
        if (view.Caption.Length > 0) DrawLabel(view.Caption, sync.Right + 4);
        _syncButton = new SyncButton(sync);

        Rectangle DrawLabel(string label, int left)
        {
            var size = context.MeasureString(label, _font);
            var button = new Rectangle(left, region.Top + 8, size.Width + 16, size.Height + 10);
            context.FillRectangle(Color.FromArgb(235, 25, 83, 105), button);
            context.DrawString(label, _font, Color.White, left + 8, button.Top + 5);
            return button;
        }
    }

    private static TimeSpan? Period(string type, string frame)
    {
        if (type == "Seconds" && int.TryParse(frame, out var seconds) && seconds > 0) return TimeSpan.FromSeconds(seconds);
        if (type != "TimeFrame" || string.IsNullOrEmpty(frame)) return null;
        if (frame == "Daily") return TimeSpan.FromDays(1);
        if (frame == "Weekly") return TimeSpan.FromDays(7);
        if (!int.TryParse(frame.AsSpan(1), out var count) || count <= 0) return null;
        return frame[0] switch { 'M' => TimeSpan.FromMinutes(count), 'H' => TimeSpan.FromHours(count),
            'D' => TimeSpan.FromDays(count), 'W' => TimeSpan.FromDays(7d * count), _ => null };
    }

    private void Tooltip(RenderContext context, Rectangle region, Point mouse, List<Anchor> anchors)
    {
        var lines = new List<string>();
        foreach (var anchor in anchors.Take(3))
        {
            var mark = anchor.Mark;
            var trade = mark.Trade;
            var time = trade.Ticket.StartsWith("API-", StringComparison.Ordinal)
                ? mark.Utc.ToString("HH:mm:ss.FFFFFF", System.Globalization.CultureInfo.InvariantCulture)
                : mark.Utc.ToString("HH:mm:ss", System.Globalization.CultureInfo.InvariantCulture);
            lines.Add($"{mark.Label}  {Projection.QuantityLabel(trade)}  #{trade.Ticket}  {time} UTC");
            lines.Add($"{trade.Symbol} {(mark.IsExit ? trade.ClosePrice : trade.OpenPrice):G29}  |  期货 {anchor.Price.Print.Price:G29}" +
                (mark.IsExit && trade.Profit.HasValue ? $"  |  P/L {trade.Profit:G29}" : ""));
            var priceSource = anchor.Price.FromChart ? "图表逐笔"
                : Projection.SourceSymbol(_view.Chart.Code, _view.Chart.Exchange) == "BTCUSD" ? "币安永续成交归档" : "本地品种逐笔";
            lines.Add($"{priceSource}  {anchor.Price.Print.Utc:HH:mm:ss.fff} UTC");
        }
        if (anchors.Count > 3) lines.Add($"另有 {anchors.Count - 3} 个重叠标记");
        var height = context.MeasureString("交易", _font).Height + 2;
        var width = lines.Max(line => context.MeasureString(line, _font).Width) + 12;
        var box = new Rectangle(Math.Clamp(mouse.X + 12, region.Left, Math.Max(region.Left, region.Right - width)),
            Math.Clamp(mouse.Y + 12, region.Top, Math.Max(region.Top, region.Bottom - lines.Count * height - 8)), width, lines.Count * height + 8);
        context.FillRectangle(Color.FromArgb(240, 34, 40, 48), box);
        for (var index = 0; index < lines.Count; index++)
            context.DrawString(lines[index], _font, Color.WhiteSmoke, box.Left + 6, box.Top + 4 + index * height);
    }

    public override bool ProcessMouseMove(RenderControlMouseEventArgs e)
    {
        if (_view.Marks.Length != 0) RedrawChart();
        return base.ProcessMouseMove(e);
    }

    public override bool ProcessMouseDown(RenderControlMouseEventArgs e)
    {
        if (e.Button != RenderControlMouseButtons.Left || !_syncButton.Bounds.Contains(e.Location))
            return base.ProcessMouseDown(e);
        _pressed = true;
        Application.Current?.Dispatcher.BeginInvoke(new Action(async () =>
        {
            if (_busy || Volatile.Read(ref _disposed) != 0) return;
            _busy = true;
            try { await SyncApi(); }
            catch (Exception error)
            {
                _syncCaption = "同步 Exness API";
                if (Volatile.Read(ref _disposed) == 0)
                {
                    RedrawChart();
                    MessageBox.Show(error.Message, "Exness 同步错误", MessageBoxButton.OK, MessageBoxImage.Error);
                }
            }
            finally { _busy = false; }
        }));
        return true;
    }

    public override bool ProcessMouseUp(RenderControlMouseEventArgs e)
    {
        if (!_pressed) return base.ProcessMouseUp(e);
        _pressed = false;
        return true;
    }

    private async Task SyncApi()
    {
        if (!await SyncGate.WaitAsync(0)) return;
        try
        {
            var account = AccountAlias;
            _syncCaption = "同步中…";
            RedrawChart();
            var chartAccountId = TradingManager?.Portfolio?.AccountID;
            var unmatched = await Task.Run(() => ApiHistoryImport.RunAsync(account, chartAccountId));
            if (Volatile.Read(ref _disposed) != 0) return;
            RefreshPrices(retryExpired: true);
            _syncCaption = unmatched > 0
                ? $"同步 Exness API · {unmatched} 个仓位待配齐"
                : $"同步 Exness API · {DateTime.Now:HH:mm} 已同步";
        }
        finally
        {
            SyncGate.Release();
            if (Volatile.Read(ref _disposed) == 0) RedrawChart();
        }
    }

    protected override void OnDispose()
    {
        Interlocked.Exchange(ref _disposed, 1);
        Application.Current?.Dispatcher.BeginInvoke(new Action(() =>
        {
            if (_timer == null) return;
            _lookup?.Cancel();
            _lookup?.Dispose();
            CancelNativeLookup();
            _nativeTimeout?.Stop();
            _timer.Stop();
            _timer.Tick -= Refresh;
            _timer = null;
        }));
        base.OnDispose();
    }
}
