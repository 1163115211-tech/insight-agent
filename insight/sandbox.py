"""只读 SQL 沙箱。

三层防护，任何一层都能单独兜住事故：

1. **静态校验**（check_sql）
   - 只允许单条语句，且必须以 SELECT / WITH 开头
   - 关键字黑名单：INSERT/UPDATE/DELETE/DROP/ALTER/CREATE/ATTACH/PRAGMA...
   - 表名白名单：FROM / JOIN 后面出现的表必须在库的表清单里（CTE 名单独放行）
   - 没有 LIMIT 的查询自动补一条，防止把百万行拉进内存

2. **连接层**（execute）
   - `file:xxx?mode=ro` 以只读方式打开，SQLite 直接拒绝写入
   - `PRAGMA query_only = ON` 双保险

3. **执行层**（execute）
   - `set_progress_handler` 每 1 万条 VM 指令检查一次耗时，超时主动 abort
   - `fetchmany(max_rows + 1)` 限制返回行数，超出截断并标记 truncated

面试可以这么讲：为什么不用 regex 一把梭？因为 regex 会被字符串字面量和注释绕过，
所以静态校验只做"早失败"，真正的兜底是数据库连接层的只读模式 + 超时中断。
"""
from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

# 语句层黑名单（词边界匹配，先去掉注释和字符串字面量再匹配）
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|truncate|attach|detach|"
    r"pragma|vacuum|reindex|grant|revoke|begin|commit|rollback|savepoint)\b",
    re.IGNORECASE,
)

_WRITE_PREFIXES = ("insert", "update", "delete", "drop", "alter", "create", "replace",
                   "truncate", "attach", "detach", "pragma", "vacuum")


@dataclass
class CheckResult:
    ok: bool
    sql: str = ""
    reason: str = ""
    checks: list[str] = field(default_factory=list)


@dataclass
class ExecResult:
    ok: bool
    columns: list[str] = field(default_factory=list)
    rows: list[list] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    elapsed_ms: int = 0
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "columns": self.columns,
            "rows": self.rows,
            "row_count": self.row_count,
            "truncated": self.truncated,
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
        }


# --------------------------------------------------------------------------- #
# 文本清洗
# --------------------------------------------------------------------------- #
def extract_sql(text: str) -> str:
    """从模型输出里抠出 SQL：优先 ```sql 代码块，其次 ``` 代码块，最后整段。"""
    if not text:
        return ""
    m = re.search(r"```(?:sql|sqlite)?\s*\n?(.*?)```", text, re.IGNORECASE | re.DOTALL)
    sql = m.group(1) if m else text
    return _strip_comments(sql).strip()


def _strip_comments(sql: str) -> str:
    """去掉 -- 行注释和 /* */ 块注释，保留字符串字面量原样。"""
    out: list[str] = []
    i, n = 0, len(sql)
    quote: str | None = None
    while i < n:
        ch = sql[i]
        if quote:
            out.append(ch)
            if ch == quote:
                # SQL 里 '' 表示转义的单引号
                if i + 1 < n and sql[i + 1] == quote:
                    out.append(sql[i + 1])
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            while i < n and sql[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            i += 2
            while i + 1 < n and not (sql[i] == "*" and sql[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _split_statements(sql: str) -> list[str]:
    """按分号切语句，但跳过字符串字面量里的分号。"""
    parts: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                if i + 1 < n and sql[i + 1] == quote:
                    buf.append(sql[i + 1])
                    i += 2
                    continue
                quote = None
        elif ch in ("'", '"', "`"):
            quote = ch
            buf.append(ch)
        elif ch == ";":
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
        i += 1
    if "".join(buf).strip():
        parts.append("".join(buf).strip())
    return [p for p in parts if p]


def _mask_literals(sql: str) -> str:
    """把字符串字面量替换成占位符，避免关键字/表名匹配被内容干扰。"""
    out: list[str] = []
    quote: str | None = None
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if quote:
            if ch == quote:
                if i + 1 < n and sql[i + 1] == quote:
                    i += 2
                    continue
                quote = None
                out.append("'<str>'")
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


# --------------------------------------------------------------------------- #
# 静态校验
# --------------------------------------------------------------------------- #
def check_sql(sql: str, allowed_tables: set[str], max_rows: int = 1000) -> CheckResult:
    """静态校验 + 自动补 LIMIT。返回规范化后的 SQL。"""
    checks: list[str] = []

    if not sql or not sql.strip():
        return CheckResult(False, reason="SQL 为空")

    statements = _split_statements(sql)
    if len(statements) != 1:
        return CheckResult(False, reason=f"只允许单条语句，检测到 {len(statements)} 条")
    sql = statements[0].rstrip(";").strip()
    checks.append("单语句")

    masked = _mask_literals(sql)

    head = masked.lstrip().lower()
    if not (head.startswith("select") or head.startswith("with")):
        return CheckResult(False, reason="只允许 SELECT / WITH 查询")
    checks.append("只读开头")

    bad = _FORBIDDEN.search(masked)
    if bad:
        return CheckResult(False, reason=f"命中禁用关键字：{bad.group(1).upper()}")
    checks.append("关键字黑名单")

    # 表名白名单：FROM / JOIN 后面的标识符
    # CTE 名要单独放行，两种写法都要认：
    #   WITH recent AS (...)                 普通 CTE
    #   WITH RECURSIVE months(ym) AS (...)    递归 CTE + 列清单
    #   WITH a AS (...), b(x) AS (...)        多个 CTE
    if re.match(r"^\s*with\b", masked, re.IGNORECASE):
        cte_names = {
            m.group(1).lower()
            for m in re.finditer(
                r"([a-zA-Z_]\w*)\s*(?:\([^)]*\))?\s+as\s*\(", masked, re.IGNORECASE
            )
        }
    else:
        cte_names = set()
    refs = [
        m.group(1).lower()
        for m in re.finditer(r"\b(?:from|join)\s+([a-zA-Z_][\w]*)", masked, re.IGNORECASE)
    ]
    unknown = {t for t in refs if t not in allowed_tables and t not in cte_names}
    if unknown:
        return CheckResult(False, reason=f"引用了未知表：{', '.join(sorted(unknown))}")
    checks.append(f"表白名单({len(set(refs))}张)")

    # 自动补 LIMIT：没有顶层 limit 就加上（聚合查询加 LIMIT 无副作用）
    if not re.search(r"\blimit\b", masked, re.IGNORECASE):
        sql = f"{sql}\nLIMIT {max_rows}"
        checks.append(f"自动补 LIMIT {max_rows}")

    return CheckResult(True, sql=sql, checks=checks)


# --------------------------------------------------------------------------- #
# 只读执行
# --------------------------------------------------------------------------- #
def execute(
    sql: str,
    db_path: str | Path,
    *,
    max_rows: int = 1000,
    timeout_ms: int = 5000,
) -> ExecResult:
    """在只读沙箱里执行一条 SQL。超时、超行数都在这里兜住。"""
    db_path = Path(db_path)
    if not db_path.exists():
        return ExecResult(False, error=f"数据库不存在：{db_path}")

    uri = f"file:{db_path.as_posix()}?mode=ro"
    started = time.perf_counter()
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=timeout_ms / 1000)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")

        # SQLite 的 pitfall：progress_handler 返回非 0 会 abort 当前语句
        def _watchdog() -> int:
            return 1 if (time.perf_counter() - started) * 1000 > timeout_ms else 0

        conn.set_progress_handler(_watchdog, 10_000)

        cur = conn.execute(sql)
        columns = [d[0] for d in (cur.description or [])]
        fetched = cur.fetchmany(max_rows + 1)
        truncated = len(fetched) > max_rows
        rows = [[_jsonable(v) for v in tuple(r)] for r in fetched[:max_rows]]
        elapsed = int((time.perf_counter() - started) * 1000)
        return ExecResult(
            True, columns=columns, rows=rows, row_count=len(rows),
            truncated=truncated, elapsed_ms=elapsed,
        )
    except sqlite3.OperationalError as e:
        msg = str(e)
        if "interrupted" in msg.lower():
            msg = f"查询超过 {timeout_ms}ms 被中断，请加聚合或缩小时间范围"
        return ExecResult(False, error=msg, elapsed_ms=int((time.perf_counter() - started) * 1000))
    except sqlite3.Error as e:
        return ExecResult(False, error=f"SQL 执行失败：{e}")
    finally:
        if conn is not None:
            conn.set_progress_handler(None, 0)
            conn.close()


def _jsonable(v):
    """SQLite 值转 JSON 友好类型。"""
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace")
    if isinstance(v, float):
        return round(v, 4)
    return v
