using System;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Runtime.Loader;
using System.Threading.Tasks;
using ATAS.DataFeedsCore;
using CommonServiceLocator;
using ExnessJournal;
using OFT.Platform.Core.DataFeedManager.Replay;
using OFT.Platform.Core.Managers.Connections;
using OFT.Platform.Core.Managers.Database;
using OFT.Platform.Core.Managers.Database.Registries;
using OFT.Platform.Core.Managers.Statistics;
using OFT.Platform.Core.Providers;
using OFT.Platform.Settings.Common;
using Utils.Common.Network.Client;

internal static class Program
{
    private const string Header = "ticket,opening_time_utc,closing_time_utc,type,lots,symbol,opening_price,closing_price,profit\n";
    private const string Closed = "API-12-987,2026-11-15 03:00:05,2026-11-15 03:01:01,buy,0.5,USTEC,23000.25,23001.25,0.50";
    private const string LegacyId = "Exness.USTEC.API-12-987.O";

    [STAThread]
    private static void Main()
    {
        string platform = Environment.GetEnvironmentVariable("ATAS_HOME") ?? @"D:\ATAS Platform";
        AssemblyLoadContext.Default.Resolving += (context, name) =>
        {
            string path = Path.Combine(platform, name.Name + ".dll");
            return File.Exists(path) ? context.LoadFromAssemblyPath(path) : null;
        };
        var directory = Directory.CreateTempSubdirectory("atas-demo-journal-");
        try { Run(directory.FullName); }
        finally { directory.Delete(recursive: true); }
        Console.WriteLine("PASS: DEMO selection, idempotent sync without reload, changed Journal detection, partial-write repair and CDB reopen");
    }

    [MethodImpl(MethodImplOptions.NoInlining)]
    private static void Run(string root)
    {
        var old = new Portfolio { AccountID = "DEMO_OLD" };
        var next = new Portfolio { AccountID = "DEMO_NEW" };
        var real = new Portfolio { AccountID = "DEMO_REAL", IsRealAccount = true };
        var stale = new Portfolio { AccountID = "DEMO_STALE", ConnectionState = ConnectionStates.Disconnected };
        var switched = Connections(FakeConnector(false, old), FakeConnector(true, next, stale, real));
        var both = Connections(FakeConnector(true, old, next, next, real));
        Check(ApiJournalSync.SelectDemoAccount(switched, old.AccountID) == next.AccountID);
        Check(ApiJournalSync.SelectDemoAccount(switched, real.AccountID) == next.AccountID);
        Check(ApiJournalSync.SelectDemoAccount(both, next.AccountID) == next.AccountID);
        Check(ApiJournalSync.SelectDemoAccount(both, old.AccountID) == old.AccountID);
        Rejects(() => ApiJournalSync.SelectDemoAccount(both, real.AccountID));
        Rejects(() => ApiJournalSync.SelectDemoAccount(Connections(FakeConnector(false, old), FakeConnector(true, real)), null));

        var paths = Stub.For<IAppDataPathProvider>((method, args) =>
        {
            if (method.Name != "GetPath") return null;
            string directory = Path.Combine(root, args[0]!.ToString()!);
            Directory.CreateDirectory(directory);
            return args.Length > 1 && args[1] is string file ? Path.Combine(directory, Path.GetFileName(file)) : directory;
        });
        ICacheManager Open()
        {
            ICacheManager cache = new FileCacheManager(paths, Stub.For<IArchiveManager>(), new PlatformSettings());
            cache.Initialize();
            return cache;
        }
        ServiceLocator.SetLocatorProvider(() => Stub.For<IServiceLocator>((method, args) =>
        {
            var service = method.IsGenericMethod ? method.GetGenericArguments()[0] : (Type)args[0]!;
            return DispatchProxy.Create(service, typeof(Stub));
        }));
        using (var cache = Open())
        {
            cache.Registry<IPortfoliosRegistry>().Save(old, true);
            cache.Registry<IPortfoliosRegistry>().Save(next, true);
            var security = new Security { SecurityId = "NQZ6@CME", Code = "NQZ6", Exchange = "CME", TickSize = .25m, TickCost = 5m };
            cache.Registry<ISecuritiesRegistry>().Save(security);
            var stats = new DbStatisticsManager(cache.Registry<IOrdersRegistry>(), cache.Registry<IMyTradesRegistry>(),
                cache.Registry<IPositionsRegistry>(), switched, cache.Registry<IHistoryMyTradeRegistry>(),
                cache.Registry<IDailyNoteRegistry>(), cache.Registry<IPlaybookRegistry>(), Stub.For<IReplayConnector>(),
                Stub.For<IPlatformTradingCore>(), cache.Registry<IPositionSyncPointRegistry>());
            stats.InitAsync().GetAwaiter().GetResult();

            // Simulate an old version interrupted after writing the entry, before saving the exit.
            stats.Add(new MyTrade { AccountID = old.AccountID, Portfolio = old, SecurityId = security.SecurityId,
                Security = security, Id = LegacyId, OrderId = LegacyId, Route = "Exness CSV", OrderDirection = OrderDirections.Buy,
                Time = new DateTime(2026, 11, 15, 3, 0, 5, DateTimeKind.Utc), Price = 23000.25m, Volume = .025m, OpenVolume = .025m });
            int flushes = 0, reloads = 0;
            bool failNextReload = false;
            var countedCache = Stub.For<ICacheManager>((method, args) =>
            {
                if (method.Name == nameof(ICacheManager.FlushAsync)) flushes++;
                return method.Invoke(cache, args);
            });
            var countedStats = Stub.For<ITradingStatisticsManager>((method, args) =>
            {
                if (method.Name == nameof(ITradingStatisticsManager.ReloadStatisticsAsync))
                {
                    reloads++;
                    if (failNextReload)
                    {
                        failNextReload = false;
                        return Task.FromException(new IOException("test reload failure"));
                    }
                }
                return method.Invoke(stats, args);
            });
            (bool Success, string Message) Sync(string data, Func<string> target)
            {
                string path = Path.Combine(root, "snapshot.csv");
                File.WriteAllText(path, Header + data);
                return ApiJournalSync.Import(path, "12", countedCache, countedStats, target).GetAwaiter().GetResult();
            }
            string NoAccountNeeded() => throw new Exception("Unexpected DEMO account lookup");
            Check(Sync("", NoAccountNeeded).Success);
            Check(Sync("API-12-1,2026-11-15 02:00:00,,buy,1,USTEC,23000,,", NoAccountNeeded).Success);
            Check(!Sync(Closed.Replace("USTEC", "OTHER"), NoAccountNeeded).Success);
            foreach (var portfolio in new[] { old, next, next, old })
            {
                var result = Sync(Closed, () => portfolio.AccountID!);
                Check(result.Success, result.Message);
            }
            Check(flushes == 2 && reloads == 2);

            // Even on the fast path, read back the actual rows. A native Journal edit
            // must not change the remembered expected values through a shared reference.
            var history = cache.Registry<IHistoryMyTradeRegistry>();
            var closed = history.Items.Single(h => h.AccountID == old.AccountID);
            var originalPrice = closed.OpenPrice;
            closed.OpenPrice += 1;
            history.Save(closed);
            Check(!Sync(Closed, () => old.AccountID!).Success);
            closed.OpenPrice = originalPrice;
            history.Save(closed);
            Check(Sync(Closed, () => old.AccountID!).Success);
            Check(flushes == 2 && reloads == 2);

            // Here no new execution is needed, but OpenVolume needs repair. If the
            // statistics refresh then fails, the next no-change sync must still retry it.
            var executions = cache.Registry<IMyTradesRegistry>();
            var entry = executions.TryGetMyTrade(old.AccountID, LegacyId, true)!;
            entry.OpenVolume = entry.Volume;
            executions.Save(entry);
            failNextReload = true;
            try { Sync(Closed, () => old.AccountID!); throw new Exception("Expected reload failure"); }
            catch (IOException e) when (e.Message == "test reload failure") { }
            Check(flushes == 3 && reloads == 3 && entry.OpenVolume == 0);
            Check(Sync(Closed, () => old.AccountID!).Success);
            Check(flushes == 4 && reloads == 4);
            var mixed = Sync(Closed + "\n" + Closed.Replace("USTEC", "OTHER").Replace("API-12-987", "API-12-2"), () => old.AccountID!);
            Check(!mixed.Success);
            Check(flushes == 4 && reloads == 4);
        }
        using var reopened = Open();
        foreach (var portfolio in new[] { old, next })
        {
            var fills = reopened.Registry<IMyTradesRegistry>().GetMyTrades(portfolio.AccountID).ToArray();
            var match = reopened.Registry<IHistoryMyTradeRegistry>().Items.Single(h => h.AccountID == portfolio.AccountID);
            Check(fills.Length == 2 && fills.All(f => f.OpenVolume == 0) && match.PnL == .50m);
            Check(fills.Any(f => f.Id == LegacyId) == (portfolio == old));
        }
    }

    private static IDataFeedConnector FakeConnector(bool connected, params Portfolio[] portfolios) =>
        Stub.For<IDataFeedConnector>((method, _) => method.Name switch
        {
            "get_IsConnected" => connected,
            "get_Portfolios" => portfolios,
            _ => null
        });

    private static IConnectorsManager Connections(params IDataFeedConnector[] connectors) =>
        Stub.For<IConnectorsManager>((method, _) => method.Name == "get_Connectors" ? connectors : null);

    private static void Rejects(Action action)
    {
        try { action(); }
        catch (InvalidOperationException) { return; }
        throw new Exception("Expected account selection to fail");
    }

    private static void Check(bool condition, string? message = null, [CallerArgumentExpression(nameof(condition))] string? expression = null)
    {
        if (!condition) throw new Exception(message ?? expression);
    }
}

public class Stub : DispatchProxy
{
    private Func<MethodInfo, object?[], object?>? _handler;

    public static T For<T>(Func<MethodInfo, object?[], object?>? handler = null) where T : class
    {
        var instance = Create<T, Stub>();
        ((Stub)(object)instance)._handler = handler;
        return instance;
    }

    protected override object? Invoke(MethodInfo? method, object?[]? args)
    {
        var type = method!.ReturnType;
        var value = _handler?.Invoke(method, args ?? []);
        if (value != null) return value;
        if (type == typeof(void)) return null;
        if (type == typeof(Task)) return Task.CompletedTask;
        if (type.IsInterface && type.IsGenericType && type.GetGenericArguments().Length == 1)
        {
            var empty = Array.CreateInstance(type.GetGenericArguments()[0], 0);
            if (type.IsInstanceOfType(empty)) return empty;
        }
        return type.IsValueType ? Activator.CreateInstance(type) : null;
    }
}
