# Web 演示界面（问数 / Insight Agent）

FastAPI + SSE + 单页前端。把 `insight.service` 的事件流实时渲染成「执行轨迹 → SQL → 结果表 →
ECharts 图表 → 结论」，并在 `clarify/interrupt` 时弹出审批卡片，选择后沿用同一个 `thread_id` 继续执行。

视觉规范见 [DESIGN.md](DESIGN.md)；事件协议见 `docs/CONTRACT.md` 第 2、3 节。

---

## 1. 启动

项目根目录（`insight-agent/`）下执行：

```powershell
# 本机环境
$py = "C:\Users\11631\anaconda3\envs\langchain1.2\python.exe"
$env:PYTHONIOENCODING="utf-8"; $env:PYTHONUTF8="1"

# 真实服务（不设 INSIGHT_MOCK）
& $py -m uvicorn web.app:app --host 127.0.0.1 --port 8000

# Mock 模式（内置演示轨迹，不调用 LLM）
$env:INSIGHT_MOCK="1"
& $py -m uvicorn web.app:app --host 127.0.0.1 --port 8000
```

然后浏览器打开 `http://127.0.0.1:8000/`。

> **端口冲突**：本机 8000 端口被系统进程 `Manager.exe`（PID 7944）占用，绑定时会报
> `WinError 10013`。联调时改用 `--port 8001` 验证；换机器或释放 8000 后直接用上面的命令即可，
> 命令本身不需要改。

### 模式切换

| 条件 | 行为 |
|---|---|
| 不设 `INSIGHT_MOCK`，且 `from insight.service import stream, resume` 成功 | 真实服务；顶部角标「真实服务」（绿） |
| 不设 `INSIGHT_MOCK`，但导入失败 / 依赖缺失 | 自动降级 mock；顶部角标「Mock 模式」（琥珀），hover 可看降级原因 |
| `INSIGHT_MOCK=1` | 强制 mock |

导入是**延迟**的（每次请求才尝试），真实服务就绪后刷新页面即自动切换，前端无需改动。
`GET /api/health` 会返回当前 `mode` 与 `reason`。

---

## 2. 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 单页 `index.html`（`Cache-Control: no-store`） |
| GET | `/api/health` | `{status, mode, reason, echarts, runs, time}` |
| POST | `/api/ask` | 入 `{question, thread_id?}`，返回 `{run_id, thread_id, mode}`；后台线程启动事件流 |
| GET | `/api/events/{run_id}?cursor=0` | SSE，逐条推送 `data: <StepEvent.to_dict()>` |
| POST | `/api/resume` | 入 `{thread_id, value}`，返回 `{run_id, thread_id, mode}`；继续执行并推送事件 |
| GET | `/api/runs/{run_id}` | 运行状态快照（`finished` / `error` / `events` 计数），前端断流兜底用 |

### SSE 约定

- 每条事件一行：`data: {"node": "...", "status": "...", "data": {...}, "ts": 1.7e9}`，与
  `StepEvent.to_dict()` 完全一致，前端不臆造字段。
- 终止：`clarify/interrupt`（等待人工确认）或 `meta/done`（成功）/`meta/error`（失败）。
- 终止后服务端再发一个命名事件 `event: end`，`data: {"cursor": n, "finished": true}`；
  前端据此关闭 `EventSource`，避免浏览器自动重连。
- `cursor` = 已消费事件数。中断后 resume 会产生新的 `run_id`，从 `cursor=0` 重新连接；
  同一 `run` 的重连则带上 cursor，不丢事件也不重放。
- 服务端 `ping=15s` 保活；单条 SSE 最长 900s。

### 事件源适配（真实服务字段差异）

真实服务与 CONTRACT 完全兼容，另有三处前端做了兼容处理，**没有改服务**：

1. **`status="start"` 进度事件**：真实服务会给部分节点先发 `start` 再发终态事件
   （如 `intake/start` → `intake/ok`）。前端把同一节点的 `start` 卡原地升级为终态卡，
   所以时间线既实时点亮又不会出现重复节点。
2. **`meta/error`**：运行时异常以 `{node:"meta", status:"error", data:{error:"..."}}` 返回，
   前端渲染为红色提示条（不是 SSE 断流）。
3. **ECharts formatter 被序列化成字符串**：真实 `visualize.ok` 的 option 里，
   `tooltip.valueFormatter`、`axisLabel.formatter`、`series.label.formatter` 可能是
   `"function(v){...}"` 字符串（JSON 序列化丢函数）。前端在 `setOption` 前做**受控还原**：
   只接受 `function(...){...}` 形状，且函数体命中 DOM/网络/全局对象关键字时直接丢弃，
   避免执行 LLM 生成的任意代码。还原失败时图表仍正常渲染，只是少了单位/格式化。
   图表标题、轴、图例由服务端 option 决定，前端只提供深色主题兜底配色。

`execute.ok` 的 `rows` 是二维数组、`columns` 是列名数组；`meta.done` 的 `usage` 是嵌套对象
（`{prompt_tokens, completion_tokens, total_tokens, cost_cny, calls}`，即 `Usage.to_dict()`），
均已联调验证。

---

## 3. 演示路径（推荐）

1. 打开页面 → 顶部确认模式角标；点击示例胶囊「各渠道 GMV 与退货率」。
2. 看左侧轨迹逐节点点亮（会话 → 受理 → 看表 → 计划 → 写 SQL → 校验 → 执行 …），
   点击任意节点可展开详情（耗时、DDL、checks、原始事件 JSON）。
3. 中间依次出现：结论 → 高亮 SQL → 结果表（可横向滚动）→ 图表；底部常驻
   Token / 成本 / 耗时 / 尝试 指标条。
4. 在输入框继续追问即可多轮（沿用同一 `thread_id`，轨迹中出现「第 2 轮 · 追问」分隔）。

能出图、且真实服务稳定通过的提问：

| 提问 | 产出 |
|---|---|
| `2026年9月各渠道的GMV，按从高到低排序` | 6 行渠道表 + 柱状图 |
| `帮我看看销售情况怎么样` | 多指标表 + 图表（服务端会补 formatter，可验证还原逻辑） |
| `对比一下各渠道表现` | 触发 `clarify/interrupt` 审批卡片，选一个口径后继续出图（推荐里 4 个选项都可选） |

Mock 模式点任意示例都能走完整链路：固定包含一次 SQL 报错 → 自动修复 → 审批中断 → 放行 → 出图。

---

## 4. 前端结构

```
web/
├── app.py                 FastAPI 门面：SSE 泵、run 存储、真实服务延迟导入 + mock 降级
├── mock_service.py        按事件协议造的完整假轨迹（stream / resume / run）
├── index.html             单页结构（中文界面）
├── static/
│   ├── app.css            设计系统 + 布局 + 组件状态
│   ├── app.js             事件渲染、轨迹时间线、表格、图表、审批、多轮
│   └── echarts.min.js     ECharts 5（本地 1.03 MB）
└── DESIGN.md              方向契约 + 设计 token + 组件状态
```

`echarts.min.js` 已下载到本地（`static/echarts.min.js`，约 1.03 MB），离线可用；
若文件缺失，页面图表区会显示「图表库未加载」的说明而不是空白。
**离线环境需要替换为本地文件**：把 `echarts@5` 的 `dist/echarts.min.js` 放到
`web/static/` 即可（当前实现默认本地优先，没有 CDN 依赖）。

---

## 5. 自测

```powershell
# 健康检查
Invoke-RestMethod http://127.0.0.1:8000/api/health

# 发起一轮
$r = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/ask `
     -ContentType "application/json; charset=utf-8" `
     -Body ([Text.Encoding]::UTF8.GetBytes('{"question":"2026年9月各渠道的GMV，按从高到低排序"}'))
$r

# 读事件流（逐条打印）
curl.exe -N "http://127.0.0.1:8000/api/events/$($r.run_id)"
```

Mock 模式快速冒烟（不起服务）：

```powershell
& $py -m web.mock_service     # 打印一轮完整事件的 JSON
```

---

## 6. 已知限制

- **8000 端口**被本机系统进程占用，见上文；这是环境问题，不是代码问题。
- run 记录只存在进程内存里（最多保留 60 条），重启进程后旧 `thread_id` 无法 resume。
- 没有鉴权与多用户隔离，定位是本地演示；不要直接暴露到公网。
- 审批卡片的选项来自 `clarify.data.options`；若服务端没给 options，前端兜底为
  「继续执行 / 取消」两个按钮（`value` 原样回传）。
- ECharts 只渲染服务端给的 bar/line/pie/scatter option（`data.chart`），前端不改数据、
  不重排系列；formatter 字符串还原是尽力而为。
- 中断后 `meta/done` 会被后端事件流一并产出，但 Web 端在 `clarify/interrupt` 处即结束本轮
  （避免把「暂停」显示成「完成」），resume 后以 `stream_resume` 的事件流接着渲染。
- 无头验证使用的 Edge 里预装了一个 Sider 扩展，控制台会有它的 CORS 报错；
  与页面无关，正常浏览器不会出现。
