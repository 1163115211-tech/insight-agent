"""沙箱与静态校验的单测——这部分是安全边界，必须有测试兜着。

    python -m pytest tests/ -q
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from insight import sandbox  # noqa: E402

TABLES = {"orders", "order_items", "customers"}


def test_extract_sql_from_fence():
    text = "这是 SQL：\n```sql\nSELECT 1;\n```\n解释一下"
    assert sandbox.extract_sql(text) == "SELECT 1;"


def test_extract_sql_strips_line_comments():
    text = "```sql\n-- 取订单\nSELECT 1;\n```"
    assert sandbox.extract_sql(text) == "SELECT 1;"


def test_reject_dml():
    for bad in (
        "DELETE FROM orders",
        "UPDATE orders SET status='x'",
        "DROP TABLE orders",
        "INSERT INTO orders VALUES (1)",
        "PRAGMA table_info(orders)",
    ):
        r = sandbox.check_sql(bad, TABLES)
        assert not r.ok, bad


def test_reject_multi_statement():
    r = sandbox.check_sql("SELECT 1; SELECT 2", TABLES)
    assert not r.ok and "单条" in r.reason


def test_semicolon_inside_string_is_not_statement():
    r = sandbox.check_sql("SELECT * FROM orders WHERE channel = 'a;b'", TABLES)
    assert r.ok


def test_reject_unknown_table():
    r = sandbox.check_sql("SELECT * FROM secret_table", TABLES)
    assert not r.ok and "未知表" in r.reason


def test_cte_name_allowed():
    sql = "WITH recent AS (SELECT * FROM orders) SELECT * FROM recent"
    r = sandbox.check_sql(sql, TABLES)
    assert r.ok, r.reason


def test_cte_with_column_list_allowed():
    """回归：WITH months(ym) AS (...) 这种带列清单的 CTE 曾被误判成未知表。"""
    sql = (
        "WITH RECURSIVE months(ym) AS ("
        "SELECT '2026-01' UNION ALL SELECT ym FROM months"
        ") SELECT ym FROM months"
    )
    r = sandbox.check_sql(sql, TABLES)
    assert r.ok, r.reason


def test_multiple_ctes_with_column_lists():
    sql = (
        "WITH a(x) AS (SELECT 1), b(y) AS (SELECT 2) "
        "SELECT a.x FROM a JOIN b ON 1=1"
    )
    r = sandbox.check_sql(sql, TABLES)
    assert r.ok, r.reason


def test_keyword_inside_string_not_flagged():
    sql = "SELECT * FROM orders WHERE channel = 'delete me'"
    r = sandbox.check_sql(sql, TABLES)
    assert r.ok, r.reason


def test_auto_limit_added():
    r = sandbox.check_sql("SELECT * FROM orders", TABLES, max_rows=100)
    assert r.ok and "LIMIT 100" in r.sql


def test_existing_limit_kept():
    r = sandbox.check_sql("SELECT * FROM orders LIMIT 5", TABLES, max_rows=100)
    assert r.ok and "LIMIT 5" in r.sql and "LIMIT 100" not in r.sql


# --------------------------------------------------------------------------- #
# 真实执行（临时 sqlite 文件）
# --------------------------------------------------------------------------- #
@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "t.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE orders(order_id INTEGER PRIMARY KEY, channel TEXT, amount REAL);
        INSERT INTO orders VALUES (1,'App',100.0),(2,'抖音',50.0);
        """
    )
    conn.commit()
    conn.close()
    return path


def test_execute_ok(db):
    r = sandbox.execute("SELECT channel, amount FROM orders ORDER BY amount DESC", db)
    assert r.ok and r.columns == ["channel", "amount"]
    assert r.rows[0] == ["App", 100.0]


def test_execute_row_limit(db):
    r = sandbox.execute("SELECT * FROM orders", db, max_rows=1)
    assert r.ok and r.row_count == 1 and r.truncated


def test_execute_write_blocked(db):
    """连接层是只读 URI + PRAGMA query_only，双保险。"""
    r = sandbox.execute("DELETE FROM orders", db)
    assert not r.ok
    assert "readonly" in r.error.lower() or "read-only" in r.error.lower() or "only" in r.error.lower()


def test_execute_bad_sql_returns_error(db):
    r = sandbox.execute("SELECT nope FROM orders", db)
    assert not r.ok and "no such column" in r.error


def test_execute_timeout(db):
    """递归 CTE 死循环必须被 progress handler 打断。"""
    sql = (
        "WITH RECURSIVE cnt(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM cnt) "
        "SELECT count(*) FROM cnt"
    )
    r = sandbox.execute(sql, db, timeout_ms=300)
    assert not r.ok and "中断" in r.error
