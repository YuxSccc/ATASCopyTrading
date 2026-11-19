using System;
using System.IO;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using CommonServiceLocator;
using OFT.Platform.Core.Managers.Database;
using OFT.Platform.Core.Managers.Statistics;

namespace ExnessJournal;

internal static class PendingJournalImport
{
    private static readonly SemaphoreSlim Gate = new(1);

    internal static async Task<T> RunExclusive<T>(Func<Task<T>> work)
    {
        await Gate.WaitAsync().ConfigureAwait(false);
        try { return await work().ConfigureAwait(false); }
        finally { Gate.Release(); }
    }

    internal static async Task Run<T>(string folder, Func<string, ICacheManager, ITradingStatisticsManager, Task<T>> import) where T : class
    {
        string directory = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "AtasExnessReview", folder);
        string pending = Path.Combine(directory, "pending.csv");
        await Gate.WaitAsync();
        try
        {
            if (!File.Exists(pending)) return;
            using var reservation = new FileStream(Path.Combine(directory, "import.lock"), FileMode.OpenOrCreate,
                FileAccess.Write, FileShare.None);
            using (var inputLock = new FileStream(pending, FileMode.Open, FileAccess.Read, FileShare.Read))
            {
                var services = ServiceLocator.Current;
                var result = await import(pending, services.GetInstance<ICacheManager>(),
                    services.GetInstance<ITradingStatisticsManager>());
                WriteReport(directory, result);
            }
            File.Move(pending, Path.Combine(directory, "imported-" + Guid.NewGuid().ToString("N") + ".csv"));
        }
        catch (Exception error)
        {
            try { WriteReport(directory, new { Status = "error", Error = error.ToString() }); }
            catch (IOException) { }
            catch (UnauthorizedAccessException) { }
        }
        finally { Gate.Release(); }
    }

    private static void WriteReport(string directory, object data)
    {
        string temp = Path.Combine(directory, "result-" + Guid.NewGuid().ToString("N") + ".tmp");
        File.WriteAllText(temp, JsonSerializer.Serialize(data, new JsonSerializerOptions { WriteIndented = true }));
        File.Move(temp, Path.Combine(directory, "result.json"), true);
    }
}
