# 吃透这个项目：7 天学习手册

> 目标不是"能背下来"，而是**面试官随便挑一个点，你能从设计动机讲到踩过的坑**。
> 用法：每天 2 小时，按第 6 节的计划走；每读完一块，做第 5 节对应的自测题。

---

## 0. 先定义"吃透"的四个等级

| 等级 | 标准 | 怎么验证 |
|---|---|---|
| **L1 能讲** | 白纸画出架构图，说清 10 个节点各自干什么、为什么这么排 | 合上代码，对着白板讲 3 分钟 |
| **L2 能改** | 加节点 / 改 prompt / 加评测题，不破坏现有功能 | 做完第 4 节的 Lab 1~3 |
| **L3 能查** | 出问题时能定位到"数据层 / 图 / 模型 / 沙箱 / 前端"哪一层 | 跑第 4 节的 Lab 2，在 10 分钟内找到根因 |
| **L4 能辩** | 回答"为什么不用 X"，并给出反例和代价 | 第 5 节硬核题全过 |

**面试通常只考到 L2，L3/L4 决定薪资档位。**

---

## 1. 学习地图：六层，每层一个文件群

| 层 | 关键文件 | 你要能回答的问题 | 建议时间 |
|---|---|---|---|
| ① 数据层 | `insight/db/schema.md`、`seed.py` | 6 张表怎么关联？GMV/退货率/客单价的口径是什么？为什么数据要能复现？ | 2h |
| ② 模型层 | `insight/prompts.py` | 为什么每个节点一个 prompt？为什么口径不写死在 prompt 里？为什么要限制输出格式？ | 2h |
| ③ **控制层** ★ | `insight/nodes.py`、`graph.py`、`service.py` | State 里每个字段谁写的？条件边怎么判？checkpoint 存了什么？ | 6h |
| ④ 安全层 | `insight/sandbox.py` | 三层防护分别挡住什么？正则为什么不够？超时怎么实现的？ | 2h |
| ⑤ 服务层 | `web/app.py`、`service.stream()` | 事件流怎么从图里到浏览器？中断时前端怎么知道？ | 2h |
| ⑥ 评测层 | `insight/eval/*`、`docs/eval-report.md` | 执行准确率怎么算的？A/B 为什么公平？失败怎么归因？ | 3h |

> ⚠️ 别按文件夹顺序读。按第 2 节的**依赖顺序**读，每读完一个文件再读下一个。

---

## 2. 代码怎么读：11 步，每步都有"读完自问"

### Step 1 · `insight/types.py`（10 分钟）
对外契约：`ScreenEvent / RunResult / Usage`。CLI、Web、Eval 三方只依赖它。
**自问**：为什么要把"事件"和"结果"分成两个类型？（提示：一个流式、一个终态）

### Step 2 · `insight/config.py`（10 分钟）
所有可调参数集中在这里，包括价目表。
**自问**：为什么价目表不写死在代码里？`REFERENCE_DATE` 为什么要固定而不是 `now()`？

### Step 3 · `insight/sandbox.py`（40 分钟）★
先读 `check_sql`（静态校验），再读 `execute`（只读执行），最后读 `_mask_literals`。
**自问**：
- 为什么不能只用正则匹配 `delete`？（提示：字符串字面量里也有这个词）
- `mode=ro` 和 `PRAGMA query_only` 为什么要两层？
- `set_progress_handler` 是怎么实现超时的？

### Step 4 · `insight/tools.py`（15 分钟）
`build_catalog`（给模型的紧凑目录）和 `build_ddl`（只给选中表）。
**自问**：为什么不让模型一开始就看到全部 6 张表的完整 DDL？

### Step 5 · `insight/prompts.py`（40 分钟）★
只精读三个：`SQL_SYSTEM`、`SUMMARY_SYSTEM`、`intake_user`。
**自问**：
- SQL prompt 里第 9、10 条（结果集纪律、禁止额外过滤）是怎么来的？（答案是：评测失败逼出来的）
- 为什么 `business_rules_block()` 只截取 schema.md 的「业务口径」一节？

### Step 6 · `insight/nodes.py`（90 分钟）★★
按这个顺序读：`intake → gen_sql → guard → execute → repair → visualize → summarize`。
每个节点只看三件事：**输入什么字段、输出什么字段、吐什么事件**。
**自问**：
- `gen_sql` 重试时，`prev_sql` 和 `prev_error` 从哪来？
- `repair` 节点为什么只"记账"不"重写"？（提示：重写发生在 `gen_sql`）
- `route_after_execute` 的循环边界在哪？

### Step 7 · `insight/graph.py`（20 分钟）
数一遍：10 个节点、4 个条件边。
**自问**：把 `clarify` 从图里删掉会发生什么？（画一遍就知道）

### Step 8 · `insight/service.py`（40 分钟）★
只看 `_iter_run` 和 `_collect` 两个函数。
**自问**：
- `stream_mode="updates"` 拿到的 chunk 长什么样？
- 中断是怎么被识别出来的？（提示：`__interrupt__`）
- 为什么 `run()` 也是用 `stream()` 实现的？

### Step 9 · `insight/cli.py`（可选，10 分钟）
看 `ask` 命令怎么把事件渲染成人能读的进度。

### Step 10 · `web/app.py`（30 分钟）
只看 SSE 泵：`queue.Queue` + 后台线程 + `cursor` 重连。
**自问**：如果用户刷新页面，之前的事件会不会丢？

### Step 11 · `insight/eval/metrics.py`（40 分钟）★
只看 `compare_result_sets`：为什么要用二分图匹配？
**自问**：`SELECT channel, gmv` 和 `SELECT gmv, channel` 为什么算对？

---

## 3. 十个核心概念速查

| 概念 | 一句话 | 在本项目哪里 |
|---|---|---|
| **State** | 图的共享内存，每个节点读它、返回局部更新 | `nodes.AgentState` |
| **Reducer** | 同名字段怎么合并（覆盖 or 累加） | `merge_usage` / `operator.add` |
| **Conditional Edge** | 按状态选下一条边 | `route_after_execute` |
| **Checkpoint** | 每一步的完整状态快照，落 SQLite | `SqliteSaver` + `thread_id` |
| **Interrupt** | 图停下来等人，不是报错 | `clarify` 节点 |
| **Command(resume)** | 从断点继续，节点会重跑一遍 | `service.resume` |
| **SSE** | 服务器单向推事件给浏览器 | `web/app.py` |
| **只读沙箱** | 模型只能"写"SQL，执行权在代码手里 | `sandbox.py` |
| **执行准确率** | 比结果集，不比 SQL 字符串 | `metrics.py` |
| **上下文工程** | 该给模型看什么、不该给什么 | `explore` 节点、口径截取 |

---

## 4. 五个动手实验（这是真正"吃透"的地方）

### Lab 0 · 热身：不看项目，手写一个 40 行的 agent loop（30 分钟）
新建 `scratch/loop.py`，用 OpenAI 兼容接口 + 一个计算器工具，实现：
```
while 还有 tool_calls: 执行工具 → 结果回填 → 再请求
```
**验收**：能说清"为什么需要 tool_call_id 把结果对回去"。
> 这一步做不做，决定你是"用过框架"还是"懂 agent"。

### Lab 1 · 给图加一个节点（1 小时）★必做
需求：在 `execute` 之后、`visualize` 之前，加一个 `data_quality` 节点：
- 如果返回 0 行 → 事件标记 `empty`
- 如果行数 > 500 → 警告"结果过多，建议加聚合"
**改哪些文件**：`nodes.py`（加函数 + 路由）、`graph.py`（加节点加边）、`web` 不用动。
**验收**：跑 `python -m insight ask "..."` 能看到 `data_quality` 事件；`pytest` 全绿。

### Lab 2 · 故意把 prompt 改坏（1 小时）★必做
把 `SQL_SYSTEM` 里的第 9、10 条删掉，跑这 3 个问题：
1. 平均订单金额是多少
2. 各渠道订单数（含已取消）
3. 哪个品类平均标价最高

**验收**：你能指出"多列""多加过滤"分别是哪条纪律在管，并解释为什么评测会判错。
> 这个实验让你亲手复现"prompt 是怎么被评测逼出来的"。

### Lab 3 · 加 5 道评测题（2 小时）
往 `questions.jsonl` 加 5 条，要求：单表 2 条、多表 2 条、窗口 1 条。
**必须**：每条 gold_sql 实跑非空，且和题面语义一致。
**验收**：`python -m insight.eval.run_eval --config both --limit 57` 能跑完并出新分数。
> 加完你会发现：出题比答题难。这就是为什么好的评测集贵。

### Lab 4 · 加一个"成本熔断"（2 小时，选做）
在 `service._iter_run` 里：单次运行 token 超过阈值就中断并返回 `error`。
**验收**：把阈值设成 100 tokens 能触发，设成 100000 不触发。

---

## 5. 30 道自测题

### 基础（12 道，L1）
1. 画一遍架构图，标出 4 个条件边。
2. 10 个节点里，哪几个调了 LLM？
3. `intake` 判定 reject 后走哪条路径？
4. SQL 生成和 SQL 执行之间为什么必须隔一个节点？
5. 表名白名单解决什么问题？
6. 没有 LIMIT 的查询会发生什么？
7. 澄清中断后，用户回答存在哪？
8. 结果集比较时，列顺序敏感吗？
9. 一次问答大约花多少 token、多少钱、多少秒？
10. 哪张表存 checkpoint？
11. `explore` 节点省的是什么？
12. 前端怎么知道"图跑到第几步了"？

### 进阶（12 道，L2）
13. 给 `AgentState` 加一个字段要注意什么？（reducer）
14. `merge_usage` 如果写成直接覆盖会怎样？
15. 为什么 `repair` 只记账不重写？
16. 循环最多几轮？边界写在哪两个地方？
17. 追问「那只看抖音呢」为什么不会被判成新问题？
18. 如果模型连续 3 次写同一个错，会发生什么？
19. `visualize` 返回的结构不合法时怎么处理？
20. 为什么 `run()` 内部也用 `stream()`？
21. SSE 断线重连怎么做到不丢事件？
22. 新增一个评测题需要改几个文件？
23. 为什么 baseline 的 prompt 要和 graph 对齐？
24. 你改了 SQL prompt，怎么知道有没有变差？

### 硬核（6 道，L3/L4）
25. 把 `clarify` 节点的 `interrupt()` 换成"让模型自己猜"，会发生什么？
26. checkpointer 从 SQLite 换 PostgreSQL 要改哪些代码？
27. 如果模型写出了能过 guard 但语义错误的 SQL（比如 join 扇出），你怎么防？
28. 上线给 100 个业务方用，这个架构哪里会先崩？
29. 怎么证明"提升 25pp"不是评测集偏差？
30. 让你把这个项目砍到 1/3 代码量，砍哪里、保哪里？

> 答案要点见 `docs/interview-guide.md`，但**先自己答，再看答案**。

---

## 6. 七天计划

| 天 | 内容 | 产出 |
|---|---|---|
| D1 | Step 1~4（契约/配置/沙箱/工具）+ Lab 0 | 手写 loop 跑通 |
| D2 | Step 5~6（prompt + 节点） | 白纸画出 10 节点数据流 |
| D3 | Step 7~8（图 + service）+ Lab 1 | 新节点跑通 |
| D4 | Step 11（评测）+ Lab 2 | 写出 3 条失败归因 |
| D5 | Lab 3 + 读 eval-report | 加 5 题并跑分 |
| D6 | Step 10（web）+ 通读 interview-guide | 能讲清 SSE 和中断 |
| D7 | 30 道自测题 + 模拟面试 | 录音复盘 |

---

## 7. 模拟面试怎么练

1. **讲给自己听**：用手机录音，讲 3 分钟项目介绍，回放找"然后…然后…"
2. **让我当面试官**：直接跟我说「模拟面试，你来问」，我按真实节奏追问，不给标准答案
3. **反向讲**：假装教一个完全不懂的人，讲不清的地方就是没懂
4. **改代码讲**：面试官说"如果我要加个功能"，你要能当场说改哪几个文件

---

## 8. 最后一句

这个项目的代码量是 6700 行，但**核心逻辑不到 800 行**：
`nodes.py` 的 10 个函数 + `sandbox.check_sql` + `service._iter_run` + `metrics.compare_result_sets`。

把这四个地方读到能默写，剩下的都是工程包装。
