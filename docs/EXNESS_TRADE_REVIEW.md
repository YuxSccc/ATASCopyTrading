# Exness Trade Review

在 ATAS 的 Custom 分类中添加 `Exness Trade Review`。图表打开时直接显示本机已保存的交易，包括以前从 CSV 导入的历史。左上角的「同步 Exness API」用于获取新成交。默认复盘别名为 `Exness`，多个图表使用同一别名即可查看同一批交易。

| ATAS 图表 | Exness 来源 |
| --- | --- |
| GC | XAUUSD |
| NQ | USTEC |
| ES | US500 |
| BTCUSDT@BinanceFutures（币安 USDT 永续） | BTCUSD |

BTC 箭头使用币安 BTCUSDT 永续成交价。指标优先从逐笔目录的 `BTCUSDT@BinanceFutures\BTCUSDT-aggTrades-yyyy-MM-dd.zip` 读取币安 USDⓈ-M 日归档，未匹配的时刻再向当前 ATAS 图表请求逐笔。把图表历史加载到成交当天，悬停箭头可以同时查看 Exness 原价和该时刻的币安价格。普通 BTCUSD 每手为 1 BTC。NQ 图上普通 USTEC 每手显示为 0.05 张等值 NQ；USTEC_x100 每手显示为 5 张等值 NQ。换算仅用于悬停提示，开平仓时间和箭头位置由成交记录与图表行情决定。

微型合约和其他图表不显示标记。买入用向上箭头，卖出用向下箭头；箭尖指向期货参考价，同一票号的开平仓之间用虚线连接。悬停箭头或线段可看成交时间（UTC）、手数、原始成交价、期货逐笔的价格和时间，以及平仓 profit。点击标记不会弹窗或跳转。箭头横向固定在成交所属 K 线的中心，期货价格仍按成交时间匹配。在指标设置的「显示」分组可调箭头大小（默认 18 像素）和连线粗细（默认 3 像素）。图表须已加载相应历史。

每个箭头使用 Exness 开仓或平仓时刻往前 5 秒内的最后一笔市场成交。API 记录保留小数秒，旧 CSV 的秒级记录以该秒起点为准。

「逐笔目录」可填本地行情的上级目录，默认是 `E:\ATASExports\Level1`。对于 GC、NQ、ES，指标先查该品种的日文件，再向 ATAS 查询当前图表合约的历史逐笔，以 ATAS 返回的匹配优先。本地期货文件只标品种，使用本地价格时还要检查该价格是否落在对应 K 线范围内。目录留空即可只用 ATAS 行情。BTC 的查找顺序见上文。指标只读该目录，导出或更新行情文件是独立操作。图表左上角的匹配数按开平仓端点计算，两个端点都有匹配价格且图表覆盖相应时间时才能画出完整连线。

### 手动同步 Exness API

在图表左上角点击「同步 Exness API」，指标会读取已配置的 Exness 交易账号，更新本机复盘记录，并把符合条件的已平仓交易同步到 ATAS 原生 Journal。成功后可继续看盘，遇到错误时才弹窗提示。普通 USTEC 的箭头显示在 NQ 图上。

同一图表实例会在内存中复用已匹配的开平仓价格。例如已有开仓箭头的 NQ 交易平仓后，只需要为新增的平仓时刻匹配价格。其他品种新增成交也不会触发当前图表重新查询旧箭头。首次添加、重新加载指标或更改逐笔目录时，当前图表需要重新匹配。行情目录只读。

首次 API 同步读取最近 24 小时；后续从上一次成功时刻向前重叠 1 小时，分页补齐新成交。重复点击按交易账户和成交 ID 去重。来源账户和旧 CSV 的票号分别保存。历史中的资金出入不产生图表标记。成交时间使用 API 提供的 UTC 小数秒，NQ 箭头使用对应时刻的期货行情。

本地数据在 `%LOCALAPPDATA%\AtasExnessReview`。同步程序在其 `api` 子目录中运行，只读密钥保存在当前用户的 Windows 凭据管理器。更换密钥时，在本机运行 `python "%LOCALAPPDATA%\AtasExnessReview\api\exness_credentials.py"`（Windows 命令提示符）。同步由按钮触发，无需运行 MT5。

每次同步时，插件优先使用图表选中的、已连接的 ATAS DEMO 账户。只有一个 DEMO 已连接时可自动使用它；多个 DEMO 同时在线时，可在图表上选定目标账户，再点同步。在 Trading Journal 里选择同一个账户。换用新 DEMO 后，插件可将本机 API 历史中符合条件的平仓交易补入新账户，旧 CSV 导入的成交仍在原 Journal 账户。图表继续显示当前别名下保存的全部交易。

普通 USTEC 按原价和 `手数 ÷ 20` 写入 NQ，黄金按原价、原手数写入 GC，BTCUSD 按 BTC 数量写入币安 BTCUSDT 永续。GC 和 NQ 已配置 2026 年 11 月成交使用的 Z6 合约。其他交割月、部分平仓或暂时无法核对的费用会在同步时提示原因。重叠持仓按 ATAS 原生规则配对。

迁移到另一台 Windows 电脑时，把 `ExnessTradeReview.dll` 和 `ExnessBtcJournalImport.dll` 安装到 ATAS Indicators，将仓库 `atas_exness_review` 下的 `exness_sync_history.py` 和 `exness_credentials.py` 复制到上述 `api` 目录。本机的 `python.exe` 需可从 PATH 启动，并安装 `requests`、`cryptography`（支持 `public_bytes_raw`）和 `pywin32`。在新电脑的 Windows 用户下重新配置只读凭据。
