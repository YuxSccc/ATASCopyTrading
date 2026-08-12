using System;
using System.ComponentModel;
using ATAS.Strategies.Chart;

namespace AtasMt5ProbeStrategy
{
    /// <summary>
    /// Compatibility type for charts saved with the pre-v3 standalone basis
    /// collector. Basis collection now lives in AtasBridgeProbeStrategy so the
    /// chart only needs one production strategy. Keeping this type loadable is
    /// important because ATAS persists the CLR type name in workspace files.
    /// </summary>
    [Category("Custom")]
    [DisplayName("AtasMt5BasisCollector (legacy compatibility)")]
    public sealed class AtasMt5BasisCollectorStrategy : ChartStrategy
    {
        protected override void OnCalculate(int bar, decimal value)
        {
            // Intentionally empty: basis collection is owned by the Probe.
        }

        protected override void OnStarted()
        {
            StrategyLog.Append(
                "Legacy basis collector loaded for workspace compatibility; " +
                "basis history is collected by AtasMt5BridgeProbe.\n");
        }

        protected override void OnSuspended()
        {
            StrategyLog.Append("Legacy basis collector suspended; no action taken.\n");
        }

        protected override void OnStopped()
        {
            StrategyLog.Append("Legacy basis collector stopped; no action taken.\n");
        }
    }
}
