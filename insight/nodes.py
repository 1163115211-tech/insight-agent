"""图的节点实现。

每个节点都是 `State -> 局部 State` 的纯函数（除了调 LLM），
返回的 `events` 会被 service 层转成 SSE 推给前端，`usage` 会被累加成成本账。

设计取舍：
- **不让模型直接执行 SQL**：generate 和 execute 之间强制插一层 guard，
  模型只负责"写"，执行由确定性的代码负责。这是 agent 安全的基本盘。
- **错误回灌而不是抛异常**：执行失败把报错原文喂回去重写，最多 MAX_RETRIES 次。
- **每个节点都可单独测试**：不依赖图也能 `node({"question": ...})` 调。
"""
from __future__ import annotations

import json
import operator
import re
import time
from functools import lru_cache
from typing import Annotated, Any, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.types import interrupt

from . import config, prompts, sandbox, tools


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
def merge_usage(left: dict, right: dict) -> dict:
    """usage 账本跨节点累加。LangGraph 的 reducer 必须是纯粹的二元函数。"""
    left = left or {}
    right = right or {}
    out = dict(left)
    for k in ("prompt_tokens", "completion_tokens", "cost_cny", "calls"):
        out[k] = out.get(k, 0) + right.get(k, 0)
    return out


class AgentState(TypedDict, total=False):
    # 输入
    question: str
    intent: str                     # query | clarify | reject

    # 分诊 / 澄清
    clarify_question: str
    clarify_options: list[str]
    clarify_answer: str

    # 上下文构建
    catalog: str
    tables: list[str]
    ddl: str
    plan: list[str]

    # SQL 生命周期
    sql: str
    guard_error: str
    exec_error: str
    attempts: int
    prev_errors: list[str]

    # 结果
    columns: list[str]
    rows: list[list]
    chart: dict | None
    answer: str

    # 跨轮上下文
    last_question: str
    last_sql: str
    last_answer: str

    # 账本
    usage: Annotated[dict, merge_usage]
    events: Annotated[list, operator.add]


def ev(node: str, status: str, **data: Any) -> dict:
    return {"node": node, "status": status, "data": data, "ts": time.time()}


# --------------------------------------------------------------------------- #
# LLM 调用封装：统一记账
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=1)
def _llm():
    return config.get_llm()


def _invoke(system: str, user: str, *, node: str) -> tuple[str, dict]:
    resp = _llm().invoke([SystemMessage(content=system), HumanMessage(content=user)])
    meta = getattr(resp, "usage_metadata", None) or {}
    prompt_tokens = int(meta.get("input_tokens", 0) or 0)
    completion_tokens = int(meta.get("output_tokens", 0) or 0)
    usage = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cost_cny": config.estimate_cost(prompt_tokens, completion_tokens),
        "calls": 1,
    }
    content = resp.content if isinstance(resp.content, str) else str(resp.content)
    return content, usage


def _history_block(state: AgentState) -> str:
    """拼上一轮对话摘要，供 intake 判断"这是不是追问"。"""
    parts: list[str] = []
    if state.get("last_question"):
        parts.append(f"上一轮问题：{state['last_question']}")
    if state.get("last_answer"):
        parts.append(f"上一轮结论：{str(state['last_answer'])[:400]}")
    if state.get("last_sql"):
        parts.append(f"上一轮 SQL：{str(state['last_sql'])[:400]}")
    return "\n".join(parts)


def parse_json(text: str) -> dict:
    """从模型输出里抠 JSON。容忍 ```json 包裹、前后废话。失败返回 {}。"""
    if not text:
        return {}
    cleaned = re.sub(r"```(?:json)?\s*", "", text).replace("```", "").strip()
    try:
        obj = json.loads(cleaned)
        return obj if isinstance(obj, dict) else {}
    except json.JSONDecodeError:
        pass
    start = min([i for i in (cleaned.find("{"), cleaned.find("[")) if i >= 0], default=-1)
    if start < 0:
        return {}
    depth, in_str, esc = 0, False, False
    for i in range(start, len(cleaned)):
        ch = cleaned[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(cleaned[start : i + 1])
                    return obj if isinstance(obj, dict) else {}
                except json.JSONDecodeError:
                    return {}
    return {}


# --------------------------------------------------------------------------- #
# 1. 意图分诊
# --------------------------------------------------------------------------- #
def intake(state: AgentState) -> dict:
    question = (state.get("question") or "").strip()
    events = [ev("intake", "start", question=question)]

    history = _history_block(state)
    text, usage = _invoke(
        prompts.INTAKE_SYSTEM, prompts.intake_user(question, history), node="intake"
    )
    obj = parse_json(text)
    intent = obj.get("intent", "query")
    if intent not in ("query", "clarify", "reject"):
        intent = "query"

    if intent == "reject":
        reason = obj.get("reason") or "这个请求超出只读数据分析的范围"
        events.append(ev("intake", "reject", intent=intent, reason=reason))
        return {
            "intent": intent,
            "answer": f"无法执行：{reason}。我只做只读的数据查询分析，不改数据、不建表、不导出全表。",
            "events": events,
            "usage": usage,
        }

    if intent == "clarify":
        cq = obj.get("clarify_question") or "请补充一下口径或范围？"
        opts = obj.get("options") or []
        events.append(ev("intake", "ok", intent=intent, reason=obj.get("reason", "")))
        return {
            "intent": intent,
            "clarify_question": cq,
            "clarify_options": opts[:4],
            "events": events,
            "usage": usage,
        }

    events.append(ev("intake", "ok", intent=intent, reason=obj.get("reason", "")))
    return {"intent": intent, "events": events, "usage": usage}


# --------------------------------------------------------------------------- #
# 2. 澄清（会真正挂起图，等人工回答）
# --------------------------------------------------------------------------- #
def clarify(state: AgentState) -> dict:
    """调用 interrupt() 挂起。恢复时 Node 会从头重跑，interrupt 直接返回答案。"""
    payload = {
        "question": state.get("clarify_question") or "请补充一下口径？",
        "options": state.get("clarify_options") or [],
    }
    answer = interrupt(payload)

    if isinstance(answer, dict):
        answer_text = str(answer.get("answer") or answer.get("value") or "")
    else:
        answer_text = str(answer or "")

    merged = state.get("question", "")
    if answer_text.strip():
        merged = f"{merged}\n（用户补充：{answer_text.strip()}）"

    return {
        "question": merged,
        "intent": "query",
        "clarify_answer": answer_text,
        "events": [ev("clarify", "ok", answer=answer_text)],
    }


# --------------------------------------------------------------------------- #
# 3. 选表
# --------------------------------------------------------------------------- #
def explore(state: AgentState) -> dict:
    catalog = tools.build_catalog()
    all_tables = [t["name"] for t in tools.list_tables()]
    started = time.perf_counter()

    if not config.USE_EXPLORE or len(all_tables) <= 3:
        tables = all_tables
        reason = "表数量少，直接全量"
        usage: dict = {}
    else:
        text, usage = _invoke(
            prompts.EXPLORE_SYSTEM, prompts.explore_user(catalog, state["question"]), node="explore"
        )
        obj = parse_json(text)
        picked = [t for t in (obj.get("tables") or []) if t in all_tables]
        tables = picked or all_tables
        reason = obj.get("reason", "")

    ddl = tools.build_ddl(tables)
    elapsed = int((time.perf_counter() - started) * 1000)
    return {
        "catalog": catalog,
        "tables": tables,
        "ddl": ddl,
        "events": [
            ev("explore", "ok", tables=tables, ddl_chars=len(ddl), reason=reason, elapsed_ms=elapsed)
        ],
        "usage": usage,
    }


# --------------------------------------------------------------------------- #
# 4. 计划
# --------------------------------------------------------------------------- #
def plan(state: AgentState) -> dict:
    text, usage = _invoke(
        prompts.PLAN_SYSTEM,
        prompts.plan_user(state.get("ddl", ""), state["question"]),
        node="plan",
    )
    obj = parse_json(text)
    steps = [str(s) for s in (obj.get("steps") or [])][:5]
    return {"plan": steps, "events": [ev("plan", "ok", steps=steps)], "usage": usage}


# --------------------------------------------------------------------------- #
# 5. 生成 / 重写 SQL
# --------------------------------------------------------------------------- #
def gen_sql(state: AgentState) -> dict:
    attempts = int(state.get("attempts", 0))
    prev_sql = state.get("sql", "") if attempts > 0 else ""
    prev_error = ""
    if attempts > 0:
        prev_error = state.get("exec_error") or state.get("guard_error") or ""

    text, usage = _invoke(
        prompts.SQL_SYSTEM,
        prompts.sql_user(
            state.get("ddl", ""),
            state["question"],
            state.get("plan") or [],
            prev_sql=prev_sql,
            prev_error=prev_error,
            history_sql=state.get("last_sql", ""),
            history_answer=state.get("last_answer", ""),
        ),
        node="gen_sql",
    )
    sql = sandbox.extract_sql(text)

    if not sql:
        return {
            "sql": "",
            "guard_error": "模型没有输出可解析的 SQL",
            "events": [ev("gen_sql", "error", attempt=attempts + 1, error="SQL 解析失败")],
            "usage": usage,
        }

    return {
        "sql": sql,
        "attempts": attempts,   # 显式写回，保证状态里一定有这个字段
        "guard_error": "",      # 新一轮生成，清掉上一轮校验错误
        "events": [ev("gen_sql", "ok", sql=sql, attempt=attempts + 1)],
        "usage": usage,
    }


# --------------------------------------------------------------------------- #
# 6. 静态校验（安全闸门，不调 LLM）
# --------------------------------------------------------------------------- #
def guard(state: AgentState) -> dict:
    sql = state.get("sql", "")
    allowed = tools.table_names()
    result = sandbox.check_sql(sql, allowed, max_rows=config.SQL_MAX_ROWS)

    if not result.ok:
        return {
            "guard_error": result.reason,
            "events": [ev("guard", "error", reason=result.reason, sql=sql)],
        }
    return {
        "sql": result.sql,
        "guard_error": "",
        "events": [ev("guard", "ok", checks=result.checks)],
    }


# --------------------------------------------------------------------------- #
# 7. 只读执行
# --------------------------------------------------------------------------- #
def execute(state: AgentState) -> dict:
    result = sandbox.execute(
        state.get("sql", ""),
        config.DB_PATH,
        max_rows=config.SQL_MAX_ROWS,
        timeout_ms=config.SQL_TIMEOUT_MS,
    )
    if not result.ok:
        return {
            "exec_error": result.error,
            "events": [ev("execute", "error", error=result.error, elapsed_ms=result.elapsed_ms)],
        }
    return {
        "exec_error": "",
        "columns": result.columns,
        "rows": result.rows,
        "events": [
            ev(
                "execute",
                "ok",
                columns=result.columns,
                rows=result.rows[:200],     # 推给前端的事件截断，避免 SSE 行过大
                row_count=result.row_count,
                truncated=result.truncated,
                elapsed_ms=result.elapsed_ms,
            )
        ],
    }


# --------------------------------------------------------------------------- #
# 8. 自修复（只记账，真正重写发生在 gen_sql）
# --------------------------------------------------------------------------- #
def repair(state: AgentState) -> dict:
    attempts = int(state.get("attempts", 0)) + 1
    error = state.get("exec_error") or state.get("guard_error") or "未知错误"
    prev_errors = list(state.get("prev_errors") or []) + [error]
    return {
        "attempts": attempts,
        "prev_errors": prev_errors,
        "events": [ev("repair", "retry", attempt=attempts, error=error, strategy="错误回灌重写")],
    }


# --------------------------------------------------------------------------- #
# 9. 出图
# --------------------------------------------------------------------------- #
_ALLOWED_CHART = {"bar", "line", "pie", "scatter"}


def _valid_chart(chart: dict) -> bool:
    if not isinstance(chart, dict) or not chart:
        return False
    series = chart.get("series")
    if not isinstance(series, list) or not series:
        return False
    return all(str(s.get("type", "")).lower() in _ALLOWED_CHART for s in series if isinstance(s, dict))


def visualize(state: AgentState) -> dict:
    rows = state.get("rows") or []
    columns = state.get("columns") or []

    if not rows:
        return {"chart": None, "events": [ev("visualize", "ok", chart=None, skip="结果为空")]}

    text, usage = _invoke(
        prompts.VIZ_SYSTEM,
        prompts.viz_user(state["question"], columns, rows),
        node="visualize",
    )
    obj = parse_json(text)

    if obj.get("skip"):
        return {
            "chart": None,
            "events": [ev("visualize", "ok", chart=None, skip=obj.get("reason", "数据不适合画图"))],
            "usage": usage,
        }
    if not _valid_chart(obj):
        return {
            "chart": None,
            "events": [ev("visualize", "ok", chart=None, skip="模型输出的图表结构不合法，已丢弃")],
            "usage": usage,
        }
    return {"chart": obj, "events": [ev("visualize", "ok", chart=obj)], "usage": usage}


# --------------------------------------------------------------------------- #
# 10. 结论
# --------------------------------------------------------------------------- #
def summarize(state: AgentState) -> dict:
    rows = state.get("rows") or []
    error_hint = state.get("exec_error") or ""
    text, usage = _invoke(
        prompts.SUMMARY_SYSTEM,
        prompts.summary_user(
            state["question"], state.get("columns") or [], rows, state.get("plan") or [], error_hint
        ),
        node="summarize",
    )
    answer = text.strip()
    if error_hint and not rows:
        answer = (
            f"这个问题没查出来：自动重试 {state.get('attempts', 0)} 次后仍然失败。"
            f"最后一次错误是「{error_hint}」。可以换个问法（比如明确时间范围或换个指标口径）再试。"
        )
    return {
        "answer": answer,
        "last_question": state.get("question", ""),
        "last_sql": state.get("sql", ""),
        "last_answer": answer,
        "events": [ev("summarize", "ok", answer=answer)],
        "usage": usage,
    }


def reject_answer(state: AgentState) -> dict:
    answer = state.get("answer") or "这个请求不在我的能力范围内。"
    return {
        "answer": answer,
        "last_question": state.get("question", ""),
        "last_answer": answer,
        "events": [ev("summarize", "ok", answer=answer, rejected=True)],
    }


# --------------------------------------------------------------------------- #
# 路由（全部是纯函数，好测）
# --------------------------------------------------------------------------- #
def route_intake(state: AgentState) -> str:
    intent = state.get("intent", "query")
    return {"clarify": "clarify", "reject": "reject_answer"}.get(intent, "explore")


def route_after_guard(state: AgentState) -> str:
    if not state.get("guard_error"):
        return "execute"
    return "repair" if int(state.get("attempts", 0)) < config.MAX_RETRIES else "summarize"


def route_after_execute(state: AgentState) -> str:
    if not state.get("exec_error"):
        return "visualize"
    return "repair" if int(state.get("attempts", 0)) < config.MAX_RETRIES else "summarize"


def route_after_repair(state: AgentState) -> str:
    return "gen_sql"
