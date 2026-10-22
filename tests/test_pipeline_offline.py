"""整图离线测试：用假 LLM 驱动，不花一分钱、不依赖网络。

覆盖三条最关键的路径：
1. 正常路径：写 SQL → 执行 → 出图 → 结论
2. 自修复路径：第一次 SQL 报错 → 错误回灌 → 第二次成功（attempts == 1）
3. 澄清路径：intake 判定 clarify → 图挂起在 interrupt → resume 后继续跑完

    python -m pytest tests/ -q
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from insight import config, nodes  # noqa: E402
from insight.graph import build_graph  # noqa: E402

GOOD_SQL = """```sql
SELECT channel AS "渠道", ROUND(SUM(total_amount), 2) AS "GMV"
FROM orders WHERE status = '已完成' GROUP BY channel ORDER BY "GMV" DESC
```"""

BAD_SQL = """```sql
SELECT nonexistent_column FROM orders GROUP BY channel
```"""


class _Resp:
    def __init__(self, content: str):
        self.content = content
        self.usage_metadata = {"input_tokens": 120, "output_tokens": 30}


class FakeLLM:
    """按 system prompt 里的身份关键词分派回复，模拟六个节点各自的模型。"""

    def __init__(self, *, first_sql_bad: bool = False, clarify: bool = False):
        self.first_sql_bad = first_sql_bad
        self.clarify = clarify
        self.sql_calls = 0
        self.seen: list[str] = []

    def invoke(self, messages):
        system = str(messages[0].content)
        self.seen.append(system[:12])

        if "意图分诊器" in system:
            if self.clarify:
                return _Resp('{"intent":"clarify","reason":"指标不明确",'
                             '"clarify_question":"活跃用户按什么定义？","options":["按登录","按下单"]}')
            return _Resp('{"intent":"query","reason":"口径明确"}')
        if "schema 检索器" in system:
            return _Resp('{"tables":["orders"],"reason":"只需要订单表"}')
        if "资深数据分析师" in system:
            return _Resp('{"steps":["按渠道聚合 GMV","排除取消单"]}')
        if "SQLite 数据分析师" in system:
            self.sql_calls += 1
            if self.first_sql_bad and self.sql_calls == 1:
                return _Resp(BAD_SQL)
            return _Resp(GOOD_SQL)
        if "数据可视化" in system:
            return _Resp('{"title":{"text":"各渠道GMV"},"xAxis":{"type":"category"},'
                         '"yAxis":{"type":"value"},"series":[{"type":"bar","data":[1,2,3]}]}')
        if "面向业务方" in system:
            return _Resp("2026 年 9 月 App 渠道 GMV 最高。")
        return _Resp("{}")


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE orders(order_id INTEGER PRIMARY KEY, order_date TEXT,
            channel TEXT, status TEXT, total_amount REAL);
        INSERT INTO orders VALUES
            (1,'2026-09-01','App','已完成',100.0),
            (2,'2026-09-02','抖音','已完成',50.0),
            (3,'2026-09-03','App','已取消',999.0);
        """
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(config, "DB_PATH", path)
    return path


def _run(fake: FakeLLM, question: str, *, resume_value=None, thread_id="t1"):
    monkeypatch_target = nodes._llm
    nodes._llm = lambda: fake  # type: ignore[assignment]
    try:
        graph = build_graph(checkpointer=InMemorySaver())
        cfg = {"configurable": {"thread_id": thread_id}, "recursion_limit": 40}
        state = {"question": question, "usage": {}, "events": []}
        first = graph.invoke(state, cfg)
        if resume_value is not None:
            first = graph.invoke(Command(resume=resume_value), cfg)
        return first
    finally:
        nodes._llm = monkeypatch_target  # type: ignore[assignment]


def test_happy_path(db):
    fake = FakeLLM()
    out = _run(fake, "2026年9月各渠道GMV")
    assert out["answer"]
    assert out["rows"] == [["App", 100.0], ["抖音", 50.0]]
    assert out["attempts"] == 0
    assert out["chart"] is not None
    # 测试库只有 1 张表，explore 走"表少直接全量"的捷径，所以是 5 次调用：
    # intake / plan / gen_sql / visualize / summarize
    assert out["usage"]["calls"] == 5
    assert out["usage"]["cost_cny"] > 0


def test_self_repair(db):
    fake = FakeLLM(first_sql_bad=True)
    out = _run(fake, "2026年9月各渠道GMV")
    assert out["attempts"] == 1                 # 失败一次后修好
    assert out["rows"] and out["exec_error"] == ""
    assert any(e["node"] == "repair" for e in out["events"])
    assert fake.sql_calls == 2


def test_self_repair_gives_error_feedback(db):
    """更严格：确认第二次调用 prompt 里确实带上了报错原文。"""
    captured: list[str] = []

    class RecordingFake(FakeLLM):
        def invoke(self, messages):
            captured.append(str(messages[1].content))
            return super().invoke(messages)

    out = _run(RecordingFake(first_sql_bad=True), "2026年9月各渠道GMV")
    assert out["attempts"] == 1
    assert any("no such column" in c for c in captured), "报错没有回灌给模型"


def test_clarify_interrupt_and_resume(db):
    fake = FakeLLM(clarify=True)
    original = nodes._llm
    nodes._llm = lambda: fake  # type: ignore[assignment]
    try:
        graph = build_graph(checkpointer=InMemorySaver())
        cfg = {"configurable": {"thread_id": "clarify-1"}, "recursion_limit": 40}
        out = graph.invoke(
            {"question": "看看活跃用户", "usage": {}, "events": []}, cfg
        )
        assert "__interrupt__" in out, "应当挂起等人工输入"
        payload = out["__interrupt__"][0].value
        assert "活跃用户" in payload["question"]

        out2 = graph.invoke(Command(resume="按下单算"), cfg)
        assert out2["answer"]
        assert "按下单算" in out2["question"]
    finally:
        nodes._llm = original  # type: ignore[assignment]
