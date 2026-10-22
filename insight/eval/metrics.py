"""评测指标：结果集比较、执行准确率、SQL 合法率、自修复成功率、延迟/成本汇总。

核心难点是**结果集比较**。Text-to-SQL 的"对"不是字符串相等：
- 列顺序不该敏感：`SELECT channel, gmv` 和 `SELECT gmv, channel` 是同一个答案；
- 行顺序不该敏感：没写 ORDER BY 时 SQLite 的返回顺序不稳定；
- 数值要有容差：`ROUND(SUM(...), 2)` 与 `SUM(ROUND(...))` 可能差几分钱；
- NULL 只能和 NULL 相等，不能当成 0 或空字符串。

因此这里做的是「带容差的行多重集 + 列双射匹配」：
1. 行数、列数不同直接判错（比任何单值差异都更根本）；
2. 先找 gold 列到 pred 列的双射（列内容作为多重集在容差下相等），解决列顺序问题；
3. 再在列对齐后的行之间做双射（二分图匹配），解决行顺序和重复行问题；
4. 都匹配上才算执行准确。

为什么用二分图匹配而不是排序后逐行比：容差相等不满足传递性（100.00、100.01、100.02
可能两两"相等"），排序+贪心在边界会误判；行/列数最多几十，O(n²) 匈牙利足够。
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Sequence

# 数值容差：题目要求 1e-2。再加一个极小的相对项吸收浮点表示误差，
# 不用 1e-6 之类的大相对容差——那会把百万级金额差几块钱的错答案放过。
ABS_TOL = 1e-2
REL_TOL = 1e-9

# 失败分类：报告里按这个归因，面试追问时能直接对应到 Agent 的哪一环。
FAILURE_NONE = ""
FAILURE_NO_SQL = "解析失败/无SQL"
FAILURE_SQL_ERROR = "SQL执行失败"
FAILURE_EMPTY = "结果为空"
FAILURE_WRONG = "结果不对"
FAILURE_TIMEOUT = "超时"
FAILURE_GRAPH = "图执行异常"
FAILURE_INTERRUPTED = "澄清中断"


def _as_number(value: Any) -> float | None:
    """数值化；字符串日期/渠道名会返回 None，不会误当数字。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def values_equal(a: Any, b: Any) -> bool:
    """单值比较：NULL 对 NULL、数值带容差、数字字符串可和数字比、其余严格相等。

    >>> values_equal(None, None)
    True
    >>> values_equal(None, 0)
    False
    >>> values_equal(100.0, 100.005)
    True
    >>> values_equal(100.0, 100.02)
    False
    >>> values_equal("100.00", 100)
    True
    >>> values_equal("App", "app")
    False
    """
    if a is None or b is None:
        return a is None and b is None
    # 两边都是字符串时严格比，避免把 "001" 和 "1" 当成相同
    if isinstance(a, str) and isinstance(b, str):
        return a == b
    num_a = _as_number(a)
    num_b = _as_number(b)
    if num_a is not None and num_b is not None:
        return math.isclose(num_a, num_b, rel_tol=REL_TOL, abs_tol=ABS_TOL)
    return a == b


def _bijection(left: Sequence[Any], right: Sequence[Any], eq: Callable[[Any, Any], bool]) -> list[int] | None:
    """求 left -> right 的完美匹配（返回 left 每个元素对应的 right 下标）。

    用 DFS 增广路（匈牙利算法）。数量级只有几十，可读性优先。
    """
    n = len(left)
    if n != len(right):
        return None
    match_right = [-1] * n  # right 下标 -> left 下标

    def try_match(i: int, seen: list[bool]) -> bool:
        for j in range(n):
            if seen[j] or not eq(left[i], right[j]):
                continue
            seen[j] = True
            if match_right[j] == -1 or try_match(match_right[j], seen):
                match_right[j] = i
                return True
        return False

    for i in range(n):
        if not try_match(i, [False] * n):
            return None
    mapping = [-1] * n
    for j, i in enumerate(match_right):
        mapping[i] = j
    return mapping


def compare_result_sets(
    gold_columns: Sequence[str],
    gold_rows: Sequence[Sequence[Any]],
    pred_columns: Sequence[str],
    pred_rows: Sequence[Sequence[Any]],
) -> tuple[bool, str, str]:
    """比较两个结果集，返回 (是否正确, 差异类型, 可读差异)。

    差异类型用于报告分类：row_count / column_count / column_align / row_mismatch。
    列名不参与比较（LLM 起别名千奇百怪，值对才是对）。

    >>> compare_result_sets(["ch"], [("App",)], ["whatever"], [("App",)])[0]
    True
    >>> compare_result_sets(["a"], [(1,)], ["x", "y"], [(1, 2)])[0]
    False
    >>> compare_result_sets(["a"], [(1,)], ["b"], [(2,)])[:2]
    (False, 'column_align')
    """
    g_cols, p_cols = list(gold_columns), list(pred_columns)
    g_rows = [tuple(r) for r in gold_rows]
    p_rows = [tuple(r) for r in pred_rows]

    if len(g_rows) != len(p_rows):
        return False, "row_count", f"行数不同：gold={len(g_rows)}，pred={len(p_rows)}"
    if len(g_cols) != len(p_cols):
        return False, "column_count", f"列数不同：gold={len(g_cols)}，pred={len(p_cols)}"
    if not g_rows:
        # 两边都空：结构一致就算对（评测集本身要求非空，这里只是兜底）
        return True, "", ""

    # 1) 列对齐：gold 第 j 列 与 pred 第 k 列的值多重集在容差下相等
    def col_equal(j: int, k: int) -> bool:
        return _bijection([r[j] for r in g_rows], [r[k] for r in p_rows], values_equal) is not None

    col_map = _bijection(list(range(len(g_cols))), list(range(len(p_cols))), col_equal)
    if col_map is None:
        return False, "column_align", "列内容无法一一对应（按值匹配失败）"
    p_rows = [tuple(row[k] for k in col_map) for row in p_rows]

    # 2) 行对齐：列对齐后逐行做多重集匹配
    row_map = _bijection(g_rows, p_rows, lambda a, b: all(values_equal(x, y) for x, y in zip(a, b)))
    if row_map is None:
        return False, "row_mismatch", _first_row_diff(g_rows, p_rows)

    return True, "", ""


def _injective_mapping(left_n: int, right_n: int, eq: Callable[[int, int], bool]) -> list[int] | None:
    """left 的每一列都要找到 right 里不同的列（可多不可少），返回映射。"""
    mapping = [-1] * left_n

    def backtrack(i: int, used: set[int]) -> bool:
        if i == left_n:
            return True
        for j in range(right_n):
            if j in used or not eq(i, j):
                continue
            mapping[i] = j
            used.add(j)
            if backtrack(i + 1, used):
                return True
            used.remove(j)
            mapping[i] = -1
        return False

    return list(mapping) if backtrack(0, set()) else None


def compare_projection(
    gold_columns: Sequence[str],
    gold_rows: Sequence[Sequence[Any]],
    pred_columns: Sequence[str],
    pred_rows: Sequence[Sequence[Any]],
) -> bool:
    """诊断指标「关键列命中」：gold 的列值都能在 pred 结果里按行找到，忽略多余列。

    为什么不拿它当执行准确率：问题问"平均订单金额"，返回 4 列里夹着正确答案，
    对用户可能可接受，但不是标准 Text-to-SQL 执行准确率的定义。这里只作为
    "多余列 vs 真算错"的归因依据，报告里单独列出，避免用宽松口径把分数做高。

    >>> compare_projection(["v"], [(1,)], ["v", "extra"], [(1, "x")])
    True
    >>> compare_projection(["v"], [(1,)], ["v", "extra"], [(2, "x")])
    False
    """
    g_rows = [tuple(r) for r in gold_rows]
    p_rows = [tuple(r) for r in pred_rows]
    if len(g_rows) != len(p_rows) or not g_rows:
        return False

    def col_equal(i: int, j: int) -> bool:
        return _bijection([r[i] for r in g_rows], [r[j] for r in p_rows], values_equal) is not None

    mapping = _injective_mapping(len(gold_columns), len(pred_columns), col_equal)
    if mapping is None:
        return False
    reordered = [tuple(row[k] for k in mapping) for row in p_rows]
    return _bijection(g_rows, reordered, lambda a, b: all(values_equal(x, y) for x, y in zip(a, b))) is not None


def _first_row_diff(g_rows: Sequence[tuple], p_rows: Sequence[tuple], max_examples: int = 3) -> str:
    """给报告用的可读差异：先按行排序，再找第一条不同的行/列。

    注意：仅用于解释失败，不参与判定（判定是上面的双射匹配）。
    """
    def sort_key(row: tuple) -> tuple:
        return tuple((v is None, type(v).__name__, v if not isinstance(v, (int, float)) else float(v)) for v in row)

    try:
        g_sorted = sorted(g_rows, key=sort_key)
        p_sorted = sorted(p_rows, key=sort_key)
    except TypeError:
        g_sorted, p_sorted = list(g_rows), list(p_rows)

    examples: list[str] = []
    for i, (g, p) in enumerate(zip(g_sorted, p_sorted)):
        for j, (gv, pv) in enumerate(zip(g, p)):
            if not values_equal(gv, pv):
                examples.append(f"行{i + 1}列{j + 1}: gold={gv!r} pred={pv!r}")
                break
        if len(examples) >= max_examples:
            break
    if not examples:
        examples.append("行内容一致但多重集无法匹配（可能是容差导致的边界情况）")
    return "；".join(examples)


@dataclass
class EvalRecord:
    """一道题的完整评测记录，jsonl 一行。

    gold/pred 的 rows 也落盘，报告里的"典型失败案例"要直接引用，不用二次重跑。
    """

    id: str
    question: str
    difficulty: str
    tags: list[str]
    config: str                     # graph | baseline
    gold_sql: str
    pred_sql: str = ""
    correct: bool = False
    projection_correct: bool = False  # 诊断：忽略多余列后关键列是否命中
    exec_ok: bool = False           # 生成的 SQL 在库上执行成功（SQL 合法率口径）
    compare_reason: str = ""        # row_count / column_count / column_align / row_mismatch
    detail: str = ""
    failure_category: str = FAILURE_NONE
    error: str | None = None
    attempts: int = 0
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_cny: float = 0.0
    llm_calls: int = 0
    gold_columns: list[str] = field(default_factory=list)
    gold_rows: list[list[Any]] = field(default_factory=list)
    pred_columns: list[str] = field(default_factory=list)
    pred_rows: list[list[Any]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["total_tokens"] = self.total_tokens
        return d


def classify_failure(*, pred_sql: str, exec_ok: bool, correct: bool, error: str | None,
                     interrupted: bool = False, pred_empty: bool = False) -> str:
    """把一次失败归到可行动的分类，供报告统计和后续优化。"""
    if correct:
        return FAILURE_NONE
    if interrupted:
        return FAILURE_INTERRUPTED
    err = (error or "").lower()
    if "timeout" in err or "timed out" in err:
        return FAILURE_TIMEOUT
    if not pred_sql:
        return FAILURE_NO_SQL
    if not exec_ok:
        return FAILURE_SQL_ERROR
    if pred_empty:
        return FAILURE_EMPTY
    if error:
        # SQL 能跑但图后续节点报错（例如可视化/总结）也算图异常
        return FAILURE_GRAPH
    return FAILURE_WRONG


def _mean(values: Sequence[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def _percentile(values: Sequence[float], pct: float) -> float:
    """最近秩百分位（不插值），样本量小时比插值更不容易骗自己。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = max(0, math.ceil(pct / 100 * len(ordered)) - 1)
    return round(float(ordered[idx]), 2)


def aggregate(records: Sequence[EvalRecord]) -> dict[str, Any]:
    """汇总一个大 config 的所有题，输出报告/控制台表要的指标。"""
    n = len(records)
    if n == 0:
        return {"n": 0}

    correct = [r for r in records if r.correct]
    exec_ok = [r for r in records if r.exec_ok]
    repaired = [r for r in records if r.attempts >= 2]
    repaired_ok = [r for r in repaired if r.exec_ok]
    latencies = [r.latency_ms for r in records]
    tokens = [r.total_tokens for r in records]
    costs = [r.cost_cny for r in records]
    failures = Counter(r.failure_category for r in records if r.failure_category)
    by_difficulty: dict[str, dict[str, Any]] = {}
    for diff in ("easy", "medium", "hard"):
        subset = [r for r in records if r.difficulty == diff]
        if subset:
            by_difficulty[diff] = {
                "n": len(subset),
                "execution_accuracy": _mean([1.0 if r.correct else 0.0 for r in subset]),
                "avg_attempts": _mean([r.attempts for r in subset]),
            }

    return {
        "n": n,
        "execution_accuracy": _mean([1.0 if r.correct else 0.0 for r in records]),
        "projection_accuracy": _mean([1.0 if (r.correct or r.projection_correct) else 0.0 for r in records]),
        "sql_valid_rate": _mean([1.0 if r.exec_ok else 0.0 for r in records]),
        "self_repair_success_rate": (round(len(repaired_ok) / len(repaired), 4) if repaired else None),
        "repaired_runs": len(repaired),
        "avg_attempts": _mean([r.attempts for r in records]),
        "avg_prompt_tokens": round(_mean([r.prompt_tokens for r in records]), 1),
        "avg_completion_tokens": round(_mean([r.completion_tokens for r in records]), 1),
        "avg_total_tokens": round(_mean(tokens), 1),
        "avg_cost_cny": round(_mean(costs), 6),
        "total_cost_cny": round(sum(costs), 4),
        "total_latency_ms": sum(latencies),
        "p50_latency_ms": _percentile(latencies, 50),
        "p95_latency_ms": _percentile(latencies, 95),
        "failure_categories": dict(sorted(failures.items(), key=lambda kv: -kv[1])),
        "by_difficulty": by_difficulty,
        "correct": len(correct),
        "exec_ok": len(exec_ok),
    }


def strip_sql_fence(text: str) -> str:
    """从模型回复里抠 SQL：兼容 ```sql 围栏、裸 SQL、前后解释文字。"""
    if not text:
        return ""
    fenced = re.search(r"```(?:sql)?\s*(.*?)```", text, re.I | re.S)
    if fenced:
        return fenced.group(1).strip().rstrip(";").strip()
    # 没有围栏时找第一条 SELECT/WITH 到分号（或文本结尾）
    match = re.search(r"(SELECT|WITH)\b.*", text, re.I | re.S)
    if not match:
        return ""
    sql = match.group(0).strip()
    if ";" in sql:
        sql = sql.split(";")[0].strip()
    return sql


def _self_test() -> None:
    # 行顺序不敏感
    assert compare_result_sets(["c"], [(1,), (2,)], ["x"], [(2,), (1,)])[0]
    # 列顺序不敏感
    assert compare_result_sets(["a", "b"], [("App", 10)], ["b", "a"], [(10, "App")])[0]
    # 数值容差
    assert compare_result_sets(["v"], [(100.0,)], ["v"], [(100.005,)])[0]
    assert not compare_result_sets(["v"], [(100.0,)], ["v"], [(100.03,)])[0]
    # NULL 处理
    assert compare_result_sets(["v"], [(None,)], ["v"], [(None,)])[0]
    assert not compare_result_sets(["v"], [(None,)], ["v"], [(0,)])[0]
    # 重复行按多重集比较
    assert compare_result_sets(["v"], [(1,), (1,), (2,)], ["v"], [(2,), (1,), (1,)])[0]
    assert not compare_result_sets(["v"], [(1,), (1,)], ["v"], [(1,), (2,)])[0]
    # 行数不同
    ok, reason, _ = compare_result_sets(["v"], [(1,)], ["v"], [(1,), (2,)])
    assert not ok and reason == "row_count"
    # 列内容能对齐、但行配对不上（两列的值集合相同，行内配对被打乱）
    ok, reason, _ = compare_result_sets(["a", "b"], [(1, 2), (2, 1)], ["a", "b"], [(1, 1), (2, 2)])
    assert not ok and reason == "row_mismatch"
    # 数字字符串与数字等价（模型 CAST 或驱动返回差异）
    assert compare_result_sets(["v"], [("100.00",)], ["v"], [(100,)])[0]
    # 文本严格比较
    assert not compare_result_sets(["v"], [("App",)], ["v"], [("app",)])[0]
    # 关键列诊断：多余列不影响，但值错/行错仍不算
    assert compare_projection(["v"], [(1,)], ["v", "extra"], [(1, "x")])
    assert not compare_projection(["v"], [(1,)], ["v", "extra"], [(2, "x")])
    assert not compare_projection(["v"], [(1,), (2,)], ["v"], [(1,), (2,), (3,)])
    assert compare_projection(["v"], [(1,), (2,)], ["v"], [(2,), (1,)])
    # SQL 提取
    assert strip_sql_fence("```sql\nSELECT 1;\n```") == "SELECT 1"
    assert strip_sql_fence("好的，SQL 如下：\nSELECT 1 AS x;\n解释略") == "SELECT 1 AS x"
    assert strip_sql_fence("抱歉我无法回答") == ""
    # 失败分类
    assert classify_failure(pred_sql="", exec_ok=False, correct=False, error=None) == FAILURE_NO_SQL
    assert classify_failure(pred_sql="SELECT 1", exec_ok=False, correct=False, error="syntax error") == FAILURE_SQL_ERROR
    assert classify_failure(pred_sql="SELECT 1", exec_ok=True, correct=False, error=None) == FAILURE_WRONG
    assert classify_failure(pred_sql="SELECT 1", exec_ok=True, correct=False, error="timeout after 60s") == FAILURE_TIMEOUT
    print("metrics self-test OK")


if __name__ == "__main__":
    import doctest

    doctest.testmod()
    _self_test()
