# 模块契约（三个并行开发方之间的接口约定）

> 这是内部开发文档。改动接口前先改这里，再改代码。

## 1. 分层

```
insight/            核心：图、节点、沙箱、工具、prompt
  ├── service.py    对外门面（run / stream / resume / get_state）
  ├── graph.py      构建并编译 StateGraph
  ├── nodes.py      各节点实现
  ├── sandbox.py    只读 SQL 沙箱 + 静态校验
  ├── tools.py      给 LLM 看的工具描述（list_tables / describe_table / sample_rows）
  └── eval/         评测（数据集、指标、跑分）
web/                FastAPI + SSE + 单页前端
docs/               文档与评测报告
data/               sqlite 库 + checkpoint（不入库）
```

**文件归属（并行开发，不要越界改）**

| 归属 | 目录 |
|---|---|
| Lead | `insight/*.py`、`docs/`（除 eval 报告）、`README.md` |
| eval-owner | `insight/db/`、`insight/eval/`、`data/`、`docs/eval-report.md` |
| web-owner | `web/` |

## 2. 对外 API（`insight.service`）

```python
from insight.service import run, stream, resume, get_state
from insight.types import RunResult, StepEvent, Usage

def run(question: str, *, thread_id: str | None = None) -> RunResult: ...
def resume(thread_id: str, value: str) -> RunResult: ...
def get_state(thread_id: str) -> dict | None: ...
def stream(question: str, *, thread_id: str | None = None):
    """生成器：依次 yield StepEvent。遇到中断时 yield 一个 node='clarify',
    status='interrupt', data=<interrupt payload> 的事件后正常结束（不抛异常）。"""
```

约定：
- `run()` 阻塞到结束或中断；中断时 `result.interrupted=True`，`result.interrupt_payload` 是该节点 `interrupt()` 传入的对象。
- `thread_id` 不传则自动生成并写回 `result.thread_id`。
- 任何异常都转成 `result.error` 字符串，不向调用方抛（CLI/Web 好处理）。

## 3. 事件协议（SSE）

每行 `data: <json>`，json 形状固定：

```json
{"node": "execute", "status": "ok", "data": {"columns": [...], "rows": [...], "row_count": 12, "elapsed_ms": 43}, "ts": 1729500000.0}
```

节点与状态：

| node | status | data 关键字段 |
|---|---|---|
| `meta` | `start` | `{"thread_id": "..."}` |
| `intake` | `ok` / `reject` | `{"reason": "...", "blocked": "写操作"}` |
| `clarify` | `interrupt` | `{"question": "...", "options": ["..."], "thread_id": "..."}` |
| `explore` | `ok` | `{"tables": ["orders","order_items"], "ddl_chars": 1234}` |
| `plan` | `ok` | `{"steps": ["按渠道聚合 GMV", "..."]}` |
| `gen_sql` | `ok` / `error` | `{"sql": "SELECT ...", "attempt": 1}` |
| `guard` | `ok` / `error` | `{"checks": [...], "reason": "..."}` |
| `execute` | `ok` / `error` | `{"columns": [...], "rows": [[...]], "row_count": 12, "elapsed_ms": 43}` 或 `{"error": "..."}` |
| `repair` | `retry` | `{"attempt": 2, "error": "...", "strategy": "..."}` |
| `visualize` | `ok` | `{"chart": {...ECharts option...}}` |
| `summarize` | `ok` | `{"answer": "..."}` |
| `meta` | `done` | `{"latency_ms": 8123, "attempts": 1, "usage": {...}}` |

**图表约定**：`chart` 是 ECharts `option` 对象，前端直接 `setOption(chart)`。
只允许 `bar` / `line` / `pie` / `scatter` 四种，且 `dataset` 内联在 option 里，不走外部数据源。

## 4. 数据库

`data/ecommerce.db`，SQLite，6 张表：`customers`、`products`、`orders`、`order_items`、`returns`、`campaigns`。
字段与语义见 `insight/db/schema.md`（eval-owner 产出）。
数据时间范围：2025-07-01 ~ 2026-09-30（"上个月"= 2026-09）。

## 5. 评测接口

`insight/eval/run_eval.py` 通过 CLI 暴露：

```
python -m insight.eval.run_eval --config graph     # 完整图
python -m insight.eval.run_eval --config baseline  # 单次生成（无探查/无修复）
python -m insight.eval.run_eval --config both --limit 10
```

输出：`docs/eval/{config}.jsonl`（逐题结果）+ `docs/eval/summary.json`（汇总）+ 控制台表格。
