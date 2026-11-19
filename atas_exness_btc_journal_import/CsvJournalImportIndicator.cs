using System.ComponentModel;
using System.Threading.Tasks;
using ATAS.Indicators;

namespace ExnessJournal;

[DisplayName("Exness CSV Journal Import")]
[Category("Custom")]
[Description("添加到图表时，将已准备的 Exness 黄金、BTCUSD、USTEC 成交写入 DEMO253F9 的交易日志。")]
public sealed class CsvJournalImportIndicator : Indicator
{
    public CsvJournalImportIndicator()
    {
        DenyToChangePanel = true;
        ((ValueDataSeries)DataSeries[0]).VisualType = VisualMode.Hide;
    }

    protected override void OnInitialize()
    {
        base.OnInitialize();
        _ = Task.Run(() => PendingJournalImport.Run("journal-import-btc", CsvJournal.Import));
    }

    protected override void OnCalculate(int bar, decimal value) { }

}
