using System;
using System.ComponentModel;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;

namespace ExnessReview;

internal static class ApiHistoryImport
{
    public static async Task<int> RunAsync(string account, string? chartAccountId)
    {
        var directory = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "AtasExnessReview", "api");
        var script = Path.Combine(directory, "exness_sync_history.py");
        if (!File.Exists(script))
            throw new FileNotFoundException("请先安装 Exness API 同步脚本。", script);
        var output = Path.Combine(directory, $"chart-{Guid.NewGuid():N}.csv");
        // The lock covers both fetching and applying the snapshot, including across ATAS reloads.
        using var lease = AcquireSyncLock(Path.Combine(directory, "chart-sync.lock"));
        try
        {
            var start = new ProcessStartInfo("python.exe")
            {
                CreateNoWindow = true, UseShellExecute = false, WorkingDirectory = directory,
                RedirectStandardOutput = true, StandardOutputEncoding = Encoding.UTF8,
                RedirectStandardError = true, RedirectStandardInput = true
            };
            foreach (var argument in new[] { "-X", "utf8", script, "--output", output })
                start.ArgumentList.Add(argument);
            using var process = new Process { StartInfo = start };
            try { process.Start(); }
            catch (Win32Exception ex)
            {
                throw new InvalidOperationException("无法启动 python.exe。请检查 ATAS 可用的 Python 安装路径。", ex);
            }
            process.StandardInput.Close();
            var stdout = process.StandardOutput.ReadToEndAsync();
            var stderr = process.StandardError.BaseStream.CopyToAsync(Stream.Null);
            using var deadline = new CancellationTokenSource(TimeSpan.FromMinutes(10));
            try { await process.WaitForExitAsync(deadline.Token).ConfigureAwait(false); }
            catch (OperationCanceledException) when (deadline.IsCancellationRequested)
            {
                if (!process.HasExited) process.Kill(entireProcessTree: true);
                await process.WaitForExitAsync().ConfigureAwait(false);
                throw new TimeoutException("读取 Exness 历史超时，请重试。");
            }
            await stderr.ConfigureAwait(false);
            var response = await stdout.ConfigureAwait(false);
            if (process.ExitCode == 3)
                throw new InvalidOperationException($"请先在本机运行以下脚本设置只读 API 密钥。\n{Path.Combine(directory, "exness_credentials.py")}");
            if (process.ExitCode != 0)
                throw new InvalidOperationException($"读取 Exness 历史失败（退出码 {process.ExitCode}）。请检查网络和本机 API 配置。");
            string sourceAccountId;
            int rows;
            HashSet<string> unmatched;
            try
            {
                using var json = JsonDocument.Parse(response);
                var root = json.RootElement;
                sourceAccountId = root.GetProperty("account_id").GetString() ?? "";
                rows = root.GetProperty("rows").GetInt32();
                unmatched = root.GetProperty("unmatched_position_ids").EnumerateArray()
                    .Select(item => item.GetString() ?? "").ToHashSet(StringComparer.Ordinal);
                if (rows < 0 || unmatched.Count != root.GetProperty("unmatched_positions").GetInt32() ||
                    unmatched.Any(id => id.Length == 0 || !id.All(char.IsAsciiDigit))) throw new FormatException();
            }
            catch (Exception ex) when (ex is JsonException or InvalidOperationException or FormatException or System.Collections.Generic.KeyNotFoundException)
            {
                throw new InvalidDataException("API 同步脚本返回的结果格式无效。");
            }
            ReviewStore.ImportApiSnapshot(account, sourceAccountId, output, rows, unmatched);
            if (unmatched.Count == 0)
                await SyncJournalAsync(output, sourceAccountId, chartAccountId).ConfigureAwait(false);
            return unmatched.Count;
        }
        finally
        {
            try { File.Delete(output); }
            catch (IOException) { }
            catch (UnauthorizedAccessException) { }
        }
    }

    private static async Task SyncJournalAsync(string csvPath, string sourceAccountId, string? chartAccountId)
    {
        // ATAS loads chart and Journal extensions independently. The chart cache is usable
        // even when the optional Journal extension has not been loaded into this host.
        try
        {
            var method = AppDomain.CurrentDomain.GetAssemblies()
                .Where(assembly => assembly.GetName().Name == "ExnessBtcJournalImport")
                .Select(assembly => assembly.GetType("ExnessJournal.ApiJournalSync")?.GetMethod("SyncClosedTradesWithStatusAsync",
                    [typeof(string), typeof(string), typeof(string)]))
                .FirstOrDefault(candidate => candidate != null);
            if (method == null)
                throw new InvalidOperationException("尚未加载新版 API 日志组件。更新指标库后可再次同步。");
            var sync = method.CreateDelegate<Func<string, string, string?, Task<(bool Success, string Message)>>>();
            var result = await sync(csvPath, sourceAccountId, chartAccountId).ConfigureAwait(false);
            if (!result.Success) throw new InvalidOperationException(result.Message);
        }
        catch (Exception ex)
        {
            throw new InvalidOperationException($"图表记录已同步。ATAS 原生 Journal 同步出错。\n{ex.Message}", ex);
        }
    }

    private static FileStream AcquireSyncLock(string path)
    {
        try { return new FileStream(path, FileMode.OpenOrCreate, FileAccess.Write, FileShare.None); }
        catch (IOException ex) when ((ex.HResult & 0xffff) is 32 or 33)
        {
            throw new InvalidOperationException("另一个 ATAS 实例正在同步 Exness 记录。", ex);
        }
    }
}
