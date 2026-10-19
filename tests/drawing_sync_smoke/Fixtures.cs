using System;
using System.Collections.Generic;
using System.Drawing;

namespace DrawingFixtures
{
    // Deliberately arbitrary private field name exercises TYPE-based ChartCore discovery.
    public sealed class ChartCoordinates
    {
        private readonly OFT.Platform.ViewModels.Charting.ChartCore renamedPrivateField;
        public ChartCoordinates(OFT.Platform.ViewModels.Charting.ChartCore core) => renamedPrivateField = core;
    }

    public sealed class Instrument { }
    public sealed class ChartData
    {
        public Collection DrawingObjects { get; } = new();
        public Instrument Instrument { get; } = new();
    }

    public sealed class Collection
    {
        public List<object> Items { get; } = new();
        public ICollection<object> GetAllObjects(Instrument instrument) => Items;
    }

    public class Anchors
    {
        public string Id { get; set; } = "rectangle-1";
        public bool IsCompleted { get; set; } = true;
        public bool Visible { get; set; } = true;
        public bool IsGlobal { get; set; }
        public DateTime _firstTime { get; set; } = new(2026, 10, 19, 12, 0, 0);
        public DateTime _secondTime { get; set; } = new(2026, 10, 19, 12, 5, 0);
        public decimal TrueFirstPrice { get; set; } = 4000;
        public decimal TrueSecondPrice { get; set; } = 4010;
        public Color Color { get; set; } = Color.Red;
        public Color AreaColor { get; set; } = Color.Blue;
        public int Width { get; set; } = 3;
        public string Style { get; set; } = "Dash";
    }

    public sealed class RectangleObj : Anchors
    {
        public bool ExtendRight { get; set; } = true;
    }

    public sealed class TrendLine : Anchors
    {
        public string LineRenderType { get; set; } = "Ray";
        public bool Ray { get; set; } = true;
    }
}

namespace OFT.Platform.ViewModels.Charting
{
    public sealed class ChartCore
    {
        public DrawingFixtures.ChartData Data { get; } = new();
    }
}
