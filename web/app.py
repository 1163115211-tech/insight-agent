"""FastAPI 门面：把 `insight.service` 的事件流暴露成 SSE，供单页前端消费。

启动（项目根目录）：
    python -m uvicorn web.app:app --port 8000

设计要点
- 真实服务是**延迟导入**的：每次请求才尝试 `from insight.service import stream, resume`。
  导入失败（Lead 还没交付 / 依赖缺失）就自动降级到 `web/mock_service.py`，
  页面顶部因此显示「Mock 模式」角标；真实服务一就绪，刷新即自动切换，无需改前端。
- 后台线程跑生成器，事件按序写入 `Run.events`；SSE 端点按 cursor 增量推送，
  因此「先发起、后连事件流」和「中断后 resume 再连」都不会丢事件或重放。
- 中断协议：`clarify/interrupt` 是终止事件；`meta/done` 是成功终止事件。
  两种情况都在最后补一个 SSE 命名事件 `end`，前端据此关闭 EventSource（防自动重连）。
"""
from __future__ import annotations

import asyncio
import json
import os
import queue
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from . import mock_service

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
INDEX_HTML = BASE_DIR / "index.html"
STATIC_DIR = BASE_DIR / "static"
ECHARTS_FILE = STATIC_DIR / "echarts.min.js"

MAX_RUNS = 60
SSE_TIMEOUT_SEC = 900


# --------------------------------------------------------------------------
# 事件源解析：真实服务优先，失败优雅降级到 mock
# --------------------------------------------------------------------------
_MODE: dict[str, Any] = {"mode": "mock", "reason": "尚未尝试连接 insight.service"}
_MODE_LOCK = threading.Lock()


def _mock_source(reason: str) -> dict[str, Any]:
    return {
        "mode": "mock",
        "reason": reason,
        "stream": mock_service.stream,
        "resume_stream": mock_service.resume,
    }


def resolve_service() -> dict[str, Any]:
    """解析本轮使用的事件源。

    `INSIGHT_MOCK=1` 时强制 mock；否则在函数内延迟导入真实服务，
    任何 ImportError / 属性错误都退化为 mock，并记录原因给 /api/health。
    """
    global _MODE

    forced = os.environ.get("INSIGHT_MOCK", "").strip().lower()
    if forced in ("1", "true", "yes", "on"):
        src = _mock_source("INSIGHT_MOCK=1：已强制使用内置演示轨迹")
        with _MODE_LOCK:
            _MODE = src
        return src

    try:
        if str(PROJECT_ROOT) not in sys.path:
            sys.path.insert(0, str(PROJECT_ROOT))
        # 延迟导入：真实服务还没就绪时不能拖垮整个 Web 进程
        from insight.service import resume as real_resume  # type: ignore
        from insight.service import stream as real_stream  # type: ignore
        import insight.service as _service  # type: ignore
    except Exception as exc:  # noqa: BLE001 - 任何导入期异常都降级
        reason = f"{type(exc).__name__}: {exc}"
        src = _mock_source(f"insight.service 不可用（{reason}），已降级为内置演示轨迹")
        with _MODE_LOCK:
            _MODE = src
        return src

    # 真实服务同时提供 stream_resume（生成器）与 resume（阻塞 RunResult）；
    # 优先用流式的 stream_resume，这样审批后的节点也能逐个点亮。
    real_resume_stream = getattr(_service, "stream_resume", None)
    src = {
        "mode": "real",
        "reason": "已连接 insight.service",
        "stream": real_stream,
        "resume_stream": real_resume_stream,
        "resume": real_resume,
    }
    with _MODE_LOCK:
        _MODE = src
    return src


# --------------------------------------------------------------------------
# 运行记录
# --------------------------------------------------------------------------
@dataclass
class Run:
    run_id: str
    thread_id: str
    question: str
    mode: str
    events: list[dict[str, Any]] = field(default_factory=list)
    #: 后台线程 -> SSE 的唤醒队列。事件本体按 cursor 从 events 里取，
    #: 队列只负责"有新事件了 / 结束了"，因此断线重连不会丢也不会重放。
    signal: "queue.Queue[object]" = field(default_factory=queue.Queue)
    finished: bool = False
    error: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None


_RUNS: dict[str, Run] = {}
_THREADS: dict[str, str] = {}  # thread_id -> 最近一次 run_id
_RUNS_LOCK = threading.Lock()


def _register(run: Run) -> None:
    with _RUNS_LOCK:
        _RUNS[run.run_id] = run
        _THREADS[run.thread_id] = run.run_id
        while len(_RUNS) > MAX_RUNS:
            oldest = next(iter(_RUNS))
            _RUNS.pop(oldest, None)


def _get_run(run_id: str) -> Run:
    with _RUNS_LOCK:
        run = _RUNS.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"未知的 run_id：{run_id}")
    return run


def _as_dict(event: Any) -> dict[str, Any]:
    """真实服务 yield 的是 StepEvent；mock 也是。这里统一成 dict。"""
    if isinstance(event, dict):
        data = dict(event)
    elif hasattr(event, "to_dict"):
        data = event.to_dict()
    else:  # 兜底：不该发生，但别让 SSE 断掉
        data = {"node": "meta", "status": "error", "data": {"detail": repr(event)}, "ts": time.time()}
    if "ts" not in data or not data["ts"]:
        data["ts"] = time.time()
    return data


def _is_terminal(event: dict[str, Any]) -> bool:
    status = event.get("status")
    node = event.get("node")
    if status == "interrupt":  # clarify 中断：本轮正常结束
        return True
    if node == "meta" and status in ("done", "error"):
        return True
    return False


def _resume_events(source: dict[str, Any], thread_id: str, value: str) -> Iterator[Any]:
    """审批/澄清后继续执行，兼容两种真实实现。

    - `stream_resume(thread_id, value)`：生成器，事件逐个产出（优先）
    - `resume(thread_id, value)`：阻塞返回 RunResult，用其 `events` 回放，
      若其中没有终止事件，用结果字段补一条 meta/done
    """
    fn = source.get("resume_stream") or source.get("resume")
    if fn is None:
        raise RuntimeError("当前服务既没有 stream_resume 也没有 resume，无法继续执行")
    out = fn(thread_id, value)

    if hasattr(out, "events") and hasattr(out, "to_dict"):
        result = out
        events = list(getattr(result, "events", None) or [])
        for ev in events:
            yield ev
        if not any(_is_terminal(_as_dict(ev)) for ev in events):
            yield {
                "node": "meta",
                "status": "done",
                "data": {
                    "latency_ms": int(getattr(result, "latency_ms", 0) or 0),
                    "attempts": int(getattr(result, "attempts", 0) or 0),
                    "usage": (result.usage.to_dict() if hasattr(result, "usage") else {}),
                },
                "ts": time.time(),
            }
        return

    for ev in out:  # 生成器实现
        yield ev


def _pump(run: Run, source: dict[str, Any], *, resume_value: str | None = None) -> None:
    """后台线程：同步生成器 -> run.events + 唤醒信号。"""
    try:
        if resume_value is None:
            it = source["stream"](run.question, thread_id=run.thread_id)
        else:
            it = _resume_events(source, run.thread_id, resume_value)
        for event in it:
            data = _as_dict(event)
            run.events.append(data)
            run.signal.put(data)
            if _is_terminal(data):
                break
    except Exception as exc:  # noqa: BLE001 - 任何异常都要让前端知道
        run.error = f"{type(exc).__name__}: {exc}"
    finally:
        run.finished_at = time.time()
        run.finished = True
        run.signal.put(None)


# --------------------------------------------------------------------------
# 请求模型
# --------------------------------------------------------------------------
class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    thread_id: str | None = Field(default=None, max_length=120)


class ResumeRequest(BaseModel):
    thread_id: str = Field(min_length=1, max_length=120)
    value: str = Field(default="继续", max_length=200)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------
app = FastAPI(title="问数 / Insight Agent", version="1.0.0", docs_url="/api/docs")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    if not INDEX_HTML.exists():
        raise HTTPException(status_code=500, detail="web/index.html 缺失")
    return FileResponse(INDEX_HTML, media_type="text/html", headers={"Cache-Control": "no-store"})


@app.get("/api/health")
def health() -> dict[str, Any]:
    source = resolve_service()
    return {
        "status": "ok",
        "mode": source["mode"],
        "reason": source["reason"],
        "echarts": ECHARTS_FILE.exists(),
        "runs": len(_RUNS),
        "time": time.time(),
    }


@app.post("/api/ask")
def ask(payload: AskRequest) -> dict[str, Any]:
    question = payload.question.strip()
    if not question:
        raise HTTPException(status_code=422, detail="问题不能为空")

    source = resolve_service()
    thread_id = (payload.thread_id or "").strip() or _new_id("th")
    run = Run(run_id=_new_id("run"), thread_id=thread_id, question=question, mode=source["mode"])
    _register(run)

    threading.Thread(target=_pump, args=(run, source), name=f"ask-{run.run_id}", daemon=True).start()
    return {"run_id": run.run_id, "thread_id": thread_id, "mode": source["mode"]}


@app.post("/api/resume")
def resume(payload: ResumeRequest) -> dict[str, Any]:
    with _RUNS_LOCK:
        previous_id = _THREADS.get(payload.thread_id)
        previous = _RUNS.get(previous_id) if previous_id else None
    if previous is None:
        raise HTTPException(status_code=404, detail="该会话没有可恢复的运行记录")

    source = resolve_service()
    run = Run(
        run_id=_new_id("run"),
        thread_id=payload.thread_id,
        question=previous.question,
        mode=source["mode"],
    )
    _register(run)

    threading.Thread(
        target=_pump,
        args=(run, source),
        kwargs={"resume_value": payload.value},
        name=f"resume-{run.run_id}",
        daemon=True,
    ).start()
    return {"run_id": run.run_id, "thread_id": payload.thread_id, "mode": source["mode"]}


@app.get("/api/runs/{run_id}")
def run_status(run_id: str) -> dict[str, Any]:
    run = _get_run(run_id)
    return {
        "run_id": run.run_id,
        "thread_id": run.thread_id,
        "mode": run.mode,
        "finished": run.finished,
        "error": run.error,
        "events": len(run.events),
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }


@app.get("/api/events/{run_id}")
async def events(run_id: str, request: Request, cursor: int = Query(default=0, ge=0)) -> EventSourceResponse:
    run = _get_run(run_id)

    async def gen() -> AsyncIterator[dict[str, Any]]:
        idx = cursor
        deadline = time.time() + SSE_TIMEOUT_SEC
        while True:
            # 1) 先补发已产生但尚未推送的事件（带 cursor 的重连不会丢也不会重放）
            while idx < len(run.events):
                event = run.events[idx]
                idx += 1
                yield {"data": json.dumps(event, ensure_ascii=False)}
                if _is_terminal(event):
                    yield {"event": "end", "data": json.dumps({"cursor": idx, "finished": True})}
                    return
            if run.finished:
                yield {
                    "event": "end",
                    "data": json.dumps(
                        {"cursor": idx, "finished": True, "error": run.error}, ensure_ascii=False
                    ),
                }
                return
            if time.time() > deadline:
                yield {
                    "event": "end",
                    "data": json.dumps({"cursor": idx, "finished": False, "error": "SSE 超时"}, ensure_ascii=False),
                }
                return
            if await request.is_disconnected():
                return
            # 2) 没有新事件时等后台线程的唤醒信号（阻塞 get 放到线程池，避免卡事件循环）
            try:
                await asyncio.to_thread(run.signal.get, True, 0.5)
            except asyncio.CancelledError:
                raise
            except queue.Empty:
                pass

    return EventSourceResponse(
        gen(),
        ping=15000,
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.exception_handler(HTTPException)
async def _http_error(request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
