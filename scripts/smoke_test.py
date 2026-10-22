"""最小闭环冒烟测试。

不依赖 52 题评测集，自己造一个迷你库，验证「分诊 → 选表 → 计划 → 写 SQL →
校验 → 执行 → 出图 → 结论」整条链路，以及 token 记账是否正常。

用法：
    python scripts/smoke_test.py
    python scripts/smoke_test.py --keep     # 保留临时库，方便手动 inspect

退出码 0 表示链路通。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB = ROOT / "data" / "_smoke.db"
CKPT = ROOT / "data" / "_smoke_checkpoints.sqlite"
os.environ.setdefault("INSIGHT_DB", str(DB))
os.environ.setdefault("INSIGHT_CHECKPOINT", str(CKPT))

DDL = """
CREATE TABLE customers(customer_id INTEGER PRIMARY KEY, name TEXT, gender TEXT, age INTEGER,
    city TEXT, province TEXT, register_date TEXT, member_level TEXT);
CREATE TABLE products(product_id INTEGER PRIMARY KEY, product_name TEXT, category TEXT,
    brand TEXT, price REAL, cost REAL, launch_date TEXT, status TEXT);
CREATE TABLE orders(order_id INTEGER PRIMARY KEY, customer_id INTEGER, order_date TEXT,
    channel TEXT, status TEXT, total_amount REAL, campaign_id INTEGER);
CREATE TABLE order_items(item_id INTEGER PRIMARY KEY, order_id INTEGER, product_id INTEGER,
    quantity INTEGER, unit_price REAL);
CREATE TABLE returns(return_id INTEGER PRIMARY KEY, order_id INTEGER, item_id INTEGER,
    product_id INTEGER, quantity INTEGER, refund_amount REAL, return_date TEXT,
    reason TEXT, status TEXT);
CREATE TABLE campaigns(campaign_id INTEGER PRIMARY KEY, campaign_name TEXT, channel TEXT,
    start_date TEXT, end_date TEXT, budget REAL, target_category TEXT);
"""

# 注意：这里的枚举值要和 insight/db/schema.md 的业务口径一致（status 用「已完成/已取消」），
# 否则模型按口径过滤后会查出空结果——这正是真实项目里最容易踩的坑。
DATA = {
    "customers": [
        (1, "张三", "男", 28, "杭州", "华东", "2025-08-01", "黄金"),
        (2, "李四", "女", 31, "上海", "华东", "2025-09-12", "普通"),
        (3, "王五", "男", 24, "成都", "西南", "2026-01-05", "普通"),
    ],
    "products": [
        (1, "降噪耳机", "数码", "某品牌", 899.0, 420.0, "2025-07-10", "在售"),
        (2, "机械键盘", "数码", "某品牌", 499.0, 210.0, "2025-08-02", "在售"),
        (3, "保温杯", "生活", "某品牌", 129.0, 45.0, "2025-06-01", "在售"),
    ],
    "orders": [
        (1001, 1, "2026-09-03", "抖音", "已完成", 1157.0, 1),
        (1002, 2, "2026-09-11", "天猫", "已完成", 474.05, 1),
        (1003, 3, "2026-09-21", "抖音", "已完成", 1438.4, 2),
        (1004, 1, "2026-08-15", "天猫", "已完成", 129.0, None),
        (1005, 2, "2026-09-28", "京东", "已取消", 899.0, None),
    ],
    "order_items": [
        (1, 1001, 1, 1, 899.0),
        (2, 1001, 3, 2, 129.0),
        (3, 1002, 2, 1, 499.0),
        (4, 1003, 1, 2, 899.0),
        (5, 1004, 3, 1, 129.0),
        (6, 1005, 1, 1, 899.0),
    ],
    "returns": [
        (1, 1001, 1, 1, 1, 899.0, "2026-09-09", "七天无理由", "已完成"),
        (2, 1003, 4, 1, 1, 899.0, "2026-09-27", "质量问题", "处理中"),
    ],
    "campaigns": [
        (1, "秋季大促", "抖音", "2026-09-01", "2026-09-15", 50000.0, "数码"),
        (2, "开学季", "抖音", "2026-09-18", "2026-09-30", 30000.0, "数码"),
    ],
}


def build() -> None:
    DB.parent.mkdir(parents=True, exist_ok=True)
    if DB.exists():
        DB.unlink()
    conn = sqlite3.connect(DB)
    conn.executescript(DDL)
    for table, rows in DATA.items():
        placeholders = ",".join("?" * len(rows[0]))
        conn.executemany(f"INSERT INTO {table} VALUES ({placeholders})", rows)
    conn.commit()
    conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="保留临时文件")
    parser.add_argument("--question", default="上个月各渠道的销售额是多少，按从高到低排序")
    args = parser.parse_args()

    build()
    from insight import service  # 必须在设置环境变量之后导入

    print(f"临时库：{DB}")
    print(f"问题：{args.question}\n")

    result = service.run(args.question)

    for e in result.trace:
        d = e["data"]
        if e["node"] == "meta" and e["status"] == "done":
            print(f"[meta done] {d}")
        elif e["status"] in ("ok", "error", "retry"):
            brief = {k: v for k, v in d.items() if k in ("tables", "steps", "sql", "reason", "row_count", "attempt")}
            print(f"[{e['node']:9s} {e['status']:5s}] {brief}")

    ok = True
    print("\n----- 结果 -----")
    if result.error:
        print("ERROR:", result.error)
        ok = False
    if result.sql:
        print("SQL:\n" + result.sql)
    print("columns:", result.columns)
    print("rows:", result.rows[:5])
    print("chart:", "有" if result.chart else "无")
    print("answer:", result.answer)
    print("usage:", result.usage.to_dict())

    if not result.rows:
        print("\n❌ 没有查到数据")
        ok = False
    if not result.answer:
        print("\n❌ 没有结论")
        ok = False
    if result.usage.calls == 0:
        print("\n❌ 没有 token 记账")
        ok = False

    if not args.keep:
        service.shutdown()
        for p in (DB, CKPT):
            try:
                p.unlink()
            except (FileNotFoundError, PermissionError):
                pass

    print("\n✅ 冒烟通过" if ok else "\n❌ 冒烟失败")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
