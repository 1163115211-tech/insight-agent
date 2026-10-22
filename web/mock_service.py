"""Mock 事件源：在没有 `insight.service` 时给 Web 前端提供一条完整的假轨迹。

它严格按 `docs/CONTRACT.md` 第 3 节的事件协议产出 `StepEvent`，因此前端既不需要
知道数据是真是假，也能在真实服务就绪后一行不改地切过去。

轨迹（一轮完整演示）：
    meta/start -> intake/ok -> explore/ok -> plan/ok -> gen_sql/ok ->
    guard/ok -> execute/error -> repair/retry -> gen_sql/ok -> guard/ok ->
    clarify/interrupt   （此处前后端交互：POST /api/resume 后继续）
    -> execute/ok -> visualize/ok -> summarize/ok -> meta/done

用 `INSIGHT_MOCK=1` 强制走 mock；否则 web/app.py 会先尝试导入真实服务。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterator

try:  # 首选真实契约类型；单文件运行 mock 时退化到本地等价实现
    from insight.types import StepEvent  # type: ignore
except Exception:  # pragma: no cover - 仅在裸跑 mock 时触发

    @dataclass
    class StepEvent:  # type: ignore[no-redef]
        node: str
        status: str
        data: dict[str, Any] = field(default_factory=dict)
        ts: float = field(default_factory=time.time)

        def to_dict(self) -> dict[str, Any]:
            return {
                "node": self.node,
                "status": self.status,
                "data": self.data,
                "ts": self.ts,
            }


#: thread_id -> 中断现场，resume() 时从这里接着往下走
_THREADS: dict[str, dict[str, Any]] = {}


def _ev(node: str, status: str, data: dict[str, Any] | None = None) -> StepEvent:
    return StepEvent(node=node, status=status, data=data or {})


def _sleep(sec: float) -> None:
    """模拟逐节点耗时，让前端时间线能看见逐个点亮。"""
    time.sleep(sec)


# --------------------------------------------------------------------------
# 三个场景：渠道 GMV / 客单价走势 / 品类退款率
# 假数据只用于演示渲染，不代表真实业务结论。
# --------------------------------------------------------------------------

_SQL_BAD = """SELECT o.channel,
       SUM(o.gmv) / 10000.0 AS gmv_wan,
       COUNT(DISTINCT o.order_id) AS orders,
       ROUND(SUM(o.gmv) / COUNT(DISTINCT o.order_id), 2) AS aov
FROM orders o
WHERE o.paid_at >= '2026-01-01'
  AND o.paid_at <  '2026-10-01'
  AND o.status = 'paid'
GROUP BY o.channel
ORDER BY gmv_wan DESC;"""

_SQL_CHANNEL = """WITH order_gmv AS (
    SELECT o.channel,
           SUM(oi.quantity * oi.unit_price) AS gmv,
           COUNT(DISTINCT o.order_id)       AS orders
    FROM orders o
    JOIN order_items oi ON oi.order_id = o.order_id
    WHERE o.paid_at >= '2026-01-01'
      AND o.paid_at <  '2026-10-01'
      AND o.status = 'paid'
    GROUP BY o.channel
),
channel_returns AS (
    SELECT o.channel,
           COUNT(DISTINCT r.return_id) * 100.0
             / COUNT(DISTINCT o.order_id) AS return_rate
    FROM orders o
    LEFT JOIN returns r ON r.order_id = o.order_id
    WHERE o.paid_at >= '2026-01-01'
      AND o.paid_at <  '2026-10-01'
      AND o.status = 'paid'
    GROUP BY o.channel
)
SELECT g.channel,
       ROUND(g.gmv / 10000.0, 1)             AS gmv_wan,
       g.orders,
       ROUND(g.gmv / g.orders, 2)            AS aov,
       ROUND(u.return_rate, 2)               AS return_rate
FROM order_gmv g
JOIN channel_returns u ON u.channel = g.channel
ORDER BY g.gmv DESC
LIMIT 20;"""

_SQL_TREND = """SELECT strftime('%Y-%m', o.paid_at)                  AS month,
       COUNT(DISTINCT o.order_id)                     AS orders,
       ROUND(SUM(oi.quantity * oi.unit_price)
             / COUNT(DISTINCT o.order_id), 2)         AS aov
FROM orders o
JOIN order_items oi ON oi.order_id = o.order_id
WHERE o.paid_at >= '2025-10-01'
  AND o.paid_at <  '2026-10-01'
  AND o.status = 'paid'
GROUP BY month
ORDER BY month ASC;"""

_SQL_CATEGORY = """SELECT p.category,
       COUNT(DISTINCT oi.order_id) AS orders,
       COUNT(DISTINCT r.return_id) AS returns,
       ROUND(COUNT(DISTINCT r.return_id) * 100.0
             / COUNT(DISTINCT oi.order_id), 2) AS return_rate
FROM order_items oi
JOIN products p  ON p.product_id = oi.product_id
JOIN orders   o  ON o.order_id   = oi.order_id
LEFT JOIN returns r
       ON r.order_id = oi.order_id
      AND r.order_item_id = oi.order_item_id
WHERE o.paid_at >= '2026-09-01'
  AND o.paid_at <  '2026-10-01'
  AND o.status = 'paid'
GROUP BY p.category
ORDER BY return_rate DESC
LIMIT 8;"""

_CHANNEL_ROWS = [
    ["天猫", 1284.6, 5312, 241.83, 6.42],
    ["京东", 1092.3, 4408, 247.80, 5.11],
    ["抖音", 864.9, 5126, 168.72, 8.94],
    ["拼多多", 512.7, 4931, 104.02, 11.27],
    ["小程序", 402.8, 2104, 191.43, 4.36],
    ["线下", 198.5, 612, 324.35, 1.62],
]

_TREND_MONTHS = [
    "2025-10", "2025-11", "2025-12", "2026-01", "2026-02", "2026-03",
    "2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09",
]
_TREND_AOV = [186.4, 194.2, 211.8, 203.5, 178.9, 197.6, 205.1, 208.7, 214.3, 219.8, 226.1, 231.4]
_TREND_ORDERS = [1712, 1893, 2044, 1988, 1520, 1946, 2011, 2088, 2140, 2201, 2266, 2318]


def _chart_bar(labels: list[str], values: list[float], name: str) -> dict[str, Any]:
    return {
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
        "grid": {"left": 52, "right": 20, "top": 28, "bottom": 36},
        "xAxis": {"type": "category", "data": labels},
        "yAxis": {"type": "value", "name": name, "nameTextStyle": {"fontSize": 11}},
        "series": [
            {
                "name": name,
                "type": "bar",
                "barMaxWidth": 34,
                "data": values,
                "itemStyle": {"borderRadius": [3, 3, 0, 0]},
            }
        ],
    }


def _chart_line(labels: list[str], values: list[float], name: str) -> dict[str, Any]:
    return {
        "tooltip": {"trigger": "axis"},
        "grid": {"left": 52, "right": 20, "top": 28, "bottom": 40},
        "xAxis": {"type": "category", "data": labels, "boundaryGap": False},
        "yAxis": {"type": "value", "name": name, "scale": True},
        "series": [
            {
                "name": name,
                "type": "line",
                "symbol": "circle",
                "symbolSize": 6,
                "lineStyle": {"width": 2},
                "data": values,
            }
        ],
    }


def _scenario(question: str) -> dict[str, Any]:
    q = question or ""
    if any(k in q for k in ("走势", "趋势", "客单价", "按月", "月度")):
        return {
            "key": "trend",
            "tables": ["orders", "order_items"],
            "ddl_chars": 1926,
            "plan": [
                "确认时间范围：2025-10 至 2026-09 共 12 个月",
                "按支付月份聚合订单数与成交额",
                "用成交额 / 订单数得到客单价",
                "按月升序输出，供折线图使用",
            ],
            "bad_sql": _SQL_BAD,
            "sql": _SQL_TREND,
            "error": "sqlite3.OperationalError: no such column: o.gmv",
            "strategy": "读 order_items 明细：SUM(quantity * unit_price) 代替并不存在的 o.gmv 字段",
            "columns": ["month", "orders", "aov"],
            "rows": [[m, o, a] for m, o, a in zip(_TREND_MONTHS, _TREND_ORDERS, _TREND_AOV)],
            "chart": _chart_line(_TREND_MONTHS, _TREND_AOV, "客单价（元）"),
            "chart_type": "line",
            "answer": (
                "过去 12 个月客单价从 2025-10 的 186.4 元升至 2026-09 的 231.4 元，"
                "累计上涨约 24.1%。其中 2025-12 有一次年末冲高（211.8 元），"
                "随后 2026-01 回落，2026-02 是全年低点（178.9 元，春节档）。"
                "2026-06 起连续 4 个月环比为正，客单价进入稳定上行通道。"
            ),
        }
    if any(k in q for k in ("品类", "退款", "退货率", "退款率")):
        cats = ["女装", "鞋靴", "美妆", "手机数码", "家居家装", "食品生鲜", "母婴", "运动户外"]
        rates = [14.82, 12.46, 9.31, 7.05, 6.42, 4.18, 3.76, 3.12]
        orders = [3820, 2214, 2960, 1478, 1902, 4120, 2276, 1688]
        returns = [round(o * r / 100) for o, r in zip(orders, rates)]
        return {
            "key": "category",
            "tables": ["order_items", "products", "returns", "orders"],
            "ddl_chars": 3184,
            "plan": [
                "锁定时间范围：2026-09（上个月）",
                "按品类关联订单明细与退货单",
                "退货率 = 退货单数 / 下单订单数",
                "按退货率降序取前 8 个品类",
            ],
            "bad_sql": _SQL_BAD,
            "sql": _SQL_CATEGORY,
            "error": "sqlite3.OperationalError: no such column: p.refund_rate",
            "strategy": "退款率不存在预计算字段，改为 COUNT(returns) / COUNT(order_items) 现算",
            "columns": ["category", "orders", "returns", "return_rate"],
            "rows": [[c, o, r, ra] for c, o, r, ra in zip(cats, orders, returns, rates)],
            "chart": _chart_bar(cats, rates, "退货率（%）"),
            "chart_type": "bar",
            "answer": (
                "2026-09 退货率最高的是女装（14.82%），其次是鞋靴（12.46%）和美妆（9.31%），"
                "都明显高于全站均值；手机数码 7.05% 属于高单价、低退货的健康区间。"
                "女装与鞋靴合计贡献了当月退货量的近一半，建议优先核查这两个品类的尺码描述与实物色差。"
            ),
        }
    return {
        "key": "channel",
        "tables": ["orders", "order_items", "returns"],
        "ddl_chars": 2421,
        "plan": [
            "确认时间范围：2026-01-01 至 2026-09-30",
            "按渠道聚合成交额（quantity × unit_price）与订单数",
            "用成交额 / 订单数得到客单价",
            "LEFT JOIN returns 计算各渠道退货率，按 GMV 降序",
        ],
        "bad_sql": _SQL_BAD,
        "sql": _SQL_CHANNEL,
        "error": "sqlite3.OperationalError: no such column: o.gmv",
        "strategy": "GMV 不是存储字段，改从 order_items 用 SUM(quantity * unit_price) 现算，并补上退货关联",
        "columns": ["channel", "gmv_wan", "orders", "aov", "return_rate"],
        "rows": _CHANNEL_ROWS,
        "chart": _chart_bar(
            [r[0] for r in _CHANNEL_ROWS], [r[1] for r in _CHANNEL_ROWS], "GMV（万元）"
        ),
        "chart_type": "bar",
        "answer": (
            "2026 年 1–9 月全渠道 GMV 合计约 4,355.8 万元，天猫以 1,284.6 万元（占 29.5%）居首，"
            "京东 1,092.3 万元次之。值得注意的是：GMV 前二的渠道退货率只有 6.42% / 5.11%，"
            "而抖音、拼多多的退货率分别达到 8.94% 和 11.27%——增长有一部分是低质量 GMV，"
            "建议按「扣除退货后的净 GMV」再核算这两个渠道的投放 ROI。"
        ),
    }


def stream(question: str, *, thread_id: str | None = None) -> Iterator[StepEvent]:
    """按事件协议逐条 yield 假轨迹，最后停在 clarify/interrupt。

    与 `insight.service.stream()` 的调用方式、产出类型完全一致。
    """
    tid = thread_id or "th_mock_demo"
    sc = _scenario(question)

    _THREADS[tid] = {"question": question, "scenario": sc, "stage": "await_approval"}

    _sleep(0.15)
    yield _ev("meta", "start", {"thread_id": tid})

    _sleep(0.35)
    yield _ev("intake", "ok", {"reason": "识别为只读聚合分析，未包含写操作"})

    _sleep(0.75)
    yield _ev(
        "explore",
        "ok",
        {"tables": sc["tables"], "ddl_chars": sc["ddl_chars"]},
    )

    _sleep(0.6)
    yield _ev("plan", "ok", {"steps": sc["plan"]})

    _sleep(0.95)
    yield _ev("gen_sql", "ok", {"sql": sc["bad_sql"], "attempt": 1})

    _sleep(0.55)
    yield _ev(
        "guard",
        "ok",
        {
            "checks": [
                "只允许单条 SELECT",
                "禁止 INSERT / UPDATE / DELETE / DDL",
                "时间范围过滤已存在",
                "LIMIT 已设置",
            ]
        },
    )

    _sleep(0.7)
    yield _ev("execute", "error", {"error": sc["error"]})

    _sleep(0.9)
    yield _ev(
        "repair",
        "retry",
        {"attempt": 2, "error": sc["error"], "strategy": sc["strategy"]},
    )

    _sleep(0.85)
    yield _ev("gen_sql", "ok", {"sql": sc["sql"], "attempt": 2})

    _sleep(0.45)
    yield _ev(
        "guard",
        "ok",
        {"checks": ["只允许单条 SELECT", "字段与 4 张表的 DDL 一致", "预计扫描行数 4.8 万"]},
    )

    _sleep(0.55)
    yield _ev(
        "clarify",
        "interrupt",
        {
            "question": "这条 SQL 会扫描 orders × order_items 约 4.8 万行明细，是否继续执行？",
            "options": ["同意执行", "拒绝并改写"],
            "thread_id": tid,
        },
    )
    # 与真实服务一致：中断事件之后生成器正常结束，不抛异常。


def resume(thread_id: str, value: str) -> Iterator[StepEvent]:
    """从 clarify 中断处继续；拒绝时直接收尾，不执行查询。"""
    state = _THREADS.pop(thread_id, None)
    if state is None:
        raise ValueError(f"未知的 thread_id：{thread_id}（可能已结束或服务重启过）")

    sc = state["scenario"]
    rejected = any(k in (value or "") for k in ("拒绝", "取消", "reject", "cancel", "no"))

    if rejected:
        _sleep(0.45)
        yield _ev(
            "summarize",
            "ok",
            {"answer": "已按你的选择取消本次执行，未对数据库产生任何读写副作用。可以改写问题后重试。"},
        )
        _sleep(0.2)
        yield _ev(
            "meta",
            "done",
            {
                "latency_ms": 4102,
                "attempts": 2,
                "usage": {
                    "prompt_tokens": 1792,
                    "completion_tokens": 388,
                    "total_tokens": 2180,
                    "cost_cny": 0.0046,
                    "calls": 3,
                },
            },
        )
        return

    _sleep(0.95)
    yield _ev(
        "execute",
        "ok",
        {
            "columns": sc["columns"],
            "rows": sc["rows"],
            "row_count": len(sc["rows"]),
            "elapsed_ms": 43,
        },
    )

    _sleep(0.6)
    yield _ev(
        "visualize",
        "ok",
        {"chart": sc["chart"], "chart_type": sc["chart_type"]},
    )

    _sleep(0.7)
    yield _ev("summarize", "ok", {"answer": sc["answer"]})

    _sleep(0.25)
    yield _ev(
        "meta",
        "done",
        {
            "latency_ms": 8124,
            "attempts": 2,
            "usage": {
                "prompt_tokens": 2418,
                "completion_tokens": 613,
                "total_tokens": 3031,
                "cost_cny": 0.0074,
                "calls": 4,
            },
        },
    )


def run(question: str, *, thread_id: str | None = None):
    """与真实服务的 run() 对齐的阻塞版，便于脚本自测；返回事件列表。"""
    events = [e.to_dict() for e in stream(question, thread_id=thread_id)]
    return {"thread_id": thread_id or "th_mock_demo", "events": events, "interrupted": True}


if __name__ == "__main__":  # python -m web.mock_service  —— 冒烟看一遍协议
    import json
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    for e in stream("2026 年各渠道的 GMV 与退货率对比", thread_id="th_smoke"):
        print(json.dumps(e.to_dict(), ensure_ascii=False))
    for e in resume("th_smoke", "同意执行"):
        print(json.dumps(e.to_dict(), ensure_ascii=False))
