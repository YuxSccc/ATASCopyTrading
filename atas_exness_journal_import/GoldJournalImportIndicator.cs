using System.ComponentModel;
using System.Threading.Tasks;
using ATAS.Indicators;

namespace ExnessJournal;

[DisplayName("Exness GC Journal Import")]
[Category("Custom")]
[Description("添加到图表时，将已准备好的 Exness 黄金成交写入 DEMO253F9 的 GCZ6 交易日志。")]
public sealed class GoldJournalImportIndicator : Indicator
{
    public GoldJournalImportIndicator()
    {
        DenyToChangePanel = true;
        ((ValueDataSeries)DataSeries[0]).VisualType = VisualMode.Hide;
    }

    protected override void OnInitialize()
    {
        base.OnInitialize();
        _ = Task.Run(() => PendingJournalImport.Run("journal-import", GoldJournal.Import));
    }

    protected override void OnCalculate(int bar, decimal value) { }

}
