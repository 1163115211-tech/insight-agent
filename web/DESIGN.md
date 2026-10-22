# Design system — 问数 / Insight Agent 演示界面

> 这份文档由构建方在交付时记录，覆盖方向契约、token、组件状态与验收记录。
> 项目根目录没有 `PRODUCT.md` / `DESIGN.md`：本任务的文件归属只到 `web/`，产品上下文按
> 任务 brief 推断并内联在这里（brief 已固定「专业数据分析工具、深色、等宽显示 SQL、动效轻」，
> 属于方向被 brief 钉死的场景，因此没有跑 concept roll；Operate 界面按规范走 code-led）。

---

## 1. Direction contract（方向契约）

**THESIS** — 这是一个「能看见推理过程的问数工作台」：左侧时间脊记录每个节点，右侧一张连续的证据页
按生成顺序堆出结论、SQL、结果、图表。它拒绝聊天泡泡 + 转圈指示器这种数据分析 Agent 的默认形态。

**OWN-WORLD** — 石墨底（#08090b）+ 两级中性面（#0d0f13 / #111419），只靠 1px 发丝线分层，不用阴影堆叠；
唯一信号色是 azure（#4c8dff），只出现在运行中/选中/焦点/可交互处；失败/重试/待确认/完成各有一个语义色，
且只承载状态。等宽字体只给 SQL、数字与 timecode，不做「技术感」装饰。

**STORY** — 用户输入一句业务问题 → 看机器按站点点亮 → 读到它真正跑过的 SQL → 因为证据就在眼前而
信任结论；需要确认时它会停下来，选择后从同一会话继续。

**FIRST VIEWPORT** — 顶栏（品牌 + 模式角标 + 会话）→ 提问框（内联运行按钮 + 示例胶囊）→ 双栏：
356px 执行轨迹（时间脊 + 节点卡）与证据页（结论 → SQL → 结果表 → 图表 → 指标条）。主操作在提问框内。

**FORM** — code-led，Operate 模式；方向由 brief 钉死（Linear / Vercel / Retool 的克制感），
`seed: none (unavailable)`（未跑 concept roll，brief 优先于 roll）。

**FINISH** — 未评审、未文档化即未完成；本构建以截图评审、live 页面检测器、本 DESIGN.md 收尾。

### 签名动作

时间脊（timeline spine）：一条 1px 竖线串起所有节点，节点以 7px 圆点标记状态；运行中的行末端有一条
「正在执行 · 已收到 N 个节点」的实时行，配一个 100ms 刷新的耗时计数。它是这个界面唯一的结构性记忆点，
并且只表达状态，不做装饰性循环动画。

---

## 2. Token

### 颜色

| Token | 值 | 用途 |
|---|---|---|
| `--ground` | `#08090b` | 页面底色 |
| `--panel` | `#0d0f13` | 面板（轨迹 / 证据页） |
| `--panel-2` | `#111419` | 输入框、表头、对话框 |
| `--panel-3` | `#151a20` | 保留层（tooltip 等） |
| `--line` | `#1b2027` | 发丝分割线 |
| `--line-2` | `#252c35` | 交互控件边框 |
| `--ink` | `#e8ebef` | 主文本（对比度 ≈15:1 on panel） |
| `--ink-2` | `#a0a9b5` | 次级文本（≈7.4:1） |
| `--ink-3` | `#7c8794` | 三级/元信息（≈4.9:1，满足 AA 正文） |
| `--accent` | `#4c8dff` | 运行中、选中、焦点、可交互 |
| `--ok` | `#3fb950` | 完成 |
| `--err` | `#f2555a` | 失败 / 拒绝 |
| `--warn` | `#dba33e` | 重试 / 降级提示 / Mock 角标 |
| `--wait` | `#b79cff` | 待确认（human-in-the-loop） |
| `--bone` | `#f2f4f7` | 主按钮底（Vercel 式白按钮），文字 `#0a0c0e` |

语义色只用于状态点、状态标签、提示条与图表状态，不用于装饰。

### 字体

- UI：`"Segoe UI Variable Text", "Segoe UI", "PingFang SC", "Microsoft YaHei", system-ui, sans-serif`
  （Operate 界面用系统无衬线；刻意没有引入 Inter 等被滥用的展示字体，也不需要联网字体）
- 代码/数字：`"Cascadia Mono", "Cascadia Code", "SFMono-Regular", Consolas, ...`
- 数字统一 `font-variant-numeric: tabular-nums`。

字号梯度（固定 rem/px，不做流体缩放）：11px（元信息下限）/ 11.5 / 12 / 12.5 / 13 / 14.5（输入）/
16.5（结论正文）/ 19（空态标题）。功能文本不小于 11px。

### 空间与形状

- 圆角：面板 10、输入/对话框 10–12、代码/表格 8、按钮 7、标签 4、胶囊 999。
- 间距：面板内 16 / 20 / 24；区块之间 20–22；组内 8–12。
- 阴影：仅对话框与 toast 使用带偏移的柔和阴影（`0 14px 38px -14px rgba(0,0,0,.72)`）。
- 深度靠中性面 + 1px 发丝线，不靠彩色光晕。

### 动效

- 时长 150–250ms，指数缓出 `cubic-bezier(.22,1,.36,1)`。
- 只表达状态：新节点卡片 220ms 淡入上移、展开箭头 160ms、骨架 1.7s 呼吸、toast 180ms。
- 无编排式入场、无循环脉冲点；运行中的「活」由事件到达与计时数字承担。
- `prefers-reduced-motion: reduce` 下全部降为 0.001ms。

---

## 3. 组件状态

每个交互组件都实现了 default / hover / focus-visible / active / disabled：

| 组件 | 状态 |
|---|---|
| 主按钮「运行」 | 白底；hover 纯白；disabled（空输入 / 运行中）；运行中改文案「运行中…」 |
| 次级按钮 / 胶囊 / 节点卡 | 透明底 + 发丝边框；hover 提亮；active 下移 0.5px |
| 输入框 | `:focus-within` 描边 azure + 3px 柔光环；空态 placeholder 用 `--ink-3` |
| 节点卡 | ok / error / retry / interrupt / running / done / start 七种状态点与标签；可展开详情 |
| 表格 | sticky 表头、行 hover、数字列右对齐 + 等宽、NULL 显示「—」、横向滚动、`tabindex=0` 可聚焦滚动 |
| 图表 | 暗色主题兜底、`aria-label`、ResizeObserver 自适应、库缺失/渲染失败的 fallback 文案 |
| 审批对话框 | 原生 `<dialog>` + `showModal`（真正需要打断与焦点保护）；Esc / 点遮罩关闭后顶部出现「继续处理」 |
| 提示条 | error（红）/ warn（琥珀）两种，可手动关闭，重试成功后自动消失 |
| Toast | error 5.2s / ok 3.2s 自动消失，`role=status aria-live=polite` |

空态与加载：首屏没有结果时，证据页给出「提交后会出现什么」的教学式空态；运行中显示
SQL 形状的骨架（非转圈）；轨迹栏空态列出全部站点，兼作图例。

浏览器表面也取自调色板：`::selection`、`caret-color`、细滚动条、`focus-visible` 环、等宽数字。

---

## 4. 响应式

- ≥1181px：356px 轨迹 + 自适应证据页；页面锁定 100dvh，两栏各自内部滚动。
- ≤1180px：轨迹收窄到 312px。
- ≤980px：单列，轨迹在上（max-height 400px 内部滚动），证据页在下，页面整体滚动。
- ≤640px：顶栏精简（隐藏英文副标题/会话标签）、示例胶囊换行、图表降到 240px、
  内边距收窄；移动端无横向溢出（实测 `scrollWidth - innerWidth = 0`）。

---

## 5. 验收记录（本轮实跑）

- 真实服务（`insight.service`）：
  - `2026年9月各渠道的GMV，按从高到低排序`：7.6s / 10 条事件 / 6 行 × 2 列表格 / 柱状图 / 指标条正确。
  - `帮我看看销售情况怎么样`：13.9s / 14 条事件，含一次真实 `execute.error → repair → gen_sql →
    execute.ok` 自修复；9 列表格可横向滚动；服务端 formatter 字符串还原后柱顶显示「87.11 万」。
  - `对比一下各渠道表现`（首轮）：触发 `clarify/interrupt`，4 个选项渲染正常；选择后
    `POST /api/resume` 继续，6 行 × 6 列结果与图表正常；轨迹里「待确认」卡原地变成已选择。
  - 多轮追问：同 `thread_id` 完成第二轮，轨迹出现「第 2 轮 · 追问」分隔。
- Mock 模式（`INSIGHT_MOCK=1`）：SQL 报错 → 修复 → 审批 → 放行 → 出图完整链路通过。
- 无头 Edge（CDP 驱动真实交互）截图评审：桌面 1440×900、移动 390×844；无 JS 报错、
  无横向溢出、对话框与控件状态正常。
- Impeccable 检测器对运行中的页面扫描结果：`[]`（无 anti-pattern）。对静态 CSS 的启发式报告里
  「panel-head / panel 全出血表头」两条是刻意设计（表头与滚动区贴边、内容自带内边距），
  「11.5px 元信息」高于该工具自己给出的 11px 功能文本下限，均记录为已知误报。
