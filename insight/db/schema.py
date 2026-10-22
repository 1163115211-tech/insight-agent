"""电商库的表结构定义。

为什么单独放一个 schema.py，而不是在 seed.py 里用一长串 execute：
1. baseline 评测需要把「全库 DDL」原样塞进 prompt，它不该为了拿 DDL 去连数据库，
   更不该 import 生成逻辑；一份常量最省事也最公平（A/B 两边看到的是同一份结构）。
2. 评测题目、报告、Web 演示都要引用表/列语义，集中一处避免三处漂移。

DDL 里刻意保留 `--` 行内注释：baseline 是「只看 schema 一次成 SQL」的对照，
注释里写清枚举值和金额口径，等于给对照组一份真实的表注释（生产里 data catalog
通常也会提供），避免把「猜枚举值」这种和 SQL 能力无关的噪声算进基线。
"""
from __future__ import annotations

# 6 张表的建表语句。字段顺序、类型即对外契约，seed 与评测都依赖它。
DDL: dict[str, str] = {
    "customers": """
CREATE TABLE customers (
    customer_id   INTEGER PRIMARY KEY,              -- 客户 ID
    name          TEXT    NOT NULL,                 -- 客户姓名
    gender        TEXT    NOT NULL,                 -- 性别：男 / 女
    age           INTEGER NOT NULL,                 -- 年龄（18~65）
    city          TEXT    NOT NULL,                 -- 所在城市（中文，如 上海）
    province      TEXT    NOT NULL,                 -- 所在省份（中文，如 上海 / 广东）
    register_date TEXT    NOT NULL,                 -- 注册日期 YYYY-MM-DD
    member_level  TEXT    NOT NULL                  -- 会员等级：普通会员 / 银卡会员 / 金卡会员 / 钻石会员
)""",
    "products": """
CREATE TABLE products (
    product_id    INTEGER PRIMARY KEY,              -- 商品 ID
    product_name  TEXT    NOT NULL,                 -- 商品名称
    category      TEXT    NOT NULL,                 -- 品类：女装 / 数码 / 家居 / 美妆 / 食品 / 母婴 / 运动 / 图书
    brand         TEXT    NOT NULL,                 -- 品牌
    price         REAL    NOT NULL,                 -- 标价（元）
    cost          REAL    NOT NULL,                 -- 成本价（元），用于毛利估算
    launch_date   TEXT    NOT NULL,                 -- 上架日期 YYYY-MM-DD
    status        TEXT    NOT NULL                  -- 状态：在售 / 下架
)""",
    "campaigns": """
CREATE TABLE campaigns (
    campaign_id     INTEGER PRIMARY KEY,            -- 投放计划 ID
    campaign_name   TEXT    NOT NULL,               -- 计划名称
    channel         TEXT    NOT NULL,               -- 投放渠道：搜索广告 / 信息流 / 直播 / 短视频 / 私域 / 联盟
    start_date      TEXT    NOT NULL,               -- 开始日期 YYYY-MM-DD
    end_date        TEXT    NOT NULL,               -- 结束日期 YYYY-MM-DD
    budget          REAL    NOT NULL,               -- 预算（元）
    target_category TEXT    NOT NULL                -- 目标品类，同 products.category
)""",
    "orders": """
CREATE TABLE orders (
    order_id     INTEGER PRIMARY KEY,               -- 订单 ID
    customer_id  INTEGER NOT NULL REFERENCES customers(customer_id),
    order_date   TEXT    NOT NULL,                  -- 下单日期 YYYY-MM-DD
    channel      TEXT    NOT NULL,                  -- 销售渠道：App / 小程序 / PC官网 / 天猫 / 京东 / 抖音
    status       TEXT    NOT NULL,                  -- 订单状态：已完成 / 已取消
    total_amount REAL    NOT NULL,                  -- 订单成交金额（元）= 该订单各商品行 quantity*unit_price 之和
    campaign_id  INTEGER REFERENCES campaigns(campaign_id)  -- 归因投放计划，NULL 表示自然流量
)""",
    "order_items": """
CREATE TABLE order_items (
    item_id    INTEGER PRIMARY KEY,                 -- 明细行 ID
    order_id   INTEGER NOT NULL REFERENCES orders(order_id),
    product_id INTEGER NOT NULL REFERENCES products(product_id),
    quantity   INTEGER NOT NULL,                    -- 购买件数
    unit_price REAL    NOT NULL                     -- 成交单价（元），可能低于标价
)""",
    "returns": """
CREATE TABLE returns (
    return_id     INTEGER PRIMARY KEY,              -- 退货单 ID
    order_id      INTEGER NOT NULL REFERENCES orders(order_id),
    item_id       INTEGER NOT NULL REFERENCES order_items(item_id),
    product_id    INTEGER NOT NULL REFERENCES products(product_id),
    quantity      INTEGER NOT NULL,                 -- 退货件数（不超过该明细行购买件数）
    refund_amount REAL    NOT NULL,                 -- 退款金额（元）= 退货件数 * 该行成交单价
    return_date   TEXT    NOT NULL,                 -- 退货日期 YYYY-MM-DD
    reason        TEXT    NOT NULL,                 -- 退货原因：七天无理由 / 尺码不合适 / 质量问题 / 与描述不符 / 物流破损
    status        TEXT    NOT NULL                  -- 处理状态：已完成 / 处理中
)""",
}

# 建表顺序：被引用的表必须先建（SQLite 默认不强制 FK，但别依赖这一点）。
TABLE_ORDER: list[str] = ["customers", "products", "campaigns", "orders", "order_items", "returns"]

# 索引：评测题目大量按月份、品类、商品过滤，缺索引时 5 万行的查询也能跑，
# 但加上能显著压低 execute 节点耗时，让延迟数据更接近真实生产。
INDEXES: list[str] = [
    "CREATE INDEX idx_orders_date ON orders(order_date)",
    "CREATE INDEX idx_orders_customer ON orders(customer_id)",
    "CREATE INDEX idx_orders_channel ON orders(channel)",
    "CREATE INDEX idx_orders_campaign ON orders(campaign_id)",
    "CREATE INDEX idx_items_order ON order_items(order_id)",
    "CREATE INDEX idx_items_product ON order_items(product_id)",
    "CREATE INDEX idx_returns_order ON returns(order_id)",
    "CREATE INDEX idx_returns_product ON returns(product_id)",
    "CREATE INDEX idx_returns_date ON returns(return_date)",
    "CREATE INDEX idx_products_category ON products(category)",
]


def schema_sql() -> str:
    """按依赖顺序拼出完整 DDL，seed 建库和 baseline 拼 prompt 共用。"""
    return ";\n\n".join(DDL[name].strip() for name in TABLE_ORDER) + ";"


def full_ddl() -> str:
    """给 LLM 看的全库 schema 文本：建表 + 索引。

    索引对写 SQL 没帮助，但对「哪些列有索引、能怎么过滤」有提示意义，
    且真实 schema 导出也会带索引，所以一起给。
    """
    return schema_sql() + "\n\n" + "\n".join(INDEXES)
