# Insight Agent 评测报告（graph vs baseline）

> 数据集：`insight/eval/questions.jsonl`（52 题）· 数据库：`data/ecommerce.db` · 逐题记录：`docs/eval/graph.jsonl` / `docs/eval/baseline.jsonl` · 汇总：`docs/eval/summary.json`
> 本报告所有数字来自真实 LLM 调用 + 真实 SQL 执行，可用附录命令一键复现。

## 0. TL;DR

| 结论 | 数字 |
|---|---|
| 执行准确率（主指标） | **graph 84.6%（44/52） vs baseline 59.6%（31/52），+25.0 个百分点** |
| 关键列命中率（诊断，非主指标） | graph 90.4%（47/52） vs baseline 78.8%（41/52） |
| SQL 合法率 | 两边都是 **100%**（52/52 可执行，无语法/字段/超时错误） |
| 难度分层 | graph：easy 100% / medium 84.6% / hard 69.2%；baseline：92.3% / 50.0% / 46.2% |
| 成本与延迟代价 | 单题平均 token ×3.3、成本 ×4.1、P50 延迟 ×7.4（¥0.0154 vs ¥0.0038，7.4s vs 1.0s） |
| 一句话 | Agent 编排的价值集中在多表 join / 比率 / 复杂综合题；简单单表题两边都行。graph 的 8 个失败里 3 个只是"多返回列"，5 个是聚合粒度/扇出、窗口基数、口径范围这类真实推理错误。 |

**本轮评测没有触发自修复**（`avg_attempts=1.0`、`repaired_runs=0`）：模型生成的 SQL 全部合法，没有进入 repair 分支。因此本报告**不宣称**"自修复提升了准确率"——那需要专门的失败 SQL 压力集来验证，见 §5.1。

## 1. 评测方法与口径

### 1.1 数据集

| 项 | 说明 |
|---|---|
| 数据库 | SQLite，6 表：`customers` 2,000 / `products` 120 / `campaigns` 24 / `orders` 20,000 / `order_items` 51,378 / `returns` 2,500 |
| 时间范围 | 2025-07-01 ~ 2026-09-30；"上月" = 2026-09，基准日 2026-10 |
| 生成方式 | `insight/db/seed.py`，`random.Random(42)` 确定性生成；两次生成 md5 一致（`b75d83ab…`） |
| 自洽校验 | 订单金额 = 明细行金额之和（精确到分）；退货单只挂在真实订单明细上，退款 = 件数×成交单价；订单日期 ≥ 客户注册日、≥ 商品上架日；退货日期晚于下单日 |
| 业务口径 | 见 `insight/db/schema.md`：GMV、客单价、退货率、同比/环比、ROI、新客定义均写死，全库统一 |

题目分布（52 题，全部中文业务口吻，题面不含表名/字段名）：

| 题型 | 数量 | 说明 |
|---|---:|---|
| 单表聚合 | 10 | 计数、均值、分组、TopN |
| 多表 join | 15 | 明细×商品×客户×活动，多指标对齐 |
| 时间序列/同比环比 | 8 | 月度趋势、同比、环比、季度、日粒度 |
| 窗口函数 | 6 | 排名、占比、累计、分组 TopN |
| 比率类 | 5 | 退货率、客单价、ROI |
| 复杂综合 | 8 | 多指标 + 同比/环比 + 归因 |
| **难度** | | easy 13 / medium 26 / hard 13 |

52 条 gold_sql 均逐条实跑验证：非空、行数 1~50、列数 ≤8，且每题结果集符合题面要求的列（详见 §1.5 的 gold 审计）。

### 1.2 A/B 设计（只改链路，不改模型）

| 维度 | graph | baseline |
|---|---|---|
| 模型 | `deepseek-chat`，temperature 0 | **完全相同** |
| 上下文 | 通过 tools 探索表结构，再计划/写 SQL/校验/执行/修复/出图/总结 | 一次给全 6 表 DDL + 业务口径 + 当前日期 + 问题 |
| 机会 | 多轮、可探查、失败可重试（`MAX_RETRIES=3`） | **一次**：不探查、不重试、不修复 |
| 执行 | 只读 SQL 沙箱 + 静态校验 | 只读连接直接执行一次 |
| 会话 | 每题独立 `service.run(question)`，不传 thread_id | 每题独立调用 |

公平性说明：
- 两边看到**同一份 DDL、同一份业务口径、同一个当前日期（2026-10-22，明确"上月"= 2026-09）**，避免 baseline 因不知道今天是几号而误解题意；
- baseline 的 prompt 里带全库 DDL 注释（枚举值、金额口径），等价于生产里的 data catalog，不是故意削弱它；
- 两边使用同一份 gold、同一个结果集比较器；
- baseline 的"弱"只来自**没有探查/修复/多轮**这一条链路差异，这正是要被测量的是 Agent 编排增益。

### 1.3 判分定义

| 指标 | 定义 |
|---|---|
| **执行准确率（主指标）** | 生成 SQL 执行成功后，结果集与 gold **完全一致**：列数、行数相同；列顺序不敏感、行顺序不敏感；数值容差 `1e-2`；NULL 只匹配 NULL；列名不参与比较。多返回一列即判错。 |
| **关键列命中率（诊断）** | gold 的每一列值都能在预测结果中按行匹配到，**忽略多余列**。仅用于解释"是格式问题还是算错了"，不替代主指标。 |
| SQL 合法率 | 生成 SQL 在真实库上执行成功（无语法/字段/超时错误）的比例 |
| 自修复成功率 | 尝试次数 ≥2 的题中，最终 SQL 执行成功的比例；本轮无此类样本，记 N/A |
| 平均尝试次数 | 每题 SQL 生成/执行尝试次数均值 |
| 延迟 | 单题端到端墙钟时间；P50/P95 用最近秩（不插值） |
| 成本 | 按 `insight/config.py` 价目（输入 ¥2/M、输出 ¥8/M）估算 |

### 1.4 运行环境

- Python：`C:\Users\11631\anaconda3\envs\langchain1.2\python.exe`（langchain 1.2.12 / langgraph 1.1.2）
- 模型：DeepSeek `deepseek-chat`，temperature 0，max_tokens 2048，单次超时 60s
- SQL 沙箱上限：1000 行 / 5000ms
- 单机、串行执行（52 题 graph 总耗时 383s，baseline 55s）
- 最终一轮 A/B 的模型成本：graph ¥0.8032 + baseline ¥0.2001 ≈ **¥1.00**

### 1.5 审计说明：评测驱动修复 + gold 修正后重算

评测本身抓出了 3 类真实问题，这里完整披露，避免"为了好看改数字"的误解：

1. **第一轮（52×2）**：graph 24/52（46.2%）< baseline 29/52（55.8%）。定位到 Agent 侧 `sandbox` 的 CTE 正则 bug：`WITH months(ym) AS (...)` 这类**带列清单的 CTE** 提取不到名字，被误判"引用了未知表"，3 题（q026/q032/q036）修复耗尽后返回空结果。该 bug 已由 Lead 修复并补回归测试。
2. **修正后第二轮**：graph 35/52（67.3%）vs baseline 33/52（63.5%）。发现**数据集自身问题**：9 题 gold 多返回了题面没要求的辅助列（例如只问"同比增长多少"，gold 却返回本期绝对值/去年同期/增速三列）。已把 gold 收敛到题面要求的列。
3. **第三轮**：gold 对齐后 raw 结果 graph 42/52（80.8%）vs baseline 30/52（57.7%）。随后审查发现 **2 个 gold 自身 bug**：
   - q022：`AVG(price)` 写在 join 明细之后，被销量加权（应先在商品表算平均标价）；
   - q030：题面问"退货单数"，gold 却 `COUNT(DISTINCT order_id)` 去重到了订单。
   修正这 2 条 gold 后，用**已落盘的真实模型预测结果离线重算**（没有重新调用模型），得到本报告最终数字：**graph 44/52（84.6%） vs baseline 31/52（59.6%）**。
4. 因此：`docs/eval/run.log` 是第三轮的原始日志；`graph.jsonl` / `baseline.jsonl` / `summary.json` 是最终（gold 修正后）重算结果；模型输出未因重算而改变。

## 2. 主结果

| 指标 | graph | baseline | 差异 |
|---|---:|---:|---:|
| 样本数 | 52 | 52 | — |
| **执行准确率（主指标）** | **84.6%（44/52）** | **59.6%（31/52）** | **+25.0 pp** |
| 关键列命中率（诊断） | 90.4%（47/52） | 78.8%（41/52） | +11.5 pp |
| SQL 合法率 | 100.0%（52/52） | 100.0%（52/52） | 0 |
| 自修复成功率 | N/A（0 次修复） | N/A | — |
| 平均尝试次数 | 1.00 | 1.00 | 0 |
| 平均 prompt token | 4,132 | 1,370 | ×3.0 |
| 平均 completion token | 898 | 139 | ×6.5 |
| 平均总 token | 5,030 | 1,508 | ×3.3 |
| 平均成本（¥/题） | 0.0154 | 0.0038 | ×4.1 |
| P50 延迟 | 7,377 ms | 999 ms | ×7.4 |
| P95 延迟 | 9,177 ms | 1,619 ms | ×5.7 |
| 总成本（52 题） | ¥0.8032 | ¥0.2001 | — |

> 主指标看严格执行准确率；关键列命中率只用于归因。两者差距说明 graph 有 3 题是"算对了但多返回列"，baseline 有 10 题同类问题。

## 3. 分维度结果

### 3.1 按难度

| 难度 | 题数 | graph | baseline | 差值 |
|---|---:|---:|---:|---:|
| easy | 13 | 100.0%（13/13） | 92.3%（12/13） | +7.7 pp |
| medium | 26 | 84.6%（22/26） | 50.0%（13/26） | +34.6 pp |
| hard | 13 | 69.2%（9/13） | 46.2%（6/13） | +23.0 pp |

解读：差距随复杂度放大。简单单表题 baseline 也能靠一次生成做对（只错 q007 比率题）；到 medium/hard，**没有探查与多表口径校验**的 baseline 明显掉队（medium 直接掉到 50%）。

### 3.2 按题型

| 题型 | 题数 | graph | baseline | 差值 |
|---|---:|---:|---:|---:|
| 单表聚合 | 10 | 100.0% | 90.0% | +10.0 pp |
| 多表 join | 15 | 86.7% | 66.7% | +20.0 pp |
| 时间序列/同比环比 | 8 | 75.0% | 50.0% | +25.0 pp |
| 窗口函数 | 6 | 100.0% | 83.3% | +16.7 pp |
| 比率类 | 5 | 60.0% | 0.0% | +60.0 pp |
| 复杂综合 | 8 | 75.0% | 37.5% | +37.5 pp |

补充事实：
- graph 做对而 baseline 做错的题有 **13 道**；**baseline 做对而 graph 做错的题为 0 道**（graph 的 8 个失败 baseline 也全错）。增益不是"此消彼长"。
- 比率类 baseline 0% 需冷静解读，见 §5.3：其中 2 题只是多列/单位格式，3 题是真实的 join 扇出与去重错误，不宜简单归因成"完全不会算比率"。
- graph 的短板集中在 **时间序列（75%）与比率类（60%）**，失败原因都是聚合粒度/窗口基数/去重口径，而不是 SQL 语法。

## 4. 失败分析

### 4.1 总览

| 配置 | 失败数 | SQL 执行失败 | 超时/解析失败 | 结果不对（能跑但答案不符） |
|---|---:|---:|---:|---:|
| graph | 8 | 0 | 0 | 8（3 多列 + 4 口径/扇出 + 1 行数爆炸） |
| baseline | 21 | 0 | 0 | 21（10 多列 + 11 口径/逻辑） |

graph 的 8 个失败中，**5 个是真实推理错误**（q021/q029/q031/q041/q046），另外 3 个（q017/q043/q047）是"值全对、多返回了上下文列"，属于输出列纪律问题。以下逐条给出问题、gold SQL、模型 SQL 与差异点。

### 4.2 graph 失败逐条

#### ① q021 年龄段客户数：分母范围错了（口径分歧）

**问题**：2026 年 9 月不同年龄段的客户各有多少人、贡献多少 GMV？

```sql
-- gold：客户数 = 9 月有已完成订单的客户（COUNT(DISTINCT o.customer_id)）
SELECT CASE WHEN c.age BETWEEN 18 AND 25 THEN '18-25岁'
            WHEN c.age BETWEEN 26 AND 35 THEN '26-35岁'
            WHEN c.age BETWEEN 36 AND 45 THEN '36-45岁'
            ELSE '46岁及以上' END AS age_group,
       COUNT(DISTINCT o.customer_id) AS customers,
       ROUND(SUM(o.total_amount), 2) AS gmv
FROM orders o JOIN customers c ON c.customer_id = o.customer_id
WHERE o.status = '已完成' AND o.order_date LIKE '2026-09%'
GROUP BY age_group ORDER BY age_group;
```

```sql
-- 模型：从 customers 全表分桶后 LEFT JOIN 9 月订单，客户数 = 全部注册客户
WITH age_bucket AS (SELECT customer_id, CASE ... END AS "年龄段" FROM customers),
     cust_cnt AS (SELECT "年龄段", COUNT(DISTINCT customer_id) AS "客户数" FROM age_bucket GROUP BY "年龄段"),
     gmv AS (SELECT ab."年龄段", SUM(o.total_amount) AS "GMV"
             FROM orders o JOIN age_bucket ab ON ab.customer_id = o.customer_id
             WHERE o.order_date LIKE '2026-09%' AND o.status = '已完成' GROUP BY ab."年龄段")
SELECT c."年龄段", c."客户数", ROUND(COALESCE(g."GMV",0),2) AS "GMV" FROM cust_cnt c LEFT JOIN gmv g ON ...
```

**差异**：GMV 完全正确，但客户数变成"该年龄段全部注册客户"：18-25 岁 341 vs gold 141，46+ 岁 831 vs 341。错在把维度表当事实表——问的是"9 月有购买的客户"，不是"注册客户"。这是典型的**指标分母范围**错误。

#### ② q029 月度环比：LAG 的基数月被过滤掉了（口径分歧）

**问题**：2026 年 4 月到 9 月，每个月的已完成订单量和环比增长率？

```sql
-- gold：先取 3-9 月算 LAG，再过滤出 4-9 月，4 月环比有基数
WITH monthly AS (SELECT substr(order_date,1,7) AS month, COUNT(*) AS orders
                 FROM orders WHERE status='已完成' AND order_date >= '2026-03-01' AND order_date <= '2026-09-30'
                 GROUP BY month),
     lagged AS (SELECT month, orders, LAG(orders) OVER (ORDER BY month) AS prev_orders FROM monthly)
SELECT month, orders, ROUND(100.0*(orders-prev_orders)/prev_orders, 2) AS mom_pct
FROM lagged WHERE month >= '2026-04' ORDER BY month;
```

```sql
-- 模型：WHERE 先截到 4-9 月，再算 LAG（4 月没有 3 月基数）
WITH monthly AS (... WHERE order_date >= '2026-04-01' ... GROUP BY substr(order_date,1,7))
SELECT "月份", "已完成订单量",
       ROUND(("已完成订单量"*1.0 / NULLIF(LAG("已完成订单量") OVER (ORDER BY "月份"),0) - 1)*100.0, 2) AS "环比增长率(%)"
FROM monthly ORDER BY "月份";
```

**差异**：4 月环比 gold=9.61%，模型返回 NULL。错在窗口函数在 WHERE 之后计算——**LAG 需要比输出区间多取一个前置周期**。

#### ③ q031 品类同比：用订单总额按品类重复累加（口径/扇出）

**问题**：2026 年 9 月各品类的 GMV 同比去年 9 月增长了多少？

```sql
-- gold：按明细行金额（quantity*unit_price）聚合，订单含多品类时金额按行拆分
WITH cur AS (SELECT p.category, SUM(i.quantity*i.unit_price) AS gmv
             FROM order_items i JOIN orders o ON o.order_id=i.order_id JOIN products p ON p.product_id=i.product_id
             WHERE o.status='已完成' AND o.order_date LIKE '2026-09%' GROUP BY p.category),
     prev AS (... '2025-09%' ...)
SELECT cur.category, ROUND(100.0*(cur.gmv-prev.gmv)/prev.gmv, 2) AS yoy_pct
FROM cur JOIN prev ON prev.category=cur.category ORDER BY yoy_pct DESC;
```

```sql
-- 模型：先把 (order_id, order_date, total_amount, category) DISTINCT，再 SUM(total_amount)
WITH order_category AS (
  SELECT DISTINCT o.order_id, o.order_date, o.total_amount, p.category
  FROM orders o JOIN order_items oi ON oi.order_id=o.order_id JOIN products p ON p.product_id=oi.product_id
  WHERE o.status='已完成' AND (substr(o.order_date,1,7)='2026-09' OR substr(o.order_date,1,7)='2025-09')),
     gmv AS (SELECT category,
                    SUM(CASE WHEN substr(order_date,1,7)='2026-09' THEN total_amount ELSE 0 END) AS gmv_2026,
                    SUM(CASE WHEN substr(order_date,1,7)='2025-09' THEN total_amount ELSE 0 END) AS gmv_2025
             FROM order_category GROUP BY category)
SELECT category AS "品类", ROUND((gmv_2026/NULLIF(gmv_2025,0)-1)*100.0,2) AS "GMV同比增长(%)" FROM gmv ...;
```

**差异**：一单含多品类时，整单金额被完整计入每个品类，品类间重复计数（数码 78.4% vs gold 57.13%，总额也对不上）。正确做法是**先按明细行拆金额再聚合**，不能把订单级金额挂到品类维度。

#### ④ q041 品类退货率：多按月份分组，行数爆炸（行数爆炸）

**问题**：2026 年 1-8 月各品类的退货率（退货件数/销售件数，退货按原订单月份归属）？

```sql
-- gold：8 个品类、每品类一行
WITH sales AS (SELECT p.category, SUM(i.quantity) AS sold FROM order_items i JOIN orders o ON ... WHERE o.status='已完成' AND o.order_date BETWEEN '2026-01-01' AND '2026-08-31' GROUP BY p.category),
     ret AS (SELECT p.category, SUM(r.quantity) AS returned FROM returns r JOIN orders o ON ... WHERE o.status='已完成' AND o.order_date BETWEEN '2026-01-01' AND '2026-08-31' GROUP BY p.category)
SELECT s.category, ROUND(100.0*COALESCE(r.returned,0)/s.sold, 2) AS return_rate_pct
FROM sales s LEFT JOIN ret r ON r.category=s.category ORDER BY return_rate_pct DESC;
```

```sql
-- 模型：按 (品类, 月份) 分组，返回 64 行
WITH sales AS (SELECT p.category AS "品类", substr(o.order_date,1,7) AS "月份", SUM(oi.quantity) AS "销售件数" ... GROUP BY p.category, substr(o.order_date,1,7)),
     ret AS (... 同构，带月份 ...)
SELECT s."品类", s."月份", ROUND(COALESCE(r."退货件数",0)*100.0/NULLIF(s."销售件数",0),2) AS "退货率(%)"
FROM sales s LEFT JOIN ret r ON r."品类"=s."品类" AND r."月份"=s."月份" ORDER BY s."月份", s."品类";
```

**差异**：gold 8 行，模型 64 行（8 品类 × 8 个月）。题目问"2026 年 1-8 月各品类"，应把月份聚合掉；模型把时间维度留在了结果里。虽然每格的比率算法正确，但问题层级理解错了。

#### ⑤ q046 品类 GMV/同比/退款占比：join 明细后 SUM(订单总额)（口径/扇出）

**问题**：2026 年 9 月各品类的 GMV、同比增速，以及当月退款金额占该品类 GMV 的比例？

```sql
-- gold：GMV 用明细行金额 SUM(i.quantity*i.unit_price)；退款单独一路 CTE
WITH cur AS (SELECT p.category, SUM(i.quantity*i.unit_price) AS gmv FROM order_items i JOIN orders o ... WHERE o.status='已完成' AND o.order_date LIKE '2026-09%' GROUP BY p.category),
     prev AS (... '2025-09%' ...),
     ref AS (SELECT p.category, SUM(r.refund_amount) AS refund FROM returns r JOIN products p ON p.product_id=r.product_id WHERE r.return_date LIKE '2026-09%' GROUP BY p.category)
SELECT cur.category, ROUND(cur.gmv,2) AS gmv,
       ROUND(100.0*(cur.gmv-prev.gmv)/prev.gmv,2) AS yoy_pct,
       ROUND(100.0*COALESCE(ref.refund,0)/cur.gmv,2) AS refund_ratio_pct
FROM cur JOIN prev ON prev.category=cur.category LEFT JOIN ref ON ref.category=cur.category ORDER BY gmv DESC;
```

```sql
-- 模型：orders JOIN order_items 之后 SUM(o.total_amount)（订单总额被行数放大）
WITH gmv_2026 AS (SELECT p.category, SUM(o.total_amount) AS gmv
                  FROM orders o JOIN order_items oi ON oi.order_id=o.order_id JOIN products p ON p.product_id=oi.product_id
                  WHERE o.status='已完成' AND o.order_date >= '2026-09-01' AND o.order_date < '2026-10-01' GROUP BY p.category),
     ...
SELECT g26.category AS "品类", ROUND(g26.gmv,2) AS "GMV",
       ROUND((g26.gmv/NULLIF(g25.gmv,0)-1)*100.0,2) AS "同比增速(%)",
       ROUND(COALESCE(rf.refund,0)/NULLIF(g26.gmv,0)*100.0,2) AS "退款占GMV比例(%)" ...;
```

**差异**：数码 GMV 3,207,225 vs gold 2,119,348——一单 N 行就被重复加了 N 次（金额"行数爆炸"）。同比、退款占比因此全错。这是最典型的多表 join 扇出错误：**多对一 join 后不能 SUM 事实表里已经是订单级的金额**。

#### ⑥ q017 退货金额 Top10：值全对，多返回 3 列（多列）

- 问题：2026 年 9 月退货金额最高的 10 个商品是哪些？
- gold：`SELECT product_name, refund_amount`
- 模型：`SELECT product_name, category, brand, refund_amount, 退货单数`（5 列）
- **差异**：gold 的 10 行值全部正确（关键列命中 = 是），只是多带品类/品牌/单数。属于输出列纪律问题，不是计算错误。

#### ⑦ q043 退货率 Top5：值全对，多返回品类列（多列）

- 问题：2026 年 1-8 月退货率最高的五个商品？
- gold：`product_name, return_rate_pct`（5 行）
- 模型：`product_name, category, return_rate_pct`（多一列品类）
- **差异**：Top5 商品与比率完全一致，仅多返回品类。

#### ⑧ q047 渠道多指标：值全对，多返回退款金额列（多列）

- 问题：2026 年 9 月各渠道 GMV、订单数、客单价、退款金额占 GMV 比例
- gold：`channel, gmv, orders, aov, refund_ratio_pct`（5 列）
- 模型：多带 `退款金额` 一列（6 列）
- **差异**：所有指标值逐行一致，仅多一列中间量。

> 小结：graph 的 8 个失败 = 3 个"多列"（可通过更硬的输出列约束消除）+ 4 个口径/扇出 + 1 个行数层级错误。**没有任何一题是 SQL 写不出来或执行失败**。

### 4.3 baseline 失败摘要（21 题）

| 分类 | 题号 | 典型原因 |
|---|---|---|
| 仅多列（值对但列多） | q007, q016, q017, q020, q024, q027, q044, q047, q048, q050 | 总是额外 `SELECT` 订单数/GMV/ID 等中间列；单表题 q007 也多了"全部订单数" |
| 口径/逻辑错误 | q021, q029, q030, q031, q035, q040, q041, q042, q043, q045, q046 | 见下 |

baseline 真实错误里最常见的 3 类：
1. **join 扇出 / 金额重复累加**：q035（品类 GMV 用订单总额）、q041（退货与销售按日期 join 后重复计数，退货率 74%）、q042（客单价 `SUM(total_amount)` 在 join 明细后放大到 9,567 元）、q046（同 q046 fan-out）。
2. **去重/范围错误**：q040（`SUM(DISTINCT total_amount)` 导致退款占比 6.43% vs 5.33%）、q030（只取 4-9 月订单的退货，漏掉 3 月订单在 4 月的退货）、q021（客户数用全量注册客户）。
3. **窗口函数基数**：q029、q045 的 4 月/7 月环比或同比为 NULL——WHERE 在窗口前截断，没有多取前置周期。

## 5. 诚实的边界

### 5.1 本轮**没有触发自修复**，不能宣称修复带来提升

- `avg_attempts=1.0`、`repaired_runs=0`：两边 104 次生成全部一次成功，SQL 合法率 100%，repair 分支一次都没进。
- 因此本报告的 +25pp **全部来自"探查 + 计划 + 口径理解 + 一次写对"**，与自修复无关。
- 自修复能力只在离线单测里验证过：`tests/test_pipeline_offline.py::test_self_repair` / `test_self_repair_gives_error_feedback`（构造错误 SQL 验证 repair 节点与错误回灌）。要在评测里量化它，需要一个**故意会让首次 SQL 失败的数据集**（不存在的列、歧义字段、超出沙箱上限的查询），否则这块能力等于没被测到。

### 5.2 数据与样本的局限

- 数据库是**造**的：干净、类型统一、日期是规范 `YYYY-MM-DD`、无缺失值、无重复主键、无 schema 漂移。真实脏库上的准确率一定低于 84.6%。
- n=52、每配置只跑 1 次、temperature 0：**没有方差/置信区间**。±1 题就是 ±1.9pp，差距 <10pp 的维度不要过度解读（例如 graph easy 100% vs baseline 92.3% 只差 1 题）。
- 判分是**严格结果集相等**：多返回一列即算错（所以有 projection 诊断作为补充）。若产品上允许"多带上下文列"，graph 的实际可用性高于主指标；反之若要求表格与问题严格对应，则主指标才是对的。
- gold 由我们编写：口径本身是"一种约定"（GMV 不含取消单、退货含处理中、新客按首笔已完成订单）。换一套口径，数字会变。评测过程中已修正 2 处 gold 自身 bug（q022/q030），说明 gold 也需要持续 review。

### 5.3 baseline 比率类 0% 的正确解读

比率类 5 题 baseline 全错，但细看：
- **2 题只是列格式问题**（q043 多返回 ID/品类/销量/退款件数，q044 多返回 GMV/预算）——值其实是对的；
- **3 题是真实错误**：q040（`SUM(DISTINCT ...)` 错误去重导致退款占比 6.43%）、q041（join 后件数重复计数，退货率 74%）、q042（join 明细后客单价放大 3 倍）。
所以"baseline 完全不会比率"是过度归因；更准确的说法是：**一次性生成在比率题上既容易多带列，也容易在 join 后算错分母**，而 Agent 的校验/多轮让它在这类题上明显更稳。

### 5.4 成本与延迟口径

- 成本按固定价目表估算，不含重试/失败请求；实际账单可能有差异。
- 延迟是单机串行墙钟时间，受网络与 API 排队影响；P95 只代表本环境。
- graph 成本是 baseline 的 4.1 倍、P50 延迟 7.4 倍。这个 trade-off 是否值得取决于产品：**问数场景通常愿意为 +25pp 准确率付这个成本**；高频简单问题可以做路由（见 §6）。

## 6. 结论与下一步

### 6.1 结论

1. **Agent 编排带来 +25.0pp 执行准确率**（84.6% vs 59.6%），且**没有一题是 baseline 对而 graph 错**——增益是净增的，不是此消彼长。
2. 增益集中在**多表 join（+20pp）、比率（+60pp）、复杂综合（+37.5pp）**；简单单表题两者都接近满分。复杂度越高，探查/口径理解/结果校验的价值越大。
3. 代价是**4.1 倍成本、7.4 倍 P50 延迟、3.3 倍 token**。这是一个明确的质量-成本曲线，不是"免费提升"。
4. graph 当前仍有 5 个真实错误，集中在 **join 扇出、窗口函数前置周期、指标分母范围、分组层级** 四类——都是可被 prompt/规则/自动校验系统性改进的。

### 6.2 下一步优化方向

1. **把本轮失败模式写进生成侧约束**：join 多对一后禁止 SUM 订单级金额；聚合前先明确 grain（订单/明细/品类/月）；窗口函数必须多取一个前置周期；指标分母必须跟随事实表过滤。目标：graph 从 84.6% → 90%+。
2. **补一个"自修复压力集"**：故意构造首次必错的题（未知列、歧义同名字段、超时大查询），量化 repair 成功率与尝试次数，再谈自修复价值。
3. **路由降本**：easy/单表题走 baseline 式单次生成（成本 ¥0.0038、1s），medium/hard 走 graph；预计整体成本可降 40%-60%，准确率损失很小。
4. **扩充评测**：脏数据版本（缺失/重复/格式混乱）、多轮追问集、图表有效性、答案忠实度（LLM-as-judge）、跨 seed 稳定性；把 n 从 52 扩到 150+ 再谈置信区间。
5. **持续 review gold**：本轮已发现 2 处 gold bug；`run_eval` 启动时会强制执行全部 gold（非空/行数校验），改题后务必重跑一次 `--config baseline --limit 3` 确认数据集仍自洽。

## 附录：复现与产物

```bash
# 1) 确定性生成数据库（两次 md5 一致）
python -m insight.db.seed

# 2) 冒烟 / A/B
python -m insight.eval.run_eval --config baseline --limit 3
python -m insight.eval.run_eval --config both

# 3) 指标自测（结果集比较、容差、NULL、诊断指标）
python -m insight.eval.metrics

# 4) 产物
# docs/eval/graph.jsonl       逐题结果（含 gold/pred 的 SQL 与结果行）
# docs/eval/baseline.jsonl    同上
# docs/eval/summary.json      汇总指标 + 失败清单 + 分维度
# docs/eval/run.log           第三轮原始跑分日志（gold 修正前的 raw 结果）
```

文件归属：数据生成 `insight/db/seed.py` + `schema.py` + `schema.md`；数据集/指标/基线/跑分 `insight/eval/`；本报告 `docs/eval-report.md`。
