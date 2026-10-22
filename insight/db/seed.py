"""确定性生成电商演示库 data/ecommerce.db（6 表）。

设计目标（不是随手造假数据）：
1. **可复现**：单进程随机数全部来自 `random.Random(SEED)`，插入顺序固定，
   同一版本两次生成的文件 md5 完全一致。评测结果因此可对比、可复盘。
2. **业务自洽**：orders.total_amount 严格等于其明细行 quantity*unit_price 之和；
   returns 只能挂在真实存在的订单明细上，退款金额 = 退货件数 * 成交单价；
   退货日期晚于下单日期。评测 gold_sql 才敢直接对这些金额求和。
3. **有分析价值**：造出季节性（618/双11 高峰）、渠道结构变化（抖音上升、
   PC 官网下降）、高价值客户长尾，这样同比/环比、排名、留存类题目才不是空跑。

时间范围固定 2025-07-01 ~ 2026-09-30；按 CONTRACT「上个月 = 2026-09」。
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from insight.config import DB_PATH
from insight.db.schema import INDEXES, TABLE_ORDER, schema_sql

SEED = 42

# ---------------- 业务词表 ----------------
# 城市 -> 省份，权重近似一线/新一线电商体量，避免所有省份人数一样（那样排名题没意义）
CITY_PROVINCE: list[tuple[str, str, float]] = [
    ("上海", "上海", 9), ("北京", "北京", 9), ("广州", "广东", 7), ("深圳", "广东", 8),
    ("杭州", "浙江", 6), ("南京", "江苏", 4), ("苏州", "江苏", 3), ("成都", "四川", 5),
    ("重庆", "重庆", 3), ("武汉", "湖北", 3), ("长沙", "湖南", 2.5), ("西安", "陕西", 2.5),
    ("天津", "天津", 2.5), ("青岛", "山东", 2), ("济南", "山东", 1.8), ("郑州", "河南", 2),
    ("合肥", "安徽", 1.8), ("福州", "福建", 1.5), ("厦门", "福建", 1.2), ("昆明", "云南", 1.2),
    ("沈阳", "辽宁", 1.2), ("大连", "辽宁", 0.8), ("哈尔滨", "黑龙江", 0.8), ("长春", "吉林", 0.7),
    ("石家庄", "河北", 1.0), ("太原", "山西", 0.7), ("南昌", "江西", 0.8), ("贵阳", "贵州", 0.6),
    ("南宁", "广西", 0.7), ("兰州", "甘肃", 0.5),
]

MEMBER_LEVELS = ["普通会员", "银卡会员", "金卡会员", "钻石会员"]
MEMBER_WEIGHTS = [0.50, 0.27, 0.17, 0.06]

SURNAMES = list("王李张刘陈杨黄赵吴周徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾肖田董袁潘于蒋蔡余杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦")
MALE_GIVEN = ["伟", "强", "磊", "军", "洋", "勇", "杰", "涛", "明", "超", "浩然", "子轩", "宇航", "俊杰", "文博", "嘉豪", "天佑", "志远", "皓轩", "晨曦"]
FEMALE_GIVEN = ["芳", "娜", "敏", "静", "丽", "婷", "雪", "琳", "欣", "悦", "雨欣", "梦琪", "诗涵", "若曦", "子涵", "佳怡", "语嫣", "思雨", "可馨", "妍希"]

CATEGORY_META: dict[str, dict] = {
    "女装": {
        "price": (79, 899),
        "brands": ["初棉", "简梨", "MICO", "云上", "素白"],
        "nouns": ["连衣裙", "针织开衫", "阔腿裤", "风衣", "T恤", "半身裙", "衬衫", "卫衣", "大衣", "牛仔外套"],
    },
    "数码": {
        "price": (199, 6999),
        "brands": ["星辰", "极光", "声阔", "雷驰", "墨影"],
        "nouns": ["蓝牙耳机", "智能手表", "机械键盘", "移动电源", "摄像头", "平板电脑", "路由器", "蓝牙音箱", "快充头", "显示器"],
    },
    "家居": {
        "price": (39, 1999),
        "brands": ["木言", "栖息", "北欧时光", "暖屋", "宜家风"],
        "nouns": ["四件套", "乳胶枕", "收纳柜", "香薰机", "落地灯", "窗帘", "餐盘套装", "保温杯", "电热毯", "地垫"],
    },
    "美妆": {
        "price": (59, 799),
        "brands": ["芙洛", "珂润", "花西", "研色", "初本"],
        "nouns": ["精华液", "面霜", "口红", "粉底液", "面膜", "防晒霜", "洁面乳", "眼影盘", "香水", "身体乳"],
    },
    "食品": {
        "price": (9, 199),
        "brands": ["食光", "稻香", "每日鲜", "原野", "麦小盒"],
        "nouns": ["坚果礼盒", "咖啡豆", "茶叶", "牛肉干", "蜂蜜", "燕麦片", "巧克力", "饼干", "水果干", "自热火锅"],
    },
    "母婴": {
        "price": (19, 599),
        "brands": ["贝乐", "小鹿", "萌宝", "亲亲", "安睡"],
        "nouns": ["纸尿裤", "婴儿湿巾", "宝宝辅食", "儿童餐具", "婴儿推车", "安全座椅", "学步鞋", "儿童牙膏", "爬行垫", "吸奶器"],
    },
    "运动": {
        "price": (49, 1299),
        "brands": ["跃动", "风行者", "迈途", "山野", "拓界"],
        "nouns": ["跑步鞋", "瑜伽垫", "运动套装", "篮球", "健身手套", "登山包", "骑行头盔", "泳镜", "跳绳", "速干T恤"],
    },
    "图书": {
        "price": (15, 149),
        "brands": ["知微", "南方出版", "博雅", "中信", "三联"],
        "nouns": ["小说", "历史读物", "儿童绘本", "经管书", "心理学", "科普", "菜谱", "传记", "艺术画册", "工具书"],
    },
}
PRODUCT_VARIANTS = ["经典款", "轻奢款", "青春款", "升级款", "限定款", "家用款", "便携款", "旗舰款", "入门款", "尊享款", "高配款", "简约款", "加厚款", "超值款", "2026新款"]

SALES_CHANNELS = ["App", "小程序", "PC官网", "天猫", "京东", "抖音"]
# 渠道结构随年份变化：抖音/小程序上升、PC 官网下降。同比题才有真实差异可讲。
CHANNEL_WEIGHTS_BY_YEAR: dict[int, list[float]] = {
    2025: [0.22, 0.18, 0.15, 0.18, 0.15, 0.12],
    2026: [0.24, 0.20, 0.08, 0.16, 0.14, 0.18],
}

CAMPAIGN_CHANNELS = ["搜索广告", "信息流", "直播", "短视频", "私域", "联盟"]
RETURN_REASONS = ["七天无理由", "尺码不合适", "质量问题", "与描述不符", "物流破损"]
RETURN_REASON_WEIGHTS = [0.40, 0.25, 0.15, 0.12, 0.08]
CANCEL_RATE = 0.08

# 月份权重：618、双 11 拔高，春节（2026-02）回落。合计约 15.3，用于把 2 万单摊到 15 个月。
MONTH_WEIGHTS: list[float] = [
    0.80, 0.85, 0.90, 0.95, 1.60, 1.20,  # 2025-07 ~ 2025-12
    0.90, 0.80, 0.95, 1.00, 1.05, 1.50,  # 2026-01 ~ 2026-06
    1.10, 1.15, 1.25,                    # 2026-07 ~ 2026-09
]

# 促销月额外折扣，让「大促月 GMV 高但客单价未必高」这类观察成立
PROMO_MONTHS = {"2025-11", "2026-06"}


def _month_list() -> list[str]:
    months: list[str] = []
    y, m = 2025, 7
    while (y, m) <= (2026, 9):
        months.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return months


def _rand_date(rng: random.Random, start: date, end: date) -> str:
    return (start + timedelta(days=rng.randint(0, (end - start).days))).isoformat()


class _Picker:
    """累积权重 + 线性探测的加权抽样。

    为什么不直接 random.choices：订单 / 明细要抽 7 万多次，每次都重算累积权重是 O(n)；
    这里只算一次然后 bisect，稳定且快。
    """

    def __init__(self, rng: random.Random, population: list, weights: list[float]):
        self.rng = rng
        self.population = population
        self.cum: list[float] = []
        total = 0.0
        for w in weights:
            total += w
            self.cum.append(total)

    def pick(self) -> object:
        x = self.rng.random() * self.cum[-1]
        return self.population[bisect.bisect_right(self.cum, x)]


def _build_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(schema_sql())
    for idx in INDEXES:
        conn.execute(idx)


def generate(db_path: Path = DB_PATH, seed: int = SEED) -> dict[str, int]:
    """生成整库并落盘，返回各表行数。已存在则覆盖（评测要求可重复生成）。"""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    rng = random.Random(seed)
    conn = sqlite3.connect(db_path)
    try:
        _build_schema(conn)

        # ---------- campaigns（24 个投放计划，按季度铺满全周期） ----------
        quarters = [
            ("2025-07-01", "2025-09-30"),
            ("2025-10-01", "2025-12-31"),
            ("2026-01-01", "2026-03-31"),
            ("2026-04-01", "2026-06-30"),
            ("2026-07-01", "2026-09-30"),
        ]
        per_quarter = [5, 5, 5, 5, 4]  # 合计 24，每个投放渠道恰好 4 个计划
        campaigns: list[tuple] = []
        cid = 0
        for (q_start, q_end), n in zip(quarters, per_quarter):
            qs = date.fromisoformat(q_start)
            qe = date.fromisoformat(q_end)
            for _ in range(n):
                cid += 1
                channel = CAMPAIGN_CHANNELS[(cid - 1) % len(CAMPAIGN_CHANNELS)]
                category = list(CATEGORY_META)[(cid - 1) % len(CATEGORY_META)]
                start = qs + timedelta(days=rng.randint(0, 20))
                end = min(start + timedelta(days=rng.randint(35, 70)), qe)
                budget = float(round(rng.uniform(50_000, 500_000), -3))
                campaigns.append((
                    cid,
                    f"{qs.year}Q{(qs.month - 1) // 3 + 1}{channel}增长计划-{cid:02d}",
                    channel,
                    start.isoformat(),
                    end.isoformat(),
                    budget,
                    category,
                ))
        conn.executemany(
            "INSERT INTO campaigns (campaign_id, campaign_name, channel, start_date, end_date, budget, target_category) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            campaigns,
        )

        # ---------- products（8 品类 * 15 = 120） ----------
        products: list[tuple] = []
        product_meta: list[dict] = []  # 生成订单时按销量热度抽样
        pid = 0
        for category, meta in CATEGORY_META.items():
            lo, hi = meta["price"]
            for i in range(15):
                pid += 1
                brand = meta["brands"][i % len(meta["brands"])]
                noun = meta["nouns"][(i * 3 + len(category)) % len(meta["nouns"])]
                price = float(round(rng.uniform(lo, hi), 2))
                cost = float(round(price * rng.uniform(0.40, 0.72), 2))
                # 90% 在售：留少量下架商品，让「在售商品数」和「商品总数」的题有区分度
                status = "在售" if rng.random() < 0.90 else "下架"
                launch = _rand_date(rng, date(2024, 1, 1), date(2026, 6, 30))
                products.append((pid, f"{brand}{noun}·{PRODUCT_VARIANTS[i]}", category, brand, price, cost, launch, status))
                # 商品热度差异（长尾）：少数爆款贡献大部分销量，排名题才有层次
                product_meta.append({"id": pid, "price": price, "launch": launch, "heat": rng.lognormvariate(0, 0.85)})
        conn.executemany(
            "INSERT INTO products (product_id, product_name, category, brand, price, cost, launch_date, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            products,
        )

        # ---------- customers（2000） ----------
        customers: list[tuple] = []
        customer_ids: list[int] = []
        city_picker = _Picker(rng, [(c, p) for c, p, _ in CITY_PROVINCE], [w for _, _, w in CITY_PROVINCE])
        level_picker = _Picker(rng, MEMBER_LEVELS, MEMBER_WEIGHTS)
        for customer_id in range(1, 2001):
            gender = "女" if rng.random() < 0.55 else "男"
            given = rng.choice(FEMALE_GIVEN if gender == "女" else MALE_GIVEN)
            name = rng.choice(SURNAMES) + given
            age = rng.randint(18, 65)
            city, province = city_picker.pick()
            # 注册日期偏后期：平台在增长，越晚注册的客户越多
            register = _rand_date(rng, date(2024, 1, 1), date(2026, 9, 30))
            customers.append((customer_id, name, gender, age, city, province, register, level_picker.pick()))
            customer_ids.append(customer_id)
        conn.executemany(
            "INSERT INTO customers (customer_id, name, gender, age, city, province, register_date, member_level) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            customers,
        )

        # 客户活跃度长尾：复购率高的客户集中在少数人，Top N 客户 / 新老客题目才有意义
        customer_heat = [rng.lognormvariate(0, 0.9) for _ in customer_ids]
        product_picker = _Picker(rng, product_meta, [p["heat"] for p in product_meta])

        months = _month_list()
        month_picker = _Picker(rng, months, MONTH_WEIGHTS)
        # 客户只能在自己注册之后下单：按「月初」切一批可下单客户，避免出现注册前消费的穿帮数据。
        # 按月预建 picker（15 个），下单时 O(log n) 抽样，不会退化成 2 万次全表过滤。
        reg_dates = [c[6] for c in customers]
        month_customer_pickers: dict[str, _Picker] = {}
        for month in months:
            cutoff = f"{month}-01"
            idxs = [i for i, reg in enumerate(reg_dates) if reg <= cutoff]
            month_customer_pickers[month] = _Picker(
                rng, [customer_ids[i] for i in idxs], [customer_heat[i] for i in idxs]
            )

        years = list(range(2025, 2027))
        year_picker = {y: _Picker(rng, SALES_CHANNELS, CHANNEL_WEIGHTS_BY_YEAR[y]) for y in years}
        qty_picker = _Picker(rng, [1, 2, 3], [0.72, 0.21, 0.07])
        item_count_picker = _Picker(rng, [1, 2, 3, 4, 5], [0.22, 0.30, 0.24, 0.16, 0.08])
        discount_picker = _Picker(rng, [1.0, 0.95, 0.90, 0.85, 0.80], [0.45, 0.25, 0.15, 0.10, 0.05])

        # campaign 归因抽样：按「在当天在投」的计划挑，保证归因订单不是凭空编的
        campaigns_by_date: dict[str, list[int]] = {}
        for c in campaigns:
            cur = date.fromisoformat(c[3])
            end = date.fromisoformat(c[4])
            while cur <= end:
                campaigns_by_date.setdefault(cur.isoformat(), []).append(c[0])
                cur += timedelta(days=1)

        orders: list[dict] = []
        for order_id in range(1, 20001):
            month = month_picker.pick()
            y, m = (int(x) for x in month.split("-"))
            day = rng.randint(1, _days_in_month(y, m))
            order_date = f"{month}-{day:02d}"
            channel = year_picker[y].pick()
            status = "已取消" if rng.random() < CANCEL_RATE else "已完成"
            campaign_id = None
            if rng.random() < 0.22:
                active = campaigns_by_date.get(order_date)
                if active:
                    campaign_id = rng.choice(active)
            orders.append({
                "order_id": order_id,
                "customer_id": month_customer_pickers[month].pick(),
                "order_date": order_date,
                "channel": channel,
                "status": status,
                "campaign_id": campaign_id,
                "items": [],
            })

        # ---------- order_items（~5 万行） ----------
        items: list[tuple] = []
        item_id = 0
        for order in orders:
            chosen: set[int] = set()
            n_items = item_count_picker.pick()
            promo = order["order_date"][:7] in PROMO_MONTHS
            for _ in range(n_items):
                for _attempt in range(30):  # 加权不放回抽样；跳过已选中的和下单时还没上架的商品
                    p = product_picker.pick()
                    if p["id"] in chosen or p["launch"] > order["order_date"]:
                        continue
                    chosen.add(p["id"])
                    break
                else:
                    continue
                item_id += 1
                quantity = qty_picker.pick()
                factor = discount_picker.pick()
                if promo:
                    factor = round(factor * 0.95, 4)  # 大促再让 5 个点
                unit_price = float(round(p["price"] * factor, 2))
                amount = round(quantity * unit_price, 2)
                items.append((item_id, order["order_id"], p["id"], quantity, unit_price))
                order["items"].append(amount)
        conn.executemany(
            "INSERT INTO order_items (item_id, order_id, product_id, quantity, unit_price) VALUES (?, ?, ?, ?, ?)",
            items,
        )

        # orders 的 total_amount 必须与明细一致：不是近似，是精确到分
        order_rows = []
        for order in orders:
            total = round(sum(order["items"]), 2)
            order_rows.append((
                order["order_id"], order["customer_id"], order["order_date"],
                order["channel"], order["status"], total, order["campaign_id"],
            ))
        conn.executemany(
            "INSERT INTO orders (order_id, customer_id, order_date, channel, status, total_amount, campaign_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            order_rows,
        )

        # ---------- returns（2500） ----------
        # 只从 2026-08-31 之前完成的订单里抽，保证 return_date <= 下单+30 天 <= 2026-09-30
        eligible: list[tuple[int, int, int, int, float, str]] = []  # item_id, order_id, product_id, qty, unit_price, order_date
        order_meta = {o["order_id"]: o for o in orders}
        for item_id_, order_id_, product_id_, quantity_, unit_price_ in items:
            o = order_meta[order_id_]
            if o["status"] == "已完成" and o["order_date"] <= "2026-08-31":
                eligible.append((item_id_, order_id_, product_id_, quantity_, unit_price_, o["order_date"]))

        sampled = rng.sample(eligible, 2500)
        reason_picker = _Picker(rng, RETURN_REASONS, RETURN_REASON_WEIGHTS)
        returns: list[tuple] = []
        for return_id, (item_id_, order_id_, product_id_, quantity_, unit_price_, order_date_) in enumerate(sampled, start=1):
            qty = quantity_
            # 部分退：多件商品里只退一部分，退货率计算才不是简单复制销量
            if qty > 1 and rng.random() < 0.35:
                qty = rng.randint(1, qty - 1)
            refund = round(qty * unit_price_, 2)
            return_date = date.fromisoformat(order_date_) + timedelta(days=rng.randint(1, 30))
            returns.append((
                return_id, order_id_, item_id_, product_id_, qty, refund,
                return_date.isoformat(),
                reason_picker.pick(),
                "已完成" if rng.random() < 0.85 else "处理中",
            ))
        conn.executemany(
            "INSERT INTO returns (return_id, order_id, item_id, product_id, quantity, refund_amount, return_date, reason, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            returns,
        )

        conn.commit()
        _assert_consistent(conn)
        counts = {
            name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in TABLE_ORDER
        }
    finally:
        conn.close()
    return counts


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (date(year, month + 1, 1) - date(year, month, 1)).days


def _assert_consistent(conn: sqlite3.Connection) -> None:
    """生成后立刻自检，失败了就别把脏库留给评测。

    这些断言同时也是 schema.md 里业务口径的可执行版本。
    """
    bad = conn.execute(
        """SELECT COUNT(*) FROM orders o
           WHERE ABS(o.total_amount - COALESCE((
               SELECT SUM(i.quantity * i.unit_price) FROM order_items i WHERE i.order_id = o.order_id
           ), 0)) > 0.005"""
    ).fetchone()[0]
    assert bad == 0, f"{bad} 个订单的 total_amount 与明细金额不一致"

    orphan = conn.execute(
        "SELECT COUNT(*) FROM returns r LEFT JOIN orders o ON o.order_id = r.order_id WHERE o.order_id IS NULL"
    ).fetchone()[0]
    assert orphan == 0, f"{orphan} 条退货找不到对应订单"

    mismatch = conn.execute(
        """SELECT COUNT(*) FROM returns r
           JOIN order_items i ON i.item_id = r.item_id
           WHERE i.order_id != r.order_id OR i.product_id != r.product_id OR r.quantity > i.quantity"""
    ).fetchone()[0]
    assert mismatch == 0, f"{mismatch} 条退货与订单明细对不上"

    bad_date = conn.execute(
        """SELECT COUNT(*) FROM returns r JOIN orders o ON o.order_id = r.order_id
           WHERE r.return_date < o.order_date"""
    ).fetchone()[0]
    assert bad_date == 0, f"{bad_date} 条退货日期早于下单日期"

    out_of_range = conn.execute(
        "SELECT COUNT(*) FROM orders WHERE order_date < '2025-07-01' OR order_date > '2026-09-30'"
    ).fetchone()[0]
    assert out_of_range == 0, f"{out_of_range} 个订单日期越界"

    early_order = conn.execute(
        """SELECT COUNT(*) FROM orders o JOIN customers c USING(customer_id)
           WHERE o.order_date < c.register_date"""
    ).fetchone()[0]
    assert early_order == 0, f"{early_order} 个订单早于客户注册日期"

    early_launch = conn.execute(
        """SELECT COUNT(*) FROM order_items i
           JOIN orders o USING(order_id) JOIN products p USING(product_id)
           WHERE o.order_date < p.launch_date"""
    ).fetchone()[0]
    assert early_launch == 0, f"{early_launch} 条明细早于商品上架日期"

    dup = conn.execute(
        "SELECT COUNT(*) FROM (SELECT item_id FROM returns GROUP BY item_id HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    assert dup == 0, f"{dup} 条明细被重复退货"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="确定性生成电商演示库")
    parser.add_argument("--db", default=str(DB_PATH), help="输出 sqlite 路径")
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args(argv)

    counts = generate(Path(args.db), args.seed)
    digest = hashlib.md5(Path(args.db).read_bytes()).hexdigest()
    print(f"生成完成：{args.db}")
    for name in TABLE_ORDER:
        print(f"  {name:<12} {counts[name]:>6} 行")
    print(f"md5: {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
