"""对外门面：run / stream / resume / get_state。

CLI、Web、评测三边都只调这里，不直接碰 graph。
所有异常都收敛成 RunResult.error，调用方不用写 try/except。
"""
from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from typing import Iterator

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from . import config
from .graph import build_graph
from .types import RunResult, StepEvent, Usage

_MISSING = object()

_graph = None
_saver_conn: sqlite3.Connection | None = None
_init_lock = threading.Lock()

# 每轮开始时重置的字段（跨轮要保留的只有 last_sql / last_answer）
_RESET_FIELDS = {
    "attempts": 0,
    "sql": "",
    "guard_error": "",
    "exec_error": "",
    "prev_errors": [],
    "columns": [],
    "rows": [],
    "chart": None,
    "answer": "",
    "plan": [],
    "tables": [],
    "ddl": "",
    "intent": "query",
}


def new_thread_id() -> str:
    return uuid.uuid4().hex[:12]


def get_graph():
    """全局单例。checkpointer 用 SQLite，服务和 CLI 共享同一个文件。"""
    global _graph, _saver_conn
    if _graph is None:
        with _init_lock:
            if _graph is None:
                config.DATA_DIR.mkdir(parents=True, exist_ok=True)
                _saver_conn = sqlite3.connect(
                    str(config.CHECKPOINT_PATH), check_same_thread=False
                )
                saver = SqliteSaver(_saver_conn)
                saver.setup()
                _graph = build_graph(checkpointer=saver)
    return _graph


def _initial_state(question: str) -> dict:
    state = dict(_RESET_FIELDS)
    state["question"] = question
    state["usage"] = {}
    state["events"] = []
    return state


def _to_events(node: str, update: dict) -> list[StepEvent]:
    events = []
    for raw in (update or {}).get("events", []) or []:
        events.append(
            StepEvent(
                node=raw.get("node", node),
                status=raw.get("status", "ok"),
                data=raw.get("data", {}) or {},
                ts=raw.get("ts", time.time()),
            )
        )
    return events


def _iter_run(
    thread_id: str,
    *,
    question: str | None = None,
    resume_value: object = _MISSING,
    timeout_s: float | None = None,
) -> Iterator[StepEvent]:
    """核心执行循环：把 LangGraph 的 updates 流转成对外的 StepEvent 流。

    - question 不为 None：新一轮提问
    - resume_value 提供：从上次 interrupt 处继续（human-in-the-loop）
    """
    graph = get_graph()
    cfg = {"configurable": {"thread_id": thread_id}, "recursion_limit": 40}
    started = time.perf_counter()

    if resume_value is not _MISSING:
        inp: object = Command(resume=resume_value)
        yield StepEvent(node="meta", status="start", data={"thread_id": thread_id, "resumed": True})
    else:
        inp = _initial_state(question or "")
        yield StepEvent(node="meta", status="start", data={"thread_id": thread_id, "question": question})

    interrupted = False
    try:
        for chunk in graph.stream(inp, cfg, stream_mode="updates"):
            if timeout_s and (time.perf_counter() - started) > timeout_s:
                yield StepEvent(node="meta", status="error", data={"error": "运行超时"})
                return
            if not isinstance(chunk, dict):
                continue
            if "__interrupt__" in chunk:
                intr = chunk["__interrupt__"][0]
                payload = intr.value if isinstance(intr.value, dict) else {"question": str(intr.value)}
                payload = {**payload, "thread_id": thread_id}
                interrupted = True
                yield StepEvent(node="clarify", status="interrupt", data=payload)
                continue
            for node, update in chunk.items():
                for e in _to_events(node, update if isinstance(update, dict) else {}):
                    yield e
    except Exception as e:  # noqa: BLE001 - 对外统一降级
        yield StepEvent(node="meta", status="error", data={"error": f"{type(e).__name__}: {e}"})
        return
    finally:
        pass

    snap = graph.get_state(cfg)
    values = snap.values or {}
    usage = Usage(
        prompt_tokens=int((values.get("usage") or {}).get("prompt_tokens", 0)),
        completion_tokens=int((values.get("usage") or {}).get("completion_tokens", 0)),
        cost_cny=float((values.get("usage") or {}).get("cost_cny", 0.0)),
        calls=int((values.get("usage") or {}).get("calls", 0)),
    )
    done = {
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "attempts": values.get("attempts", 0),
        "usage": usage.to_dict(),
        "interrupted": interrupted or bool(snap.next),
        "next": list(snap.next or ()),
    }
    yield StepEvent(node="meta", status="done", data=done)


def _collect(thread_id: str, events: list[StepEvent]) -> RunResult:
    """把事件流 + 最终 checkpoint 组装成 RunResult。"""
    graph = get_graph()
    snap = graph.get_state({"configurable": {"thread_id": thread_id}})
    values = snap.values or {}
    usage_raw = values.get("usage") or {}
    usage = Usage(
        prompt_tokens=int(usage_raw.get("prompt_tokens", 0)),
        completion_tokens=int(usage_raw.get("completion_tokens", 0)),
        cost_cny=float(usage_raw.get("cost_cny", 0.0)),
        calls=int(usage_raw.get("calls", 0)),
    )
    done = next((e for e in reversed(events) if e.node == "meta" and e.status == "done"), None)
    error_ev = next((e for e in reversed(events) if e.node == "meta" and e.status == "error"), None)
    intr = next((e for e in reversed(events) if e.status == "interrupt"), None)

    # 图跑完了却没数据，也要把最后一次失败原因透出来。
    # 否则调用方只看到 rows=[]，会误判成"业务上确实没数据"。
    error = error_ev.data.get("error") if error_ev else None
    if error is None and not intr:
        error = values.get("exec_error") or values.get("guard_error") or None

    prev_errors = list(values.get("prev_errors") or [])
    if error and error not in prev_errors:
        prev_errors.append(error)

    return RunResult(
        thread_id=thread_id,
        question=values.get("question", ""),
        answer=values.get("answer", "") or "",
        sql=(values.get("sql") or "").rstrip(),
        columns=values.get("columns") or [],
        rows=values.get("rows") or [],
        chart=values.get("chart"),
        attempts=int(values.get("attempts", 0)),
        interrupted=bool(intr) or bool(snap.next),
        interrupt_payload=intr.data if intr else None,
        error=error,
        errors=prev_errors,
        latency_ms=int(done.data.get("latency_ms", 0)) if done else 0,
        usage=usage,
        trace=[e.to_dict() for e in events],
        events=[e.to_dict() for e in events],
    )


def run(question: str, *, thread_id: str | None = None, timeout_s: float | None = None) -> RunResult:
    tid = thread_id or new_thread_id()
    events = list(_iter_run(tid, question=question, timeout_s=timeout_s))
    return _collect(tid, events)


def resume(thread_id: str, value: str) -> RunResult:
    events = list(_iter_run(thread_id, resume_value=value))
    return _collect(thread_id, events)


def stream(question: str, *, thread_id: str | None = None) -> Iterator[StepEvent]:
    """SSE 用。生成器正常结束（不抛异常），中断表现为 status='interrupt' 的事件。"""
    tid = thread_id or new_thread_id()
    yield from _iter_run(tid, question=question)


def stream_resume(thread_id: str, value: str) -> Iterator[StepEvent]:
    yield from _iter_run(thread_id, resume_value=value)


def shutdown() -> None:
    """释放 checkpoint 连接。测试和脚本收尾时调用，否则 Windows 上文件被占用删不掉。"""
    global _graph, _saver_conn
    if _saver_conn is not None:
        try:
            _saver_conn.close()
        except Exception:  # noqa: BLE001
            pass
    _graph = None
    _saver_conn = None


def get_state(thread_id: str) -> dict | None:
    graph = get_graph()
    snap = graph.get_state({"configurable": {"thread_id": thread_id}})
    if not snap.values:
        return None
    values = snap.values
    return {
        "thread_id": thread_id,
        "question": values.get("question"),
        "sql": values.get("sql"),
        "columns": values.get("columns"),
        "rows": values.get("rows"),
        "chart": values.get("chart"),
        "answer": values.get("answer"),
        "attempts": values.get("attempts", 0),
        "next": list(snap.next or ()),
        "interrupted": bool(snap.next),
        "usage": values.get("usage") or {},
        "last_sql": values.get("last_sql"),
        "last_answer": values.get("last_answer"),
        "tables": values.get("tables"),
    }
