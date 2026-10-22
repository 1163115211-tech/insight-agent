"""集中配置：模型、成本价目、沙箱上限、路径。

所有可调参数都从这里出，避免散落在各模块里——面试时"你怎么调成本和延迟"就有话可讲。
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录（insight/ 的上一层）
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 优先读项目根 .env，其次环境变量
load_dotenv(PROJECT_ROOT / ".env", override=False)

# ---------------- 路径 ----------------
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = Path(os.getenv("INSIGHT_DB", DATA_DIR / "ecommerce.db"))
CHECKPOINT_PATH = Path(os.getenv("INSIGHT_CHECKPOINT", DATA_DIR / "checkpoints.sqlite"))

# 评测输出
EVAL_OUT_DIR = PROJECT_ROOT / "docs" / "eval"

# 业务口径文档：由数据方维护，运行时读进 prompt（数据和应用解耦）
SCHEMA_DOC = PROJECT_ROOT / "insight" / "db" / "schema.md"

# 数据快照日期。固定值而不是 now()，保证评测可复现：
# 换一天跑，"上月"就会变，gold SQL 立刻全错。
REFERENCE_DATE = os.getenv("INSIGHT_REFERENCE_DATE", "2026-10-22")

# 是否启用 explore 节点（先筛表再给 DDL）。关掉就全量 DDL 直接喂给模型，
# 用来做 ablation：看上下文工程到底省了多少 token。
USE_EXPLORE = os.getenv("INSIGHT_USE_EXPLORE", "1") != "0"

# ---------------- 模型 ----------------
MODEL_NAME = os.getenv("INSIGHT_MODEL", "deepseek-chat")
TEMPERATURE = float(os.getenv("INSIGHT_TEMPERATURE", "0"))
MAX_TOKENS = int(os.getenv("INSIGHT_MAX_TOKENS", "2048"))
REQUEST_TIMEOUT = float(os.getenv("INSIGHT_TIMEOUT", "60"))

# ---------------- 沙箱 ----------------
SQL_MAX_ROWS = int(os.getenv("INSIGHT_MAX_ROWS", "1000"))       # 结果行数上限
SQL_TIMEOUT_MS = int(os.getenv("INSIGHT_SQL_TIMEOUT_MS", "5000"))  # 单条 SQL 执行时限
MAX_RETRIES = int(os.getenv("INSIGHT_MAX_RETRIES", "3"))        # SQL 自修复最大尝试次数

# ---------------- 成本（估算用） ----------------
# DeepSeek 官方价目（cache miss），单位：元 / 百万 token。
# 换模型时改这里即可，代码里不写死。
PRICE_INPUT_PER_M = float(os.getenv("INSIGHT_PRICE_IN", "2.0"))
PRICE_OUTPUT_PER_M = float(os.getenv("INSIGHT_PRICE_OUT", "8.0"))
USD_TO_CNY = 1.0


def get_llm(*, temperature: float | None = None, max_tokens: int | None = None):
    """构造 LLM。OpenAI 兼容协议，默认指向 DeepSeek。

    注意：每次调用都新建是浪费的，实际由 nodes 层复用同一个实例。
    """
    from langchain_openai import ChatOpenAI

    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError(
            "缺少 DEEPSEEK_API_KEY。请在项目根目录 .env 里填写，参考 .env.example"
        )
    return ChatOpenAI(
        model=MODEL_NAME,
        api_key=api_key,
        base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        temperature=TEMPERATURE if temperature is None else temperature,
        max_tokens=MAX_TOKENS if max_tokens is None else max_tokens,
        timeout=REQUEST_TIMEOUT,
        max_retries=2,
    )


def estimate_cost(prompt_tokens: int, completion_tokens: int) -> float:
    """按价目表估算单次调用成本（元）。"""
    cost = (
        prompt_tokens / 1_000_000 * PRICE_INPUT_PER_M
        + completion_tokens / 1_000_000 * PRICE_OUTPUT_PER_M
    )
    return round(cost * USD_TO_CNY, 6)
