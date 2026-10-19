# GC/MGC 手动区域与趋势线单向同步 V1

指标名称：`Custom → GC-MGC Drawing Sync V1`。

在同一个 ATAS Classic 进程中，将 MGC 图表上的原生矩形区域、趋势线投射到 GC 图表。
MGC 上新增、拖动、隐藏或删除对象后，GC 的显示在约 250 ms 后跟随更新。
GC 上显示的是指标图层，需要回 MGC 编辑；不会在 GC 创建可拖动的原生绘图对象。

V1.1 将投射对象默认统一为青色，并显示小型 `MGC` 标签，与 GC 自己的绘图区分。
来源 MGC 的绘图颜色不变。此更新保留原指标名称和程序集身份；安装更新后重启 ATAS，
现有两张图上的指标可以继续使用，不必移除后重新添加。

## 首次试用

1. 打开同交割月的 MGC 和 GC 图表，例如 MGCZ6 与 GCZ6。
2. 重启 ATAS 后，在**两张图**都添加 `GC-MGC Drawing Sync V1`。
3. 两边保持 `Mode=Auto`、`Channel=GC-MGC`；MGC 自动发送，GC 自动接收。
4. 在 MGC 用原生矩形工具画一个区域，再用趋势线工具画一条线。不必勾选 Global。
5. GC 图底部应出现 `GC target <- MGC... | zones 1, lines 1`，并显示对应区域和线。
6. 在 MGC 拖动、隐藏、删除这些对象，核对 GC 同步变化。清空来源绘图后 GC 应不再显示副本。

普通图表即可，不要求 Footprint。先用相同的时间周期试验，确认正常后可换为不同时间周期。
GC 必须加载趋势线起点所在时间的历史；若起点早于已加载数据，状态栏会提示加载更多历史。
矩形可以在已加载的历史左边缘裁切显示。

## 参数

| 参数 | 默认 | 作用 |
| --- | --- | --- |
| Mode | Auto | MGC 发送、GC 接收；可指定 SourceMgc/TargetGc，Off 停止当前实例 |
| Channel | GC-MGC | 源和目标必须相同；一条通道只保留一个 MGC 源，可有多个 GC 目标 |
| Global objects only | false | false 同步本源图的局部和 Global 对象；true 只同步 Global 对象 |
| Target price offset | 0 | GC 显示价 = MGC 对象价格 + 偏移，不做合约规模的十倍换算 |
| Zone opacity (%) | 20 | 目标区域填色不透明度 |
| Use source colors | false | false 使用统一映射色；true 恢复来源的边框色和填充色 |
| Mapped color | 青色 RGB(0,180,190) | 目标区域和趋势线的统一颜色，不改 MGC 原图 |
| Show MGC labels | true | 在每个可见投射对象旁标注 MGC，可关闭 |
| Show status | true | 在图表底部显示来源、对象数量及兼容性错误 |

若要两组独立的图表分别同步，把第一组源/目标通道设为 `gold-a`，第二组设为 `gold-b`。
若一个通道装了两个 MGC 源，会显示 Source conflict，不会让两个源交替覆盖彼此。

## 绘图与坐标

- 同步范围仅为原生 `RectangleObj` 和 `TrendLine`；其他绘图、指标产生的线、订单线不参与。
- 矩形左右延伸、趋势线线段/射线/双向延长、线宽及虚线样式会投射到目标。
- 默认使用青色边框和半透明青色填充；开启 Use source colors 后恢复来源配色。
- 区域透明度使用插件的 Zone opacity，不复制来源透明度；文字、箭头、警报不复制。
- 读取真实时间/价格锚点，横坐标重新映射到 GC 的 K 线；不会把 MGC 的 Bar 编号套到 GC。
- 时间锚点落在目标 K 线中间时按时间插值。相同时间戳的非时间 K 线定位到该时间最后一根，
  这类图表不保证逐笔事件级对齐；未来时间按最近有效 K 线间隔估算位置。
- 已识别的合约月份不同则停止显示并提示 Expiry mismatch。
  若图表仅提供 `GC`/`MGC` 等无月份名称，则按通道同步，并提示自行核对同交割月。
- 来源图必须保持打开、指标处于启用状态；关闭来源会撤下副本，来源卡住时副本最多保留三秒。
- GC 自己的原生绘图不修改。来源数据恢复后，会完整替换指标的显示快照。
- 不保存绘图副本，重启后由打开的 MGC 源重新发布；原对象仍由 ATAS 正常保存。

## 内部 API 适配与诊断

此试验版使用本机 ATAS Classic 内部接口，读取路径为：

```text
ChartInfo (ChartCoordinates)
  -> 按字段类型发现 ChartCore
  -> Data.DrawingObjects.GetAllObjects(Data.Instrument)
  -> RectangleObj / TrendLine 的 Id、时间锚点、TrueFirstPrice/TrueSecondPrice
  -> 不包含原生对象引用的只读显示快照
  -> GC 指标图层
```

不依赖被混淆的字段名；平台升级仍可能改变这些类型或成员。
读取错误会显示在底部，错误快照不会被当作“用户删光对象”发布。
插件只调用绘图读取方法，不调用原生绘图新增、保存、删除、克隆或交易方法。
来源读取在 UI dispatcher 上每 250 ms 执行；跨图表仅交换时间、价格和样式值。

每实例每五秒更新一份诊断状态：

```text
%APPDATA%\ATAS\IndicatorData\CrossInstrumentDrawingsV1\status-<实例ID>.json
```

状态文件包含图表品种、通道、已解析对象数量、实际读取路径、错误信息和平台程序集版本。
若加载后不同步，可以直接读取最新状态文件检查，无需改原有绘图：

```powershell
Get-ChildItem "$env:APPDATA\ATAS\IndicatorData\CrossInstrumentDrawingsV1" -Filter '*.json' |
    Sort-Object LastWriteTime -Descending | Select-Object -First 2 |
    ForEach-Object { Get-Content -LiteralPath $_.FullName -Raw }
```

## 构建与检查

```powershell
dotnet build atas_drawing_sync\AtasDrawingSync.csproj -c Release
dotnet run --project tests\drawing_sync_smoke\DrawingSyncSmoke.csproj -c Release -- 'D:\ATAS Platform\OFT.Platform.Core.dll'
```

输出 `atas_drawing_sync\bin\Release\net10.0-windows\CrossInstrumentDrawingsV1.dll`。
安装到 `%APPDATA%\ATAS\Indicators\CrossInstrumentDrawingsV1.dll`。

已有 32 项行为与本机元数据检查通过，覆盖只读对象读取、锚点、样式、修改/隐藏/删除、
源冲突、来源过期、合约月匹配、跨周期投射及视口裁切。构建零警告。
用户已在真实 ATAS 图表中确认 MGC 到 GC 的绘图映射可用；V1.1 使用青色和 MGC 标签
区分映射与本地绘图。状态文件仍可用于后续版本兼容性诊断。
