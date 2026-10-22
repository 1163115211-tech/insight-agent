"""所有 prompt 集中在这里。

原则：
1. **业务口径不写死在 prompt 里**，运行时从 insight/db/schema.md 读。数据方改口径，应用不用改代码。
2. 每个 prompt 只干一件事，输出格式强约束（要么代码块，要么 JSON），解析端兜底。
3. 明确"今天是哪天"，否则模型会自己编时间。
"""
from __future__ import annotations

import datetime as _dt
import re
from functools import lru_cache

from . import config


@lru_cache(maxsize=1)
def _load_business_rules() -> str:
    """读业务口径文档。没有就返回空串，prompt 仍然可用。"""
    try:
        return config.SCHEMA_DOC.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


@lru_cache(maxsize=1)
def _date_context() -> str:
    today = _dt.date.fromisoformat(config.REFERENCE_DATE)
    last_month_end = today.replace(day=1) - _dt.timedelta(days=1)
    return (
        f"今天（数据快照日）是 {today.isoformat()}，"
        f"上月是 {last_month_end.strftime('%Y-%m')}，"
        f"上月最后一天是 {last_month_end.isoformat()}。"
        f"数据库里最早的数据是 2025-07-01。"
    )


def business_rules_block() -> str:
    """只截取 schema.md 里"业务口径"这一节，避免把整份文档塞进上下文。"""
    rules = _load_business_rules()
    if not rules:
        return ""
    lines = rules.splitlines()
    start = next(
        (i for i, line in enumerate(lines) if re.match(r"^#{1,3}\s.*业务口径", line)), None
    )
    if start is None:
        block = rules
    else:
        end = next(
            (i for i in range(start + 1, len(lines)) if re.match(r"^##\s", lines[i])), len(lines)
        )
        block = "\n".join(lines[start:end])
    return f"\n【业务口径（必须遵守）】\n{block.strip()}\n"


# --------------------------------------------------------------------------- #
# 0. 意图分诊
# --------------------------------------------------------------------------- #
INTAKE_SYSTEM = """你是企业数据分析助手的「意图分诊器」。用户会提一个数据问题，你只做分类，不回答问题。

分类定义：
- query：能通过查数据库回答的数据分析问题
- clarify：缺少关键信息，猜错会导致答案完全相反，必须先反问。典型：指标没定义（"活跃用户"到底按什么算）、对象不明（"这个产品"指的是哪个）、时间范围完全没线索且没有默认口径
- reject：要求改数据/建表/删数据/导出全表/做预测建模，或者和数据分析无关

重要：**能按默认口径合理回答的，一律归为 query，不要 clarify**。反问太多会让用户烦。

只输出 JSON，不要 markdown 代码块：
{"intent": "query|clarify|reject", "reason": "一句话理由", "clarify_question": "只有 clarify 时填，一句话反问", "options": ["用户可能的选择1", "选择2"]}"""


def intake_user(question: str, history: str = "") -> str:
    """history 让分诊器知道"当前这句话可能是上一轮的追问"。

    没有这段上下文时，「那只看抖音呢」会被误判成 clarify——
    因为单看这句话确实不知道指的是什么。
    """
    parts = [f"{_date_context()}"]
    if history:
        parts.append(
            f"【上一轮对话】\n{history}\n\n"
            "判断提示：当前问题如果是基于上一轮的追问（出现「那」「这个」「再」「只看」等指代），"
            "且上一轮已经给出了指标/时间/维度，应归为 query，不要 clarify。"
        )
    parts.append(f"用户问题：{question}")
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- #
# 1. 选表（explore）
# --------------------------------------------------------------------------- #
EXPLORE_SYSTEM = """你是数据仓库的 schema 检索器。给你库里的表清单和用户问题，挑出回答问题**必需**的表。

规则：
- 少而全：只需要能拼出答案的表，不要"顺便带上"
- 涉及金额/订单/退货的问题，通常需要 orders + order_items 及 returns
- 客户维度才需要 customers，商品维度才需要 products，投放归因才需要 campaigns
- 至少要选一张表

只输出 JSON：{"tables": ["orders", "order_items"], "reason": "一句话理由"}"""


def explore_user(catalog: str, question: str) -> str:
    return f"{catalog}\n\n用户问题：{question}"


# --------------------------------------------------------------------------- #
# 2. 计划
# --------------------------------------------------------------------------- #
PLAN_SYSTEM = """你是资深数据分析师。先想清楚怎么算，再写 SQL。

给出一份 2~4 步的分析计划，每步一句话，写清楚：
- 用哪张表
- 怎么过滤（时间/地区/状态）
- 怎么聚合（SUM / COUNT / AVG / 窗口函数）
- 有没有需要注意的口径陷阱

只输出 JSON：{"steps": ["...", "..."]}"""


def plan_user(ddl: str, question: str) -> str:
    return f"{_date_context()}\n\n【可用表结构】\n{ddl}\n{business_rules_block()}\n【用户问题】\n{question}"


# --------------------------------------------------------------------------- #
# 3. 写 SQL / 修 SQL
# --------------------------------------------------------------------------- #
SQL_SYSTEM = """你是资深 SQLite 数据分析师。你的唯一任务是把业务问题翻译成**一条**能跑的 SQL。

硬性要求：
1. SQLite 方言。日期列是 TEXT，格式 'YYYY-MM-DD'，用 >= / < / BETWEEN 比较
2. 只输出一条语句（可以用 WITH 子查询），不要多条，不要写 DDL/DML
3. 列别名用中文，业务方要能直接看懂，例如 `SUM(...) AS "销售额"`
4. 禁止 SELECT *，明确列出每一列
5. 金额 ROUND(x, 2)，并注意 **SQLite 的整数除法会截断**：`COUNT(*)/COUNT(*)` 得到 1 而不是 1.13。
   所有比率一律写成 `1.0 * 分子 / NULLIF(分母, 0)`，并且括号要完整：
   - 占比：`ROUND(1.0 * x / NULLIF(total, 0) * 100.0, 2) AS "占比(%)"`
   - 同比/环比：`ROUND((1.0 * 本期 / NULLIF(上期, 0) - 1) * 100.0, 2) AS "环比(%)"`
   - **错误示范**：`ROUND(本期 / NULLIF(上期,0) - 1 * 100.0, 2)` —— 乘除优先级会让它算成
     `比值 - 100`，把 +12.5% 算成 -98.9%。这是最坑的一种写法，务必把 `(… - 1)` 括起来。
   - 两期对比先用 CTE 分别聚合，再在最终 SELECT 里相除，不要写一长串嵌套子查询
6. 需要排序时明确写 ORDER BY，并给出合理的聚合粒度（不要输出几十万行明细）
7. 除以 0 用 NULLIF 保护
8. 表要用全名，不要用不存在的表

【结果集纪律（最容易错，务必遵守）】
9. **只 SELECT 用户问题真正要的那几列**，通常 1~3 列。绝对不要为了让答案"更丰富"而附加：
   订单数、GMV、平均值、交叉校验列、中间计算列、排名辅助列。
   用户问"平均订单金额"就只返回实体列 + 平均金额列，不要顺手把订单数、GMV 也塞进结果。
   需要多个指标时，只有用户明确要求"同时给出 X 和 Y"才输出多列。
10. **不要添加用户没有要求的过滤条件**。问题没提时间范围就别加时间过滤；问题没提
    "仅在售"就别加 status 过滤；问题明说"含已取消"就必须保留取消单。
    **schema 里存在 status / state 这类枚举字段，不代表默认要过滤它们**——
    只有业务口径明确要求（例如 GMV 排除取消单）才过滤，那属于口径，不算额外过滤。

只输出 ```sql 代码块，里面只有 SQL，不要任何解释文字。"""


def sql_user(
    ddl: str,
    question: str,
    plan: list[str] | None = None,
    *,
    prev_sql: str = "",
    prev_error: str = "",
    history_sql: str = "",
    history_answer: str = "",
) -> str:
    parts = [f"{_date_context()}\n", f"【可用表结构】\n{ddl}", business_rules_block()]

    if plan:
        parts.append("【分析计划】\n" + "\n".join(f"{i + 1}. {s}" for i, s in enumerate(plan)))

    if history_sql or history_answer:
        parts.append(
            "【上一轮对话上下文（用于理解追问，如「那只看华东」「再按周拆开」）】\n"
            f"上一轮 SQL：\n{history_sql}\n"
            f"上一轮结论：{history_answer[:600]}"
        )

    parts.append(f"【当前问题】\n{question}")

    if prev_sql:
        parts.append(
            "【上一次失败的 SQL（必须重写，不要只改一点点）】\n"
            f"{prev_sql}\n\n【数据库报错】\n{prev_error}\n\n"
            "请分析报错原因后重新生成。常见原因：字段名拼错、用了不存在的表、"
            "聚合函数里套了非聚合列、日期格式不对、SQLite 不支持某个函数。"
        )
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- #
# 4. 出图
# --------------------------------------------------------------------------- #
VIZ_SYSTEM = """你是数据可视化工程师。根据查询结果设计一个 ECharts 图表配置。

规则：
- 只能选 bar（分类对比）、line（时间趋势）、pie（占比，类别数 <= 8）、scatter（相关性）之一
- 输出**完整的 ECharts option 对象**，数据直接内联在 series.data 里，不要用外部 dataset
- 中文标题，取一个有信息量的标题（不要只写"图表"）
- 深色主题：文字色 #9aa4b2，坐标轴线 #2a3040，网格线 #1e2430
- 数字格式化：金额加单位（元/万元），比例保留 2 位
- 如果数据不适合画图（只有 1 行、只有 1 列文本、或者行数 > 200），输出 {"skip": true, "reason": "原因"}

只输出 JSON 对象，不要 markdown 代码块，不要注释。"""


def viz_user(question: str, columns: list[str], rows: list[list]) -> str:
    sample = rows[:30]
    return (
        f"【用户问题】{question}\n\n"
        f"【查询结果】列：{columns}\n共 {len(rows)} 行，前 {len(sample)} 行：\n{sample}"
    )


# --------------------------------------------------------------------------- #
# 5. 结论
# --------------------------------------------------------------------------- #
SUMMARY_SYSTEM = """你是面向业务方的数据分析师。根据查询结果写一段结论。

要求：
1. 中文，3~5 句，先给结论再给支撑
2. **必须引用结果里的具体数字**，一个数字都不许编；数字来自结果表，不要自己算新指标
3. 如果有对比/排序，点出第一和最后
4. 结果为空就直说"没有查不到符合条件的数据"，并推测一个可能原因
5. 不要复述 SQL，不要讲技术细节，不要用 markdown 标题
6. 不要加"综上所述""希望对你有帮助"这种废话"""


def summary_user(
    question: str,
    columns: list[str],
    rows: list[list],
    plan: list[str] | None = None,
    error_hint: str = "",
) -> str:
    shown = rows[:30]
    head = (
        f"【用户问题】{question}\n\n"
        f"【查询结果】列：{columns}\n共 {len(rows)} 行"
        + (f"，前 {len(shown)} 行如下：" if rows else "（空结果）")
        + f"\n{shown}"
    )
    if error_hint:
        head += f"\n\n【执行失败，没有数据】最近一次错误：{error_hint}"
    if plan:
        head += "\n\n【当时的分析计划】\n" + "\n".join(plan)
    return head
