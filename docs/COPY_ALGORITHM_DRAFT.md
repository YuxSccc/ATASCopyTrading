# ATAS GC → MT5 XAUUSD 复制算法草案 v0.1

> 状态：设计评审稿。本文不启用任何自动下单规则。

## 1. 目标与边界

- ATAS 是交易意图和最终期望仓位的权威来源。
- GC 的每一个 ATAS 订单实例，分别映射为一个 MT5 订单实例；ATAS
  拖动订单产生的“撤旧单 + 新订单”也按两个独立事实处理。
- 订单、成交、撤单、保护单和 MT5 账户状态都必须可审计、可重放。
- 目标是本机桥接不引入固定防抖；不承诺交易所、经纪商或 MT5 服务器
  的成交延迟。
- v0.1 不猜测新旧 ATAS 订单是否属于同一意图，也不把一个 ATAS 订单
  自动改成 MT5 的另一种订单类型。

## 2. 不可违反的规则

1. **ATAS 事件只入队，不等待 MT5。** C# 回调只写入有界、单写者队列并
   立即返回；不在回调线程调用网络或等待确认。
2. **每个 MT5 目标品种只有一个串行协调器。** `XAUUSD` 的创建、撤销、
   改单、补偿和仓位处理绝不并发执行。
3. **只有确认的 MT5 状态能改变映射。** “请求已发送”不能等价于“订单已
   创建/撤销/成交”。
4. **事件缺口立即冻结开仓动作。** 收到缺失的 `event_id`、进程重连、未知
   执行结果或手工篡改时，取消仍可执行，但新增风险必须等待 ATAS 快照与
   MT5 快照协调完成。
5. **只管理桥接自身的 MT5 对象。** 使用独立 magic number、稳定的注释前缀
   和本地映射表；发现未知的 MT5 仓位/订单时默认不触碰，并进入告警状态。

## 3. 三个状态层

```text
ATAS 原始事件 (全局 session_id + event_id)
  -> ATAS canonical state: 净仓位 + 活跃订单实例
  -> desired target state: 由复制策略计算
  -> observed MT5 state: EA 推送交易事务 + 周期快照
```

`desired target state` 不等于“已发送请求”。它只描述桥接希望 MT5 最终具有
的订单和仓位；`observed MT5 state` 才是执行后的事实。

### 3.1 ATAS 订单实例

映射键为：

```text
(ATAS session_id, source_symbol, source_order_id)
```

`ExtId` 仅作为诊断字段。测试已表明移动挂单会得到新的 `Id` 和 `ExtId`，
因此不将其视为跨移动的永久身份。

订单状态：

- `Active`：可创建或保留 MT5 对应挂单；
- `Done, canceled=true`：撤销对应 MT5 挂单；
- `Done, canceled=false`：源订单已成交。等待/处理随后仓位变化，不将其误作
  撤单；
- `Failed/Unknown`：冻结该源订单的新增动作，要求快照。

### 3.2 MT5 命令状态

```text
PENDING_SEND -> SENT -> ACCEPTED -> CONFIRMED
                        |            |
                        v            v
                     REJECTED     FAILED / UNKNOWN
```

- `SENT`：MQL5 EA 已调用 `OrderSendAsync`；不是成交或修改成功。
- `ACCEPTED`：收到服务器请求处理结果；仍可能等待订单/成交/仓位事务。
- `CONFIRMED`：EA 的 `OnTradeTransaction` 加上订单/仓位快照，满足命令的
  后置条件。
- `FAILED` 或 `UNKNOWN`：停止该品种新的风险增加命令，写入持久 journal，
  进入协调流程；不盲目重试。

## 4. 异步与低延迟设计

```text
ATAS callbacks
  -> single ordered outbox (C#)
  -> localhost ingress + append-only journal + immediate ACK
  -> XAUUSD serialized coordinator
  -> MQL5 EA command inbox -> OrderSendAsync
  -> OnTradeTransaction + MT5 snapshots -> coordinator
```

- C# outbox 按 `event_id` 单写者发送，绝不对每条事件启动独立 `Task`；这样
  不会因异步调度使撤单和新单乱序。
- 报价事件参与 `event_id` 连续性检查，但不触发复制决策。
- 仓位回调按完整语义去重（同品种、净量、均价均未变则丢弃），不用 250ms
  防抖。订单事件永不因去重丢弃。
- 每个 XAUUSD 协调器一次最多一个“待确认的风险增加命令”。撤单和减仓命令
  可优先，但也必须记录确认。
- 建议初始阈值：本地 command ACK 100ms、交易事务确认 1s、快照协调 3s。
  超时不是失败重试，而是 `UNKNOWN`。

## 5. GC ↔ XAUUSD 价格映射

GC 是期货，XAUUSD 是现货/CFD；即使短周期基差通常较小，也不能默认为零。
价格映射应在每个**新建或修改来源事件**发生时采样，并记录采样值，便于审计。

### 5.1 默认：按成交侧实时基差映射

```text
Buy  参考价：ask
Sell 参考价：bid
basis_side = XAUUSD_reference - GC_reference
target_price = round_to_XAUUSD_tick(source_price + basis_side)
```

- Buy Limit/Buy Stop 使用 ask 基差；Sell Limit/Sell Stop 使用 bid 基差。
- 以 Last 为触发条件的 stop 使用中间价基差，并在提交前做 bid/ask 有效性
  检查。
- ATAS 没有新的订单事件时，不因基差变化而偷偷移动现有 MT5 订单。
  ATAS 自己移动后产生的新订单，才按新时刻基差创建新的 MT5 订单。

### 5.2 提交前价格保护

提交前必须同时满足：

- 两端报价均新鲜（建议小于 250ms，可配置）；
- 基差绝对值及其短时变化未超过品种配置上限；
- XAUUSD tick size、StopsLevel、FreezeLevel、最小挂单距离均满足；
- 映射后订单仍为同一语义：Buy Limit 在 target ask 下方、Sell Limit 在 target
  bid 上方、Buy Stop 在 ask 上方、Sell Stop 在 bid 下方。

任一条件失败：**不把挂单降级为市价单**。记录 `price_guard_blocked`，等待
下一源事件、报价恢复或人工协调。

### 5.3 可配置的价格策略

1. `side_basis`（建议默认）：上述实时基差；
2. `raw_price`：GC 价格直接作为 XAUUSD 价格，仅用于确认两端报价实际上
   接近的经纪商；
3. `fixed_offset`：人工设置的常量偏移，仅用于临时诊断；
4. `disabled`：仅采集，不复制价格型订单。

合约换月或 MT5 品种规格变化必须触发新的 symbol mapping 版本；旧版本的
活跃订单需要先协调完成，不能静默跨版本复制。

### 5.4 已确认的基差模型：4 小时近期加权曲线

默认价格策略改为 `side_basis_4h`。桥接持续记录 GC 与 XAUUSD 的双边报价，
按买卖方向形成基差序列：

```text
buy_basis(t)  = XAUUSD_ask(t) - GC_ask(t)
sell_basis(t) = XAUUSD_bid(t) - GC_bid(t)
```

每次 ATAS 新建订单时，使用过去 4 小时、时间越近权重越高的基差估计值。实现
应采用指数衰减的加权中位数或截尾加权均值，而不是普通平均值，以免单个坏报价
把挂单整体移走。初始半衰期建议 15 分钟，待收集真实数据后调整。

这里真正需要控制的是**基差估计误差和突变**，而不是基差的绝对值必须接近零：
稳定但非零的期货-现货基差可以被正常转换。每次映射会记录当前基差、4 小时
估计值、加权离散度和短时变化率。

若当前基差偏离估计值、加权离散度或变化率超过暂定阈值，则：

1. 不创建或修改风险增加订单；
2. 撤单与减仓仍允许；
3. 向 ATAS 策略回传 `basis_guard` 告警，并在图表/日志中显示；
4. 阈值先使用保守占位值，实盘采样后再校准。

## 6. 订单生命周期规则（初版）

### 6.1 ATAS 新挂单

1. 记录 ATAS 订单实例和基差样本；
2. 校验 symbol mapping、报价、新鲜度、价格语义、目标账户交易权限与保证金；
3. 创建一个 MT5 pending order，写入 `source instance -> MT5 ticket` 映射；
4. 仅在 MT5 `ORDER_ADD`/快照确认后标记为 `CONFIRMED`。

SL 和 TP 在 v0.1 同样按显式 pending order 镜像，不依赖 `comment=SL/TP`、
`IsAttached` 或 OCO 字段。这样可以完整保留 ATAS 的创建、撤销和重建语义。
将保护单转换成 MT5 position-level SL/TP 是后续可选优化，前提是能可靠关联
保护单和具体目标仓位。

这表示：初版**不使用 MT5 position 的原生 SL/TP 字段**，而是在 MT5 创建一个
明确的 Stop（止损）和一个明确的 Limit（止盈）挂单。它们与 ATAS 的两个订单
逐一对应，因此 ATAS 增仓后若将保护订单数量从 1 手更新为 2 手，桥接会按源端
撤旧建新的事实，在 MT5 也撤旧建新为对应的 2 手保护挂单。

保护订单数量有一条额外风险规则：不得超过已确认的 MT5 桥接仓位数量。ATAS
已增仓而 MT5 尚未完全成交时，先以已确认仓位数量创建/调整保护；MT5 后续
确认增仓后再补足保护数量。这避免保护单先成交并把尚不存在的目标仓位反向。

### 6.2 ATAS 撤单或移动

ATAS 的移动已实测为：

```text
old order: Done + canceled=true
new order: Active + new source_order_id
```

因此严格模式为：

1. 为旧 source instance 发送 MT5 delete；
2. 收到 `ORDER_DELETE`/快照确认后，删除该映射；
3. 再处理新 source instance 的 MT5 create。

这会忠实反映源端短暂的无单区间，且不需要推断新旧订单关系。未来可提供
显式的“使用 MT5 modify 优化”模式，但绝不作为默认。

### 6.3 ATAS 成交、MT5 未成交

当 ATAS `Done, canceled=false` 后出现新的 ATAS 净仓位：

1. 将该 MT5 pending ticket 标为“等待结算”，查询 MT5 当前订单/仓位；
2. 若 MT5 挂单已成交，直接进入仓位协调；
3. 若仍活跃，先撤销并确认撤销；
4. 再用受滑点/报价保护的市价命令，将 MT5 的**桥接仓位**收敛到 ATAS 映射
   净仓位；
5. 只有目标仓位确认后，才创建 ATAS 后续出现的 SL/TP。

这避免“旧挂单后来成交 + 补单市价成交”造成双倍仓位。

### 6.4 MT5 成交、ATAS 未成交

这是最危险的非对称成交，可能由基差、延迟或目标订单陈旧造成。

1. MT5 EA 上报 mapped ticket 成交；协调器立刻取消同一源实例的剩余 target
   orders，并暂停该品种新的风险增加命令；
2. 等待很短的 ATAS 结算窗口（建议 50ms）和一个 ATAS 快照；
3. 若 ATAS 仍未形成对应仓位，进入 `TARGET_AHEAD` 故障状态。

`TARGET_AHEAD` 默认使用 **atas_authoritative_auto_flatten**：ATAS 是权威源，
因此在最大滑点、最大数量、市场可交易和来源快照检查通过时，以 reduce-only
平掉多出来的桥接 MT5 仓位。平仓失败、超时或保护条件不满足时，退回
`quarantine`，不再尝试开新仓，等待人工协调。

## 7. ATAS 单向净仓位 → MT5 双向/净持仓

ATAS 对每个品种只有一个有符号净仓位 `S`。将其映射和标准化后，目标的规范
状态永远是单边：

```text
S > 0: desired_long = map(S), desired_short = 0
S < 0: desired_long = 0,      desired_short = map(abs(S))
S = 0: desired_long = 0,      desired_short = 0
```

### 7.1 MT5 hedging 账户

若桥接自身同时有 long 与 short，处理顺序固定为：

1. 平掉与期望方向相反的一侧；
2. 平掉同方向超额部分；
3. 仅在前两步确认后，补齐同方向不足部分。

例如 ATAS 从 +1 变为 -1：先平掉全部桥接 long，确认后才开 bridge short。
不触碰其他 magic number 或人工仓位；发现它们时默认 quarantine。

### 7.2 MT5 netting 账户

同样采用“先归零、再反向”的逻辑，即使平台可用一笔反向订单自动净额处理。
这是为了让日志、错误处理和 ATAS 单向模型保持可证明的一致性。

## 8. 故障处理与恢复

| 场景 | 自动动作 | 恢复条件 |
| --- | --- | --- |
| ATAS event_id 缺口/重连 | 停止新增风险；保留 journal | 新 ATAS snapshot |
| ATAS 回调重复 | 语义去重 | 无需动作 |
| MT5 请求拒绝 | 记录失败；不盲重试 | 新 target snapshot + 新策略决策 |
| MT5 请求超时 | 标记 UNKNOWN；暂停该品种 | `OnTradeTransaction` 或完整快照 |
| 目标订单被手工删除/修改 | 标记 external divergence | quarantine 或显式重建策略 |
| MT5 先成交、ATAS 未成交 | `TARGET_AHEAD` + 自动 reduce-only 平仓 | ATAS 追上、平仓确认或人工处理 |
| ATAS 先成交、MT5 未成交 | 撤销残余 pending 后按净仓位补齐 | MT5 仓位确认 |
| GC/XAUUSD 报价失效或基差越界 | 不新建/不改单；撤单仍允许 | 双端新鲜报价 |
| MT5 EA 断线/重启 | 停止命令；从 MT5 完整快照恢复 | snapshot 与 journal 对账 |

## 9. 已确认与仍待确认的策略选择

已确认：

1. **非对称成交**：MT5 先成交而 ATAS 未成交时，以 ATAS 为准，自动平掉
   多出来的桥接 MT5 仓位；失败才 quarantine。
2. **基差**：使用 4 小时近期加权的 `side_basis_4h`，对波动和突变向 ATAS 告警；
   初始阈值通过采样后校准。
3. **保护单初版**：以 MT5 显式 Stop/Limit pending orders 镜像 ATAS 的 SL/TP。

仍待确认：

1. **反向规则**：无论 hedging 还是 netting，是否始终“先平反向桥接仓，再开新
   方向”？
2. **人工干预**：检测到同 magic 的人工改单、未知订单或未知仓位时，是否默认
   quarantine？是否需要一个只允许撤单/减仓的紧急模式？

## 10. 验证顺序

1. 使用现有 Probe 收集更多实盘/模拟连接下的订单事件和双端报价；
2. 新增 MT5 EA observer，只记录 `OnTradeTransaction` 和完整快照；
3. 先实现 observe-only 的 desired-state diff 和故障注入测试；
4. 在 demo 上仅启用“远离市价的普通 pending order create/cancel”；
5. 再启用成交补偿、SL/TP、反向仓位和价格 guard；
6. 最后才考虑 `OrderSendAsync` 的 modify 优化。

## 参考

- [MQL5 OrderSendAsync](https://www.mql5.com/en/docs/trading/OrderSendAsync)
- [MQL5 OnTradeTransaction](https://www.mql5.com/en/docs/event_handlers/ontradetransaction)
- [MQL5 netting / hedging account properties](https://www.mql5.com/en/docs/constants/environment_state/accountinformation)

## 11. v0.2 决策补充：快速收敛与允许暂时对冲

本节覆盖第 7 节中“必须先归零、再反向”的严格规则。严格归零可保留为可选
`normalize_gross_exposure` 模式，但不作为默认策略。

### 11.1 权威方向与 ATAS 反向修复

ATAS 的交易管理接口可执行开单、改单、撤单、平仓以及设置止盈止损。因此，桥接在技术上可以
按 MT5 状态反向修正 ATAS。

不过默认仍是 **ATAS authoritative**：MT5 偏离 ATAS 时，优先在 MT5 取消、减仓或补齐，使其
回到 ATAS 的已确认净仓和活动订单；不自动在 ATAS 创建一笔用户并未下达的订单。这样不会把一次
MT5 的迟到成交或服务器故障变成 ATAS 端新的交易意图。

ATAS 反向操作保留为显式、可开关的 `atas_repair_mode`：仅对有完整映射和幂等
`repair_request_id` 的对象执行 `CancelOrder` 或 `ClosePositionAsync`，且必须先在 ATAS 图表和日志中
告警。v0.1 不启用该模式；它是人工确认后的应急修复通道，而不是日常同步路径。

### 11.2 默认持仓模型：净敞口等价，而非票据形态等价

协调器持续计算仅属于 bridge magic 的 MT5 净敞口：

```text
target_net = sum(bridge long volume) - sum(bridge short volume)
desired_net = mapped(ATAS signed net position)
delta = desired_net - target_net
```

目标是不迟延地把 `delta` 收敛到零；**不要求** MT5 hedging 账户的 long 与 short 永不并存。
所有 ATAS 的活动挂单（包括一次性 -2 的反向挂单）按原数量、方向和价格语义独立镜像。它触发后，
若 MT5 同时存在旧的 +1 与新的 -2，净敞口为 -1，已与 ATAS 的净仓一致；该暂时的对冲票据状态
是允许的，不能因为追求票据整齐而额外发出高风险订单。

对于立即执行的补偿 `delta`，仍采取“能平则先平”的轻量优先级：在 hedging 账户中先按 ticket
部分或全部平掉相反方向的 bridge 持仓，只有剩余 `delta` 才开新方向。这个优先级不阻塞 ATAS
挂单的正常镜像，也不等待“完全归零”才处理后续事件。以后 ATAS 再次变仓时重新计算净额；在无活动
来源挂单、无保护约束且用户启用时，低优先级 `hedge_cleanup` 才可整理等量的对冲腿。

### 11.3 快速修复状态机与时限

不同步不是一个可以长期维持的状态。每个 `XAUUSD` 协调器维护一条持久化 repair journal，按以下
优先级连续运行：

1. 立即取消已经失去 ATAS 来源的 MT5 风险增加型挂单；
2. 读取 ATAS canonical snapshot 与 MT5 EA 快照，重新计算 `delta`；
3. 若 ATAS 已成交而 MT5 未成交，先确认取消残余 target pending，再以受滑点保护的市价补齐净额；
4. 若 MT5 先成交而 ATAS 在极短结算窗口后仍未成交，立即用指定 bridge ticket 的 reduce-only
   平仓把 MT5 净额拉回 ATAS；
5. 仅在确认的快照显示 `delta = 0` 后结束 repair。

初始可配置预算为：ATAS 结算窗口 50ms、本地入队和规划不等待、MT5 请求收据 100ms、交易事务确认
500ms、超时后的快速 MT5 快照 100ms。它们是监控和触发协调的上限，不是固定防抖。网络或经纪商延迟
无法保证在 100ms 内成交，但桥接本身不额外等待。

唯一例外是“命令结果未知”：不能在未知的撤单/市价单上盲目重发，否则可能双重成交。此时立刻快照
对账；若快照仍不能判定，进入 `quarantine`、持续告警并禁止新增风险，直到事实状态可证明。取消和
降低已知 bridge 风险仍可继续执行。

### 11.4 TP/SL 的初版语义

初版仍可用显式 MT5 Stop/Limit 挂单镜像 ATAS 的 TP/SL；ATAS 把保护数量从 1 改为 2 时，MT5 也按
“撤旧建新”映射为 2。保护单成交后若在 hedging 账户形成了相反 ticket，而非直接消灭旧 ticket，
其结果只要 bridge **净额**已与 ATAS 一致即为有效，后续由上面的净额协调和可选 cleanup 处理。

但保护单的数量绝不能超过已确认的 MT5 bridge 净仓可保护数量；否则保护先触发时可能把尚未复制的
风险反向打开。原生 position SL/TP 是后续可选实现：在 hedging 账户中它能明确绑定某个 position
ticket，而显式反向挂单未必如此。两种方式都可支持，先以可审计的显式订单版本验证行为。

### 11.5 新增已确认决策

4. **反向/对冲**：默认采用 `net_equivalent`。优先减仓，但允许 bridge 的正反 ticket 暂时并存；
   不为维持单一方向而强制复杂的“先全平、后开仓”状态机。
5. **修复方向**：日常自动修复从 ATAS 指向 MT5；ATAS 的反向平仓能力只作为受控、显式启用的应急能力。

## 12. v0.3 决策补充：意图映射图，而非一对一订单映射

### 12.1 基础同步与未来执行质量修复分层

第一版只实行 **ATAS -> MT5**：ATAS 的已确认订单和净仓是事实，MT5 的异常成交按第 11 节快速
拉回。这使基础链路的正确性可验证。

未来允许增加独立的 `execution_quality_reconciliation` 策略。例如 MT5 挂单因 GC/XAUUSD 的细小价差
而先成交，且成交价、数量、时延均在预设容差内，该策略可判定它是“符合原始 ATAS 意图的可接受成交”。
随后按策略选择：

1. 取消 ATAS 原挂单并在 ATAS 以市价追入，使两端重新一致；或
2. 仅在桥接的虚拟 canonical state 中把该来源意图视作已完成，并明确标记为 virtual fill；或
3. 不满足条件时，仍回到第一版的 ATAS-authoritative 自动平 MT5。

这不是普通故障补偿，必须独立开关和审计。ATAS 已公开的交易接口提供开单、撤单、改单、平仓等操作；
是否能把一个既有 ATAS 挂单直接“标记为模拟成交”不应假设为可用。因此实现时需在“取消后市价追入”和
“桥接内部虚拟成交”之间明确选择，不能伪造 ATAS 的真实 broker 状态。

该策略的准入条件至少包括：唯一来源实例仍有效、两端报价新鲜、目标成交偏差不超过配置、成交量不超过
来源剩余量、ATAS 此后没有撤单/改价事件、没有并发 repair、以及一次性的 journal decision id。任一条件
失败即不接管，恢复普通 ATAS -> MT5 修复。

### 12.2 映射单位改为 `intent` 与 `target leg`

不能把映射表设计成 `ATAS order id -> 一个 MT5 ticket`。正确的数据模型是：

```text
source order instance
  -> bridge intent (来源、方向、数量、价格语义、版本)
  -> 1..N target legs (各自有 role、数量、MT5 ticket/position ticket、状态)
```

每个 `target leg` 记录不可变的 `leg_id`，并反向链接其 `source_order_id` 与 `intent_id`。建议的角色为：

- `entry_pending`：按来源创建的新开仓挂单；
- `reduce_pending`：在触发时优先减少一个已有 bridge position 的腿；
- `protective_tp` / `protective_sl`：由 ATAS 的当前保护语义产生的腿；
- `market_reconcile`：处理已确认净额差的补偿腿；
- `cleanup`：仅在低优先级净额已经正确后整理等量对冲。

这样一笔 ATAS 的 `-2` 订单，在已有 `+1` bridge 净仓时，可以合法编译成：一个与现有多仓关联的
`reduce_pending -1`，再加一个 `entry_pending -1`；也可先选择原样镜像为单一 `entry_pending -2`。
两种输出都属于同一个 source intent，只是不同的 `mapping policy`，不需要在状态机中增加特殊例外。

来源撤单、移动或成交时，协调器按 intent 找到全部 active legs，逐一取消、确认或重新计算；绝不靠价格、
时间间隔或 ticket 顺序猜测它们的关系。反过来，任一 MT5 成交、撤单或修改事件也能沿 `leg_id` 回溯到
唯一的来源意图。这正是之后支持“一个 ATAS 挂单对应 TP + 新挂单”等组合映射所必需的基础。

### 12.3 初版的映射策略

虽然持久化模型从第一天就支持 `1..N` legs，首个真实下单版本只启用最简单的
`direct_pending` policy：每个普通 ATAS 挂单生成一个 `entry_pending`，保护单单独生成各自的保护 leg。
`split_reduce_then_entry` 与执行质量修复先只在 observe-only / 模拟盘中输出计划和审计记录，收集数据后
再启用。这样不会因未来策略的复杂性牺牲当前低延迟和可证明性。
