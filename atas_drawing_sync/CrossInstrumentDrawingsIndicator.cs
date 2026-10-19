using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Threading;
using System.Windows;
using System.Windows.Threading;
using ATAS.Indicators;
using OFT.Attributes.Editors;
using OFT.Rendering.Context;
using OFT.Rendering.Tools;
using Rectangle = System.Drawing.Rectangle;

namespace CrossInstrumentDrawings;

public enum SyncMode { Auto, SourceMgc, TargetGc, Off }

[DisplayName("GC-MGC Drawing Sync V1")]
[Category("Custom")]
[Description("One-way MGC native rectangle/trend-line display on GC. Add to both charts; default Auto mode.")]
public sealed class CrossInstrumentDrawingsIndicator : Indicator
{
    private sealed record ViewState(DrawingSnapshot? Snapshot, string Status, bool IsTarget);
    private readonly Guid _owner = Guid.NewGuid();
    private readonly NativeDrawingReader _reader = new();
    private readonly object _timesGate = new();
    private readonly List<DateTime> _times = new();
    private readonly RenderFont _font = new("Arial", 10);
    private DispatcherTimer? _timer;
    private int _disposed;
    private string _channel = "GC-MGC";
    private SyncMode _mode;
    private bool _globalOnly;
    private decimal _priceOffset;
    private int _fillOpacity = 20;
    private bool _useSourceColors;
    private System.Windows.Media.Color _mappedColor = System.Windows.Media.Color.FromRgb(0, 180, 190);
    private bool _showSourceLabels = true;
    private bool _showStatus = true;
    private ViewState _view = new(null, "Waiting for chart initialization", false);
    private DateTimeOffset _nextLog;
    private string? _publishedChannel;

    [Display(Name = "Mode", GroupName = "Sync", Order = 10,
        Description = "Auto: MGC publishes; GC receives. Off stops this instance.")]
    public SyncMode Mode { get => _mode; set => _mode = value; }

    [Display(Name = "Channel", GroupName = "Sync", Order = 20,
        Description = "Use the same name on one MGC source and all its GC targets. Different groups need different names.")]
    public string Channel { get => _channel; set => _channel = string.IsNullOrWhiteSpace(value) ? "GC-MGC" : value.Trim(); }

    [Display(Name = "Global objects only", GroupName = "Sync", Order = 30,
        Description = "Off includes local and global drawings on the source chart. On exports only Global drawings.")]
    public bool GlobalObjectsOnly { get => _globalOnly; set => _globalOnly = value; }

    [Display(Name = "Target price offset", GroupName = "Display", Order = 10,
        Description = "GC display price = MGC drawing price + offset. Same expiry normally uses zero.")]
    [NumericEditor(-10000, 10000)]
    [Range(-10000, 10000)]
    public decimal TargetPriceOffset { get => _priceOffset; set => _priceOffset = Math.Clamp(value, -10000, 10000); }

    [Display(Name = "Zone opacity (%)", GroupName = "Display", Order = 20)]
    [NumericEditor(NumericEditorTypes.TrackBar, 0, 80)]
    [Range(0, 80)]
    public int ZoneOpacity { get => _fillOpacity; set => _fillOpacity = Math.Clamp(value, 0, 80); }

    [Display(Name = "Use source colors", GroupName = "Display", Order = 21,
        Description = "Off gives all MGC projections a distinct mapped color. Does not change source drawings.")]
    public bool UseSourceColors { get => _useSourceColors; set { _useSourceColors = value; RedrawChart(); } }

    [Display(Name = "Mapped color", GroupName = "Display", Order = 22,
        Description = "Color for projected borders, fills and trend lines when Use source colors is off.")]
    public System.Windows.Media.Color MappedColor { get => _mappedColor; set { _mappedColor = value; RedrawChart(); } }

    [Display(Name = "Show MGC labels", GroupName = "Display", Order = 23,
        Description = "Mark projected objects with a small MGC tag; edit them on the source MGC chart.")]
    public bool ShowMgcLabels { get => _showSourceLabels; set { _showSourceLabels = value; RedrawChart(); } }

    [Display(Name = "Show status", GroupName = "Display", Order = 30)]
    public bool ShowStatus { get => _showStatus; set => _showStatus = value; }

    public CrossInstrumentDrawingsIndicator() : base(true)
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
        var dispatcher = Application.Current?.Dispatcher;
        if (dispatcher is null)
        {
            Volatile.Write(ref _view, new ViewState(null, "ATAS UI dispatcher unavailable", false));
            return;
        }
        dispatcher.BeginInvoke(new Action(() =>
        {
            if (Volatile.Read(ref _disposed) != 0) return;
            _timer = new DispatcherTimer(DispatcherPriority.Background, dispatcher)
            { Interval = TimeSpan.FromMilliseconds(250) };
            _timer.Tick += Tick;
            _timer.Start();
            Tick(null, EventArgs.Empty);
        }));
    }

    protected override void OnCalculate(int bar, decimal value)
    {
        var candle = GetCandle(bar);
        if (candle is null) return;
        lock (_timesGate)
        {
            if (bar == 0) _times.Clear();
            if (bar == _times.Count) _times.Add(candle.Time);
            else if (bar < _times.Count) _times[bar] = candle.Time;
        }
    }

    private void Tick(object? sender, EventArgs args)
    {
        if (Volatile.Read(ref _disposed) != 0) return;
        var now = DateTimeOffset.UtcNow;
        var instrument = InstrumentInfo?.Instrument ?? string.Empty;
        var root = InstrumentIdentity.Root(instrument);
        var channel = _channel;
        var source = (_mode == SyncMode.Auto && root == "MGC") || (_mode == SyncMode.SourceMgc && root == "MGC");
        var target = (_mode == SyncMode.Auto && root == "GC") || (_mode == SyncMode.TargetGc && root == "GC");
        try
        {
            if (!source || _publishedChannel != channel)
            {
                DrawingBus.Shared.RemoveOwner(_owner);
                _publishedChannel = null;
            }
            ViewState view;
            if (source && ChartInfo is { } chart)
            {
                var snapshot = _reader.Capture(chart, _owner, instrument, _globalOnly, now);
                if (!DrawingBus.Shared.Publish(channel, snapshot))
                    view = new ViewState(null, $"Source conflict on '{channel}': keep one MGC source per channel", false);
                else
                {
                    _publishedChannel = channel;
                    view = new ViewState(null,
                        $"MGC source | {instrument} | {snapshot.Shapes.Count} shapes / {snapshot.TotalObjects} native objects | {channel}", false);
                }
            }
            else if (target)
            {
                var snapshot = DrawingBus.Shared.Read(channel, now);
                var expiry = InstrumentIdentity.Contract(instrument, now.Year);
                if (snapshot is null)
                    view = new ViewState(null, $"GC target | waiting for MGC source on '{channel}'", true);
                else if (!InstrumentIdentity.ContractsMatch(snapshot.Contract, expiry))
                    view = new ViewState(null, $"Expiry mismatch: MGC {snapshot.Contract} / GC {expiry}", true);
                else
                {
                    var rectangles = snapshot.Shapes.Count(x => x.Kind == ShapeKind.Rectangle);
                    var unknown = snapshot.Contract.Length == 0 || expiry.Length == 0 ? " | check same expiry" : "";
                    view = new ViewState(snapshot,
                        $"GC target <- {snapshot.Instrument} | zones {rectangles}, lines {snapshot.Shapes.Count - rectangles} | {channel}{unknown}", true);
                }
            }
            else view = new ViewState(null, _mode == SyncMode.Off ? "Drawing sync OFF" :
                $"Waiting for MGC/GC chart; current instrument '{instrument}'", false);
            Volatile.Write(ref _view, view);
        }
        catch (Exception error)
        {
            var cause = error is System.Reflection.TargetInvocationException { InnerException: { } inner } ? inner : error;
            // Leave the source drawings untouched; stale target snapshots expire after three seconds.
            Volatile.Write(ref _view, new ViewState(null, $"Sync read error: {cause.GetType().Name}: {cause.Message}", target));
        }
        if (now >= _nextLog)
        {
            WriteStatus(instrument, now);
            _nextLog = now.AddSeconds(5);
        }
        RedrawChart();
    }

    protected override void OnRender(RenderContext context, DrawingLayouts layout)
    {
        var chart = ChartInfo;
        var container = Container;
        if (chart is null || container is null) return;
        var view = Volatile.Read(ref _view);
        var region = container.Region;
        var mapped = _mappedColor;
        var projectionColor = Color.FromArgb(mapped.A, mapped.R, mapped.G, mapped.B);
        var useSourceColors = _useSourceColors;
        var showSourceLabels = _showSourceLabels;
        var missing = 0;
        if (view.IsTarget && view.Snapshot is { } snapshot && DateTimeOffset.UtcNow - snapshot.CapturedAt < DrawingBus.Lease)
        {
            DateTime[] times;
            lock (_timesGate) times = _times.ToArray();
            foreach (var shape in snapshot.Shapes)
            {
                var t1 = shape.FirstTime;
                var t2 = shape.SecondTime;
                if (shape.Kind == ShapeKind.Rectangle && times.Length > 0)
                {
                    var first = t1 <= t2 ? t1 : t2;
                    var last = t1 <= t2 ? t2 : t1;
                    if (last < times[0] && !shape.ExtendRight) continue;
                    t1 = first < times[0] ? times[0] : first;
                    t2 = last < times[0] ? times[0] : last;
                }
                if (!TimeProjection.TryBar(times, t1, out var b1) || !TimeProjection.TryBar(times, t2, out var b2))
                { missing++; continue; }
                double X(double position)
                {
                    var floor = (int)Math.Clamp(Math.Floor(position), 0, times.Length - 1);
                    var x = chart.GetXByBar(floor, false);
                    var width = floor < times.Length - 1 ? chart.GetXByBar(floor + 1, false) - x :
                        floor > 0 ? x - chart.GetXByBar(floor - 1, false) : 10;
                    return x + (position - floor) * Math.Max(1, width);
                }
                var x1 = X(b1);
                var x2 = X(b2);
                var y1 = chart.GetYByPrice(shape.FirstPrice + _priceOffset, false);
                var y2 = chart.GetYByPrice(shape.SecondPrice + _priceOffset, false);
                var dash = Enum.TryParse<DashStyle>(shape.Dash, out var style) ? style : DashStyle.Solid;
                var lineColor = useSourceColors ? shape.Color : projectionColor;
                var fillColor = useSourceColors ? shape.FillColor : projectionColor;
                var pen = new RenderPen(lineColor, shape.Width, dash);
                PointF? Line(double ax, double ay, double bx, double by, LineExtent extent = LineExtent.Segment)
                {
                    if (TimeProjection.ClipLine(ax, ay, bx, by, region, extent, out var a, out var b))
                    {
                        context.DrawLine(pen, (int)Math.Round(a.X), (int)Math.Round(a.Y), (int)Math.Round(b.X), (int)Math.Round(b.Y));
                        return a;
                    }
                    return null;
                }
                void SourceLabel(double x, double y)
                {
                    if (!showSourceLabels || region.Width < 52 || region.Height < 26) return;
                    var labelX = (int)Math.Clamp(x + 4, region.Left + 4, region.Right - 46);
                    var labelY = (int)Math.Clamp(y - 19, region.Top + 4, region.Bottom - 22);
                    context.FillRectangle(Color.FromArgb(210, 15, 18, 25), new Rectangle(labelX, labelY, 40, 18));
                    context.DrawString("MGC", _font, lineColor, labelX + 4, labelY + 1);
                }
                if (shape.Kind == ShapeKind.TrendLine)
                {
                    if (Line(x1, y1, x2, y2, shape.Extent) is { } anchor)
                        SourceLabel(anchor.X, anchor.Y);
                }
                else
                {
                    var left = shape.ExtendLeft ? region.Left : Math.Min(x1, x2);
                    var right = shape.ExtendRight ? region.Right : Math.Max(x1, x2);
                    var top = Math.Min(y1, y2);
                    var bottom = Math.Max(y1, y2);
                    var clippedLeft = Math.Max(region.Left, left);
                    var clippedRight = Math.Min(region.Right, right);
                    var clippedTop = Math.Max(region.Top, top);
                    var clippedBottom = Math.Min(region.Bottom, bottom);
                    if (clippedRight >= clippedLeft && clippedBottom >= clippedTop)
                    {
                        var rectangle = Rectangle.FromLTRB((int)clippedLeft, clippedTop, (int)clippedRight, clippedBottom);
                        context.FillRectangle(Color.FromArgb(_fillOpacity * 255 / 100, fillColor), rectangle);
                        Line(left, top, right, top);
                        Line(left, bottom, right, bottom);
                        Line(left, top, left, bottom);
                        Line(right, top, right, bottom);
                        SourceLabel(clippedLeft, clippedTop);
                    }
                }
            }
        }
        if (_showStatus)
        {
            var status = view.Status + (missing > 0 ? $" | load more GC history for {missing} anchors" : "");
            context.FillRectangle(Color.FromArgb(190, 15, 18, 25),
                new Rectangle(region.Left + 4, Math.Max(region.Top, region.Bottom - 28), Math.Max(0, region.Width - 8), 24));
            context.DrawString(status, _font, Color.LightGoldenrodYellow, region.Left + 8, Math.Max(region.Top, region.Bottom - 25));
        }
    }

    private void WriteStatus(string instrument, DateTimeOffset time)
    {
        try
        {
            var directory = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
                "ATAS", "IndicatorData", "CrossInstrumentDrawingsV1");
            Directory.CreateDirectory(directory);
            var view = Volatile.Read(ref _view);
            File.WriteAllText(Path.Combine(directory, $"status-{_owner:N}.json"), JsonSerializer.Serialize(new
            {
                time, instrument, channel = _channel, mode = _mode.ToString(), status = view.Status,
                reader = _reader.AccessPath, receivedShapes = view.Snapshot?.Shapes.Count ?? 0,
                platform = typeof(Indicator).Assembly.GetName().Version?.ToString()
            }, new JsonSerializerOptions { WriteIndented = true }));
        }
        catch (IOException) { }
        catch (UnauthorizedAccessException) { }
    }

    protected override void OnDispose()
    {
        Interlocked.Exchange(ref _disposed, 1);
        DrawingBus.Shared.RemoveOwner(_owner);
        var timer = _timer;
        if (timer != null)
            timer.Dispatcher.BeginInvoke(new Action(() => { timer.Stop(); timer.Tick -= Tick; }));
        base.OnDispose();
    }
}
