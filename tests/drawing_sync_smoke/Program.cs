using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Reflection.Metadata;
using System.Reflection.PortableExecutable;
using CrossInstrumentDrawings;
using DrawingFixtures;

var passed = 0;
void Check(bool condition, string name)
{
    if (!condition) throw new InvalidOperationException("FAILED: " + name);
    passed++;
    Console.WriteLine("PASS: " + name);
}
Check(InstrumentIdentity.Root("MGCZ6@COMEX") == "MGC" && InstrumentIdentity.Root("GCZ26") == "GC",
    "MGC and GC routing stays distinct");
Check(InstrumentIdentity.Root("NQZ6") == "" && InstrumentIdentity.Root("MGCLABEL") == "", "other symbols rejected");
Check(InstrumentIdentity.Contract("MGCZ6@COMEX", 2026) == InstrumentIdentity.Contract("GCZ26", 2026),
    "single and double-digit contract years match");
Check(!InstrumentIdentity.ContractsMatch("Z2026", "G2027"), "different expiries blocked");
Check(InstrumentIdentity.ContractsMatch("", "Z2026"), "unknown expiry allowed with UI advisory");

var core = new OFT.Platform.ViewModels.Charting.ChartCore();
var chart = new ChartCoordinates(core);
var rectangle = new RectangleObj();
var line = new TrendLine { Id = "line-1", IsGlobal = true };
core.Data.DrawingObjects.Items.AddRange(new object[] { rectangle, line, new object() });
var owner = Guid.NewGuid();
var now = new DateTimeOffset(2026, 10, 19, 12, 0, 0, TimeSpan.Zero);
var reader = new NativeDrawingReader();
var first = reader.Capture(chart, owner, "MGCZ6", false, now);
Check(ReferenceEquals(NativeDrawingReader.ResolveCore(chart), core), "private field discovery tolerates renamed field");
Check(first.Shapes.Count == 2 && first.TotalObjects == 3 && first.SkippedObjects == 1,
    "capture rectangles and trend lines only");
Check(first.Shapes[0].FirstTime == rectangle._firstTime && first.Shapes[0].FirstPrice == 4000 &&
    first.Shapes[0].Color == Color.Red && first.Shapes[0].FillColor == Color.Blue,
    "read inherited time/price anchors and preserve colors");
Check(first.Shapes[0].ExtendRight && first.Shapes[1].Extent == LineExtent.Ray, "extension modes retained");
Check(reader.Capture(chart, owner, "MGCZ6", true, now).Shapes.Count == 1, "Global-only filter optional");
rectangle.TrueFirstPrice = 4020;
var moved = reader.Capture(chart, owner, "MGCZ6", false, now.AddMilliseconds(250));
Check(moved.Shapes[0].FirstPrice == 4020 && first.Shapes[0].FirstPrice == 4000, "drag replaces snapshot; old snapshot independent");
rectangle.Visible = false;
Check(reader.Capture(chart, owner, "MGCZ6", false, now).Shapes.Count == 1, "hidden drawing removed from next snapshot");
rectangle.Visible = true;
rectangle.IsCompleted = false;
Check(reader.Capture(chart, owner, "MGCZ6", false, now).Shapes.Count == 1, "unfinished drawing excluded");
rectangle.IsCompleted = true;
rectangle.TrueFirstPrice = 0;
var invalidRejected = false;
try { reader.Capture(chart, owner, "MGCZ6", false, now); }
catch (InvalidOperationException) { invalidRejected = true; }
Check(invalidRejected, "invalid anchors fail capture instead of broadcasting false deletion");
rectangle.TrueFirstPrice = 4000;

var bus = new DrawingBus();
Check(bus.Publish("a", first), "source publishes");
Check(!bus.Publish("a", first with { Owner = Guid.NewGuid() }), "second source cannot overwrite active source");
Check(bus.Read("b", now) == null, "channels isolated");
Check(bus.Read("a", now.AddSeconds(4)) == null, "stale publisher expires");
core.Data.DrawingObjects.Items.Clear();
var empty = reader.Capture(chart, owner, "MGCZ6", false, now.AddSeconds(1));
bus.Publish("a", empty);
Check(bus.Read("a", now.AddSeconds(1))?.Shapes.Count == 0, "delete-all propagates via authoritative empty snapshot");
bus.RemoveOwner(Guid.NewGuid());
Check(bus.Read("a", now.AddSeconds(1)) != null, "other source cannot unpublish owner");
bus.RemoveOwner(owner);
Check(bus.Read("a", now.AddSeconds(1)) == null, "closing source unpublishes drawings");

var t = new DateTime(2026, 10, 19, 12, 0, 0);
var targetTimes = new[] { t, t.AddMinutes(5), t.AddMinutes(10) };
Check(TimeProjection.TryBar(targetTimes, t.AddMinutes(2.5), out var position) && position == 0.5,
    "time anchor maps between GC bars, without MGC bar index");
Check(!TimeProjection.TryBar(targetTimes, t.AddMinutes(-1), out _), "missing old history detected");
Check(TimeProjection.TryBar(targetTimes, t.AddMinutes(15), out position) && position == 3, "future time anchor projects beyond last bar");
Check(TimeProjection.TryBar(new[] { t, t, t.AddMinutes(5) }, t, out position) && position == 1,
    "duplicate non-time bar timestamps deterministic");
var bounds = new Rectangle(0, 0, 100, 100);
Check(TimeProjection.ClipLine(-10, -10, 110, 110, bounds, LineExtent.Segment, out var a, out var b) &&
    a == new PointF(0, 0) && b == new PointF(100, 100), "trend segments clip to viewport");
Check(TimeProjection.ClipLine(20, 20, 40, 40, bounds, LineExtent.Ray, out a, out b) &&
    a == new PointF(20, 20) && b == new PointF(100, 100), "rays extend only in original endpoint direction");
Check(TimeProjection.ClipLine(20, 20, 40, 40, bounds, LineExtent.Infinite, out a, out b) &&
    a == new PointF(0, 0) && b == new PointF(100, 100), "extended line clips both directions");
Check(!TimeProjection.ClipLine(-2, 0, -2, 100, bounds, LineExtent.Infinite, out _, out _), "offscreen parallel line skipped");

// Verify actual installed metadata in addition to the isolated behavioral fixtures.
if (args.Length == 1)
{
    using var file = File.OpenRead(args[0]);
    using var pe = new PEReader(file);
    var md = pe.GetMetadataReader();
    TypeDefinition Find(string fullName) => md.TypeDefinitions.Select(md.GetTypeDefinition)
        .Single(x => md.GetString(x.Namespace) + "." + md.GetString(x.Name) == fullName);
    bool HasProperty(TypeDefinition type, string name) => type.GetProperties().Any(x => md.GetString(md.GetPropertyDefinition(x).Name) == name);
    Check(HasProperty(Find("OFT.Platform.ViewModels.Charting.ChartCore"), "Data") &&
        HasProperty(Find("OFT.Platform.ViewModels.Charting.ChartData"), "DrawingObjects") &&
        HasProperty(Find("OFT.Platform.ViewModels.Charting.ChartData"), "Instrument"), "installed ATAS core/data read path present");
    Check(Find("OFT.Platform.DrawingObjects.DrawingObjectsCollection").GetMethods()
        .Any(x => md.GetString(md.GetMethodDefinition(x).Name) == "GetAllObjects"), "installed drawing enumerator present");
    Check(HasProperty(Find("OFT.Platform.DrawingObjects.TwoCoordinateDrawingObject"), "TrueFirstPrice") &&
        HasProperty(Find("OFT.Platform.DrawingObjects.RectangleObj"), "_firstTime"), "installed anchor accessors present");
}
Console.WriteLine($"{passed} checks passed.");
