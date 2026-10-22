"""数据访问工具层。

agent 不直接拼 SQL 去摸库，而是通过这一层拿"数据库长什么样"：
- list_tables：有哪些表、多少行
- describe_table：某张表的 DDL + 样例
- sample_rows：抽样看值长什么样（省份怎么写、status 有哪些枚举）

这一层的价值在于**把数据库翻译成模型能懂的上下文**，也顺便成为 CLI `describe` 的数据来源。
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from . import config


def _connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path or config.DB_PATH)
    uri = f"file:{path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def table_names(db_path: str | Path | None = None) -> set[str]:
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    return {r["name"] for r in rows}


def list_tables(db_path: str | Path | None = None) -> list[dict]:
    """返回 [{name, rows, columns:[...]}]，用于 explore 节点选表。"""
    with closing(_connect(db_path)) as conn:
        names = [
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        out = []
        for name in names:
            cols = [r["name"] for r in conn.execute(f'PRAGMA table_info("{name}")')]
            count = conn.execute(f'SELECT COUNT(*) AS c FROM "{name}"').fetchone()["c"]
            out.append({"name": name, "rows": count, "columns": cols})
    return sorted(out, key=lambda t: t["name"])


def describe_table(table: str, db_path: str | Path | None = None) -> str:
    """输出建表语句 + 3 行样例，给模型判断字段含义。"""
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not row:
            raise ValueError(f"表不存在：{table}")
        ddl = row["sql"] or ""
        samples = conn.execute(f'SELECT * FROM "{table}" LIMIT 3').fetchall()
    lines = [ddl, "", f"-- {table} 样例 (3 行):"]
    for s in samples:
        lines.append("-- " + ", ".join(f"{k}={s[k]}" for k in s.keys()))
    return "\n".join(lines)


def sample_rows(table: str, k: int = 3, db_path: str | Path | None = None) -> list[dict]:
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(f'SELECT * FROM "{table}" LIMIT ?', (k,)).fetchall()
    return [dict(r) for r in rows]


def build_catalog(db_path: str | Path | None = None) -> str:
    """给 explore 用的紧凑目录：表名 + 行数 + 列名，不塞样例（省 token）。"""
    lines = ["库中可用的表："]
    for t in list_tables(db_path):
        lines.append(f"- {t['name']}（{t['rows']} 行）：{', '.join(t['columns'])}")
    return "\n".join(lines)


def build_ddl(tables: list[str], db_path: str | Path | None = None) -> str:
    """只给选中表的完整 DDL，这是"上下文工程"里最省钱的一步。"""
    parts: list[str] = []
    with closing(_connect(db_path)) as conn:
        for t in tables:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (t,)
            ).fetchone()
            if row and row["sql"]:
                parts.append(row["sql"].strip() + ";")
    return "\n\n".join(parts)
