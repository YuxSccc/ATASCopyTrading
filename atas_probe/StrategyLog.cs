using System;
using System.IO;

namespace AtasMt5ProbeStrategy
{
    internal static class StrategyLog
    {
        private static readonly object Sync = new object();
        private static readonly string LogDirectory = ResolveLogDirectory();

        public static void Append(string message)
        {
            try
            {
                Directory.CreateDirectory(LogDirectory);
                var path = Path.Combine(LogDirectory, "AtasMt5Probe-" + DateTime.Now.ToString("yyyy-MM-dd") + ".log");
                lock (Sync)
                    File.AppendAllText(path, DateTime.Now.ToString("O") + " " + message);
            }
            catch
            {
                // Probe diagnostics must never prevent ATAS from loading a strategy.
            }
        }

        private static string ResolveLogDirectory()
        {
            var documents = Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments);
            return string.IsNullOrWhiteSpace(documents)
                ? Path.Combine(Path.GetTempPath(), "ATASLogs")
                : Path.Combine(documents, "ATASLogs");
        }
    }
}
