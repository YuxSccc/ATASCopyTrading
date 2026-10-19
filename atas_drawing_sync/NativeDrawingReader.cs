using System;
using System.Collections;
using System.Collections.Generic;
using System.Drawing;
using System.Globalization;
using System.Linq;
using System.Reflection;

namespace CrossInstrumentDrawings;

/// <summary>
/// Read-only adapter for the installed ATAS Classic internal object model.
/// No calls to Clone, Add, Delete, Save, SetBounds, setters, or trade APIs.
/// Invoke from the ATAS UI dispatcher because native drawings are mutable.
/// </summary>
public sealed class NativeDrawingReader
{
    private const BindingFlags Members = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic;
    private object? _lastChart;
    private object? _core;
    public string AccessPath { get; private set; } = "not attached";

    public DrawingSnapshot Capture(object chart, Guid owner, string instrument, bool globalOnly, DateTimeOffset now)
    {
        if (!ReferenceEquals(chart, _lastChart))
        {
            _core = ResolveCore(chart);
            _lastChart = chart;
        }
        var data = Require(_core!, "Data");
        var collection = Require(data, "DrawingObjects");
        var nativeInstrument = Require(data, "Instrument");
        var method = collection.GetType().GetMethods(Members).SingleOrDefault(x => x.Name == "GetAllObjects" &&
            x.GetParameters().Length == 1 && x.GetParameters()[0].ParameterType.IsInstanceOfType(nativeInstrument))
            ?? throw new MissingMethodException(collection.GetType().FullName, "GetAllObjects(instrument)");
        if (method.Invoke(collection, new[] { nativeInstrument }) is not IEnumerable items)
            throw new InvalidOperationException("ATAS drawing collection did not return an enumerable.");
        AccessPath = chart.GetType().Name + " -> ChartCore.Data.DrawingObjects.GetAllObjects";
        // Copy the complete native collection before parsing. A failed capture is never published as empty.
        var nativeObjects = items.Cast<object>().Take(2001).ToArray();
        if (nativeObjects.Length > 2000) throw new InvalidOperationException("Source has more than 2000 drawings.");
        var shapes = new Dictionary<string, DrawingShape>(StringComparer.Ordinal);
        var skipped = 0;
        foreach (var item in nativeObjects)
        {
            var name = item.GetType().Name;
            if (name != "RectangleObj" && name != "TrendLine") { skipped++; continue; }
            if (!Boolean(item, "IsCompleted", false) || !Boolean(item, "Visible", true) ||
                Boolean(item, "IsObjectInPanel", false) || (globalOnly && !Boolean(item, "IsGlobal", false)))
            { skipped++; continue; }
            var id = Convert.ToString(Get(item, "Id"), CultureInfo.InvariantCulture);
            if (string.IsNullOrEmpty(id)) throw new InvalidOperationException("Native drawing has no stable Id.");
            var first = Date(item, "_firstTime", "FirstTime");
            var second = Date(item, "_secondTime", "SecondTime");
            var p1 = Decimal(item, "TrueFirstPrice");
            var p2 = Decimal(item, "TrueSecondPrice");
            if (first == default || second == default || p1 <= 0 || p2 <= 0)
                throw new InvalidOperationException($"Cannot read time/price anchors for {name} {id}.");
            var lineType = Convert.ToString(Get(item, "LineRenderType"), CultureInfo.InvariantCulture);
            var extent = lineType == "ExtendedLine" ? LineExtent.Infinite :
                lineType == "Ray" || Boolean(item, "Ray", false) ? LineExtent.Ray : LineExtent.Segment;
            shapes[id] = new DrawingShape(id, name == "RectangleObj" ? ShapeKind.Rectangle : ShapeKind.TrendLine,
                first, p1, second, p2, ReadColor(Get(item, "Color"), Color.Gold),
                ReadColor(Get(item, "AreaColor"), Color.Gold),
                Math.Clamp((float)Decimal(item, "Width", 2), 1, 10),
                Convert.ToString(Get(item, "Style"), CultureInfo.InvariantCulture) ?? "Solid",
                Boolean(item, "ExtendLeft", false), Boolean(item, "ExtendRight", false), extent);
        }
        return new DrawingSnapshot(owner, instrument, InstrumentIdentity.Contract(instrument, now.Year), now,
            Array.AsReadOnly(shapes.Values.ToArray()), nativeObjects.Length, skipped);
    }

    public static object ResolveCore(object chart)
    {
        if (chart.GetType().FullName == "OFT.Platform.ViewModels.Charting.ChartCore") return chart;
        if (Get(chart, "ChartCore") is { } direct) return direct;
        // ChartInfo is ChartCoordinates in ATAS Classic; discover the field by its stable TYPE,
        // not by its obfuscated name. Do not traverse arbitrary platform services.
        for (var type = chart.GetType(); type != null; type = type.BaseType)
        {
            var field = type.GetFields(Members | BindingFlags.DeclaredOnly)
                .FirstOrDefault(x => x.FieldType.FullName == "OFT.Platform.ViewModels.Charting.ChartCore");
            if (field?.GetValue(chart) is { } core) return core;
        }
        throw new NotSupportedException("Unsupported chart adapter: " + chart.GetType().FullName);
    }

    private static object Require(object obj, string name)
        => Get(obj, name) ?? throw new MissingMemberException(obj.GetType().FullName, name);

    private static object? Get(object obj, string name)
    {
        for (var type = obj.GetType(); type != null; type = type.BaseType)
        {
            var property = type.GetProperty(name, Members | BindingFlags.DeclaredOnly);
            if (property != null && property.GetIndexParameters().Length == 0) return property.GetValue(obj);
            var field = type.GetField(name, Members | BindingFlags.DeclaredOnly);
            if (field != null) return field.GetValue(obj);
        }
        return null;
    }

    private static bool Boolean(object obj, string name, bool fallback)
        => Get(obj, name) is bool value ? value : fallback;

    private static decimal Decimal(object obj, string name, decimal fallback = 0)
        => Get(obj, name) is { } value ? Convert.ToDecimal(value, CultureInfo.InvariantCulture) : fallback;

    private static DateTime Date(object obj, params string[] names)
    {
        foreach (var name in names)
            if (Get(obj, name) is DateTime value && value != default) return value;
        return default;
    }

    private static Color ReadColor(object? value, Color fallback)
    {
        if (value is Color color) return color;
        // System.Windows.Media.Color without a platform UI dependency in the adapter/tests.
        if (value != null && Get(value, "R") is byte r && Get(value, "G") is byte g && Get(value, "B") is byte b)
            return Color.FromArgb(Get(value, "A") is byte a ? a : 255, r, g, b);
        return fallback;
    }
}
