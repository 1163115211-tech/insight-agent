"""对外数据契约。

CLI、Web、Eval 三个消费方都只依赖这里的类型，不碰 graph 内部实现。
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class StepEvent:
    """一次节点执行产生的事件，用于前端实时进度和 trace 落盘。"""

    node: str                       # intake | explore | plan | gen_sql | guard | execute | repair | visualize | summarize
    status: str                     # start | ok | error | retry | interrupt | done
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Usage:
    """token 与成本账本，跨节点累加。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_cny: float = 0.0
    calls: int = 0

    def add(self, prompt_tokens: int, completion_tokens: int, cost: float) -> "Usage":
        self.prompt_tokens += int(prompt_tokens or 0)
        self.completion_tokens += int(completion_tokens or 0)
        self.cost_cny += float(cost or 0)
        self.calls += 1
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
            "cost_cny": round(self.cost_cny, 6),
            "calls": self.calls,
        }


@dataclass
class RunResult:
    """一次完整运行的结果。中断时 interrupted=True，其余字段为已产生的部分结果。"""

    thread_id: str
    question: str
    answer: str = ""
    sql: str = ""
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    chart: dict[str, Any] | None = None
    attempts: int = 0
    interrupted: bool = False
    interrupt_payload: dict[str, Any] | None = None
    error: str | None = None
    errors: list[str] = field(default_factory=list)   # 历次失败原因，按时间顺序
    latency_ms: int = 0
    usage: Usage = field(default_factory=Usage)
    trace: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["usage"] = self.usage.to_dict()
        return d
