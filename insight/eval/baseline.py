"""A/B 对照组的 B 面：单次生成基线（single-shot baseline）。

刻意"笨"：
1. 一次性把**全部** 6 张表的 DDL 塞进 prompt（不探查、不选表、不看样例数据）；
2. LLM 只生成一条 SQL，**不重试、不修复**；
3. 直接执行，失败就记失败。

这样它复现的是"没有 Agent 编排时，一个普通 Text-to-SQL prompt"的水平，
和 graph 配置（探索→计划→生成→校验→执行→修复）对比，差值才是 Agent 带来的增益。
"""
from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from insight.config import DB_PATH, estimate_cost, get_llm
from insight.db.schema import full_ddl
from insight.eval.metrics import strip_sql_fence

# 只读打开：即使模型生成了 UPDATE/DELETE，也写不进库；执行层再加一次 SELECT 白名单。
_RO_SQL_PREFIX = re.compile(r"^\s*(SELECT|WITH)\b", re.I)
MAX_RESULT_ROWS = 1001  # 比 gold 的 50 行上限宽松；超出说明 SQL 写错，截断防撑爆内存

SYSTEM_PROMPT = (
    "你是一名资深电商数据分析师，精通 SQLite。"
    "根据给定的数据库结构和业务口径，把用户的业务问题翻译成一条可在 SQLite 上执行的 SQL。"
    "只输出 SQL，不要解释思路，不要输出多条语句。"
)


@lru_cache(maxsize=1)
def _schema_text() -> str:
    return full_ddl()


def build_prompt(question: str) -> str:
    """把「当前日期 + 全库 DDL + 业务口径 + 问题」拼成一条 human message。

    DDL 和口径必须一次性给全：baseline 的机会只有一次，不能像 Agent 那样回头查。
    当前日期要写死，否则模型会把"上月"理解成训练时的某个月。
    """
    return f"""今天日期：2026-10-22；数据覆盖 2025-07-01 ~ 2026-09-30，因此"上月"指 2026-09。

【数据库结构（SQLite DDL）】
{_schema_text()}

【业务口径】
- GMV = 已完成订单的成交金额合计（orders.total_amount 求和），不含已取消订单，不扣退货。
- 订单数默认只统计状态为"已完成"的订单；客单价 = GMV / 已完成订单数（订单级指标，分组时分母仍是订单数）。
- 退货率（订单口径）= 有退货记录的已完成订单数 / 已完成订单数；退货单不区分处理状态，含"处理中"。
- 同比 = 本期 / 去年同期 - 1；环比 = 本期 / 上一期 - 1。
- 新客 = 首笔已完成订单发生在统计期的客户；老客 = 之前已有已完成订单。
- 退货记录挂在订单明细行上：returns.item_id -> order_items.item_id。

【问题】
{question}

【要求】
1. 只输出一条 SELECT 或 WITH 查询，不要输出解释。
2. 不要使用 date('now')，所有时间条件都写明确的日期或 LIKE 'YYYY-MM%'。
3. 需要排名/TopN 时给出合适的 LIMIT；结果列数不要超过 8 列。
"""


@dataclass
class BaselineResult:
    """baseline 一次问答的全部产物，供 run_eval 组装 EvalRecord。"""

    sql: str = ""
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    exec_ok: bool = False
    error: str | None = None
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_cny: float = 0.0
    raw_response: str = ""
    truncated: bool = False


def _extract_usage(response: Any) -> tuple[int, int]:
    """兼容 langchain 不同版本/不同 provider 的 usage 字段位置。"""
    usage = getattr(response, "usage_metadata", None) or {}
    prompt = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
    completion = usage.get("output_tokens") or usage.get("completion_tokens") or 0
    if not prompt and not completion:
        token_usage = (getattr(response, "response_metadata", None) or {}).get("token_usage", {}) or {}
        prompt = token_usage.get("prompt_tokens", 0)
        completion = token_usage.get("completion_tokens", 0)
    return int(prompt or 0), int(completion or 0)


def _execute_readonly(db_path: Path, sql: str) -> tuple[list[str], list[list[Any]], bool]:
    """在只读连接上执行 SQL，返回 (列名, 行, 是否被截断)。"""
    if not _RO_SQL_PREFIX.match(sql):
        raise ValueError("只允许执行 SELECT / WITH 查询")
    conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    try:
        cursor = conn.execute(sql)
        columns = [d[0] for d in cursor.description or []]
        fetched = cursor.fetchmany(MAX_RESULT_ROWS)
        truncated = len(fetched) >= MAX_RESULT_ROWS
        return columns, [list(row) for row in fetched], truncated
    finally:
        conn.close()


def run_question(question: str, *, llm: Any = None, db_path: Path = DB_PATH) -> BaselineResult:
    """单次生成 + 单次执行，不重试。

    LLM 调用失败、SQL 语法错、结果超限都只记录在返回值里，不抛异常——
    评测要的是"这一题对照组的真实表现"，不是让一个偶发错误打断整轮跑分。
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    start = time.perf_counter()
    result = BaselineResult()
    try:
        model = llm or get_llm()
        response = model.invoke([SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=build_prompt(question))])
        raw = response.content if isinstance(response.content, str) else str(response.content)
        result.raw_response = raw
        result.prompt_tokens, result.completion_tokens = _extract_usage(response)
        result.cost_cny = estimate_cost(result.prompt_tokens, result.completion_tokens)
    except Exception as exc:  # LLM 网络/鉴权/解析异常都算这一题失败
        result.error = f"LLM 调用失败：{exc}"
        result.latency_ms = int((time.perf_counter() - start) * 1000)
        return result

    sql = strip_sql_fence(result.raw_response)
    result.sql = sql
    if not sql:
        result.error = "模型回复中未解析出 SQL"
        result.latency_ms = int((time.perf_counter() - start) * 1000)
        return result

    try:
        columns, rows, truncated = _execute_readonly(db_path, sql)
        result.columns, result.rows, result.truncated = columns, rows, truncated
        result.exec_ok = True
    except Exception as exc:  # 语法错、表名错、超时等：不修复，直接记失败
        result.error = f"SQL 执行失败：{exc}"

    result.latency_ms = int((time.perf_counter() - start) * 1000)
    return result
