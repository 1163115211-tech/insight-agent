"""评测入口：跑 graph / baseline 两种配置，逐题落 jsonl，汇总落 summary.json。

用法（CONTRACT 第 5 节）：
    python -m insight.eval.run_eval --config graph
    python -m insight.eval.run_eval --config baseline
    python -m insight.eval.run_eval --config both --limit 10

设计取舍：
- **每题独立会话**：graph 调用 `service.run(question)` 不传 thread_id，避免上下文互相污染；
- **gold 只执行一次**：52 条 gold 结果缓存复用，两种配置比的是同一份标准答案；
- **一个配置跑挂不拖垮另一个**：graph 服务未就绪时，baseline 结果照样落盘，
  退出码非 0 提醒调用方（开发期很常见，方便先验 baseline）。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from insight.config import DB_PATH, EVAL_OUT_DIR
from insight.eval import baseline as baseline_mod
from insight.eval.metrics import (
    EvalRecord,
    aggregate,
    classify_failure,
    compare_projection,
    compare_result_sets,
)

QUESTIONS_PATH = Path(__file__).resolve().parent / "questions.jsonl"
MAX_GOLD_ROWS = 50  # 数据集约定：gold 行数 1~50


# ---------------- 数据集与 gold 执行 ----------------
def load_questions(path: Path = QUESTIONS_PATH, limit: int | None = None) -> list[dict[str, Any]]:
    """读 questions.jsonl 并做结构校验，坏数据要在烧 token 之前暴露。"""
    questions: list[dict[str, Any]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"questions.jsonl 第 {lineno} 行不是合法 JSON：{exc}") from exc
        for key in ("id", "question", "gold_sql", "difficulty", "tags"):
            if key not in item:
                raise ValueError(f"questions.jsonl 第 {lineno} 行缺少字段 {key}")
        questions.append(item)

    ids = [q["id"] for q in questions]
    if len(ids) != len(set(ids)):
        raise ValueError("questions.jsonl 存在重复 id")
    if limit is not None:
        questions = questions[: max(0, limit)]
    return questions


def execute_gold(db_path: Path, questions: Sequence[dict[str, Any]]) -> dict[str, tuple[list[str], list[list[Any]]]]:
    """一次性执行全部 gold SQL 并缓存结果；gold 跑不通说明数据集坏了，直接报错停跑。"""
    conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    results: dict[str, tuple[list[str], list[list[Any]]]] = {}
    try:
        for question in questions:
            try:
                cursor = conn.execute(question["gold_sql"])
                columns = [d[0] for d in cursor.description or []]
                rows = [list(row) for row in cursor.fetchall()]
            except Exception as exc:
                raise RuntimeError(f"gold SQL 执行失败 {question['id']}: {exc}") from exc
            if not rows:
                raise RuntimeError(f"gold SQL 结果为空 {question['id']}，数据集不合法")
            if len(rows) > MAX_GOLD_ROWS:
                raise RuntimeError(f"gold SQL 行数 {len(rows)} 超过 {MAX_GOLD_ROWS} {question['id']}")
            results[question["id"]] = (columns, rows)
    finally:
        conn.close()
    return results


# ---------------- 记录组装 ----------------
def _build_record(
    question: dict[str, Any],
    config: str,
    gold: tuple[list[str], list[list[Any]]],
    *,
    pred_sql: str,
    pred_columns: list[str],
    pred_rows: list[list[Any]],
    exec_ok: bool,
    error: str | None,
    attempts: int,
    latency_ms: int,
    usage: dict[str, Any],
    interrupted: bool = False,
    extra: dict[str, Any] | None = None,
) -> EvalRecord:
    gold_columns, gold_rows = gold
    correct, reason, detail = False, "", ""
    projection_correct = False
    if exec_ok:
        correct, reason, detail = compare_result_sets(gold_columns, gold_rows, pred_columns, pred_rows)
        projection_correct = correct or compare_projection(gold_columns, gold_rows, pred_columns, pred_rows)
    category = classify_failure(
        pred_sql=pred_sql,
        exec_ok=exec_ok,
        correct=correct,
        error=error,
        interrupted=interrupted,
        pred_empty=bool(exec_ok and not pred_rows),
    )
    return EvalRecord(
        id=question["id"],
        question=question["question"],
        difficulty=question["difficulty"],
        tags=list(question["tags"]),
        config=config,
        gold_sql=question["gold_sql"],
        pred_sql=pred_sql,
        correct=correct,
        projection_correct=projection_correct,
        exec_ok=exec_ok,
        compare_reason=reason,
        detail=detail,
        failure_category=category,
        error=error,
        attempts=attempts,
        latency_ms=latency_ms,
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        cost_cny=float(usage.get("cost_cny") or 0.0),
        llm_calls=int(usage.get("calls") or 0),
        gold_columns=list(gold_columns),
        gold_rows=gold_rows,
        pred_columns=list(pred_columns),
        pred_rows=pred_rows,
        extra=extra or {},
    )


def _log_progress(config: str, index: int, total: int, record: EvalRecord) -> None:
    status = "✔" if record.correct else "✘"
    print(
        f"[{config:<8} {index:>2}/{total}] {record.id} {status} "
        f"attempts={record.attempts} {record.latency_ms}ms "
        f"tokens={record.total_tokens} {record.failure_category or 'ok'}",
        flush=True,
    )


# ---------------- baseline 配置 ----------------
def evaluate_baseline(
    questions: Sequence[dict[str, Any]],
    gold: dict[str, tuple[list[str], list[list[Any]]]],
    db_path: Path,
) -> list[EvalRecord]:
    """单次生成基线：每题一次 LLM 调用 + 一次执行，不重试。"""
    llm = baseline_mod.get_llm()  # 复用同一个客户端，避免每题重建连接
    records: list[EvalRecord] = []
    for index, question in enumerate(questions, 1):
        result = baseline_mod.run_question(question["question"], llm=llm, db_path=db_path)
        usage = {
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "cost_cny": result.cost_cny,
            "calls": 1 if result.raw_response else 0,
        }
        record = _build_record(
            question,
            "baseline",
            gold[question["id"]],
            pred_sql=result.sql,
            pred_columns=result.columns,
            pred_rows=result.rows,
            exec_ok=result.exec_ok,
            error=result.error,
            attempts=1,
            latency_ms=result.latency_ms,
            usage=usage,
            extra={"truncated": result.truncated},
        )
        _log_progress("baseline", index, len(questions), record)
        records.append(record)
    return records


# ---------------- graph 配置 ----------------
def _normalize_graph_rows(result: Any) -> tuple[list[str], list[list[Any]]]:
    """service 契约里 rows 是 list[dict]；这里统一成矩阵，方便和 gold 比较。"""
    columns = list(getattr(result, "columns", None) or [])
    rows = list(getattr(result, "rows", None) or [])
    if rows and isinstance(rows[0], dict):
        if not columns:
            columns = list(rows[0].keys())
        return columns, [[row.get(col) for col in columns] for row in rows]
    return columns, [list(row) if not isinstance(row, dict) else list(row.values()) for row in rows]


def _graph_exec_ok(result: Any) -> bool:
    """判断 SQL 是否真的在 execute 节点跑成功了。

    有 events 时必须看到 execute/ok 才算成功——修复轮次耗尽、guard 反复拒杀时
    rows 为空且 service 不一定回填 error，不能拿"没报错"当执行成功。
    没有 events 的旧实现才退化为「没报错且有 SQL」。
    """
    if getattr(result, "interrupted", False):
        return False
    events = list(getattr(result, "events", None) or [])
    if events:
        return any(
            isinstance(e, dict) and e.get("node") == "execute" and e.get("status") == "ok"
            for e in events
        )
    return getattr(result, "error", None) is None and bool(getattr(result, "sql", ""))


def _graph_error(result: Any) -> str | None:
    """把 service 没透传出来的失败原因从事件流里捞出来（guard 拒绝 / execute 报错）。"""
    error = getattr(result, "error", None)
    if error:
        return str(error)
    for event in reversed(list(getattr(result, "events", None) or [])):
        if not isinstance(event, dict) or event.get("status") != "error":
            continue
        data = event.get("data") or {}
        message = data.get("error") or data.get("reason")
        if message:
            return f"{event.get('node')} 失败：{message}"
    return None


def _graph_pred_sql(result: Any) -> str:
    """优先取 gen_sql 事件里的 SQL；result.sql 为空时兜底。"""
    sql = (getattr(result, "sql", "") or "").strip()
    if sql:
        return sql
    for event in reversed(list(getattr(result, "events", None) or [])):
        if isinstance(event, dict) and event.get("node") == "gen_sql":
            candidate = (event.get("data") or {}).get("sql")
            if candidate:
                return str(candidate).strip()
    return ""


def evaluate_graph(
    questions: Sequence[dict[str, Any]],
    gold: dict[str, tuple[list[str], list[list[Any]]]],
) -> list[EvalRecord]:
    """跑真实 Agent 图。service 未就绪时抛 ImportError，由 main 决定是否继续跑 baseline。"""
    from insight.service import run as run_graph  # 延迟导入：baseline-only 模式不依赖它

    records: list[EvalRecord] = []
    for index, question in enumerate(questions, 1):
        started = time.perf_counter()
        try:
            result = run_graph(question["question"])
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            pred_sql = _graph_pred_sql(result)
            pred_columns, pred_rows = _normalize_graph_rows(result)
            usage_obj = getattr(result, "usage", None)
            usage = usage_obj.to_dict() if hasattr(usage_obj, "to_dict") else dict(usage_obj or {})
            attempts = int(getattr(result, "attempts", 0) or 0)
            if attempts == 0 and pred_sql:
                attempts = 1
            latency_ms = int(getattr(result, "latency_ms", 0) or elapsed_ms)
            record = _build_record(
                question,
                "graph",
                gold[question["id"]],
                pred_sql=pred_sql,
                pred_columns=pred_columns,
                pred_rows=pred_rows,
                exec_ok=_graph_exec_ok(result),
                error=_graph_error(result),
                attempts=attempts,
                latency_ms=latency_ms,
                usage=usage,
                interrupted=bool(getattr(result, "interrupted", False)),
                extra={
                    "answer": (getattr(result, "answer", "") or "")[:300],
                    "has_chart": bool(getattr(result, "chart", None)),
                    "thread_id": getattr(result, "thread_id", ""),
                },
            )
        except Exception as exc:  # 单题异常不能中断整轮，记录后继续
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            record = _build_record(
                question,
                "graph",
                gold[question["id"]],
                pred_sql="",
                pred_columns=[],
                pred_rows=[],
                exec_ok=False,
                error=f"图执行异常：{exc}",
                attempts=0,
                latency_ms=elapsed_ms,
                usage={},
            )
        _log_progress("graph", index, len(questions), record)
        records.append(record)
    return records


# ---------------- 汇总与输出 ----------------
def summarize(records: Sequence[EvalRecord]) -> dict[str, Any]:
    """aggregate 之外补两块报告要用的信息：分标签准确率、失败清单。"""
    summary = aggregate(records)
    by_tag: dict[str, dict[str, Any]] = {}
    tags = sorted({r.tags[0] for r in records if r.tags})
    for tag in tags:
        subset = [r for r in records if r.tags and r.tags[0] == tag]
        by_tag[tag] = {
            "n": len(subset),
            "execution_accuracy": round(sum(1 for r in subset if r.correct) / len(subset), 4),
            "projection_accuracy": round(sum(1 for r in subset if r.correct or r.projection_correct) / len(subset), 4),
            "avg_attempts": round(sum(r.attempts for r in subset) / len(subset), 4),
        }
    summary["by_tag"] = by_tag
    summary["failures"] = [
        {
            "id": r.id,
            "category": r.failure_category,
            "compare_reason": r.compare_reason,
            "detail": r.detail,
            "error": r.error,
            "attempts": r.attempts,
            "latency_ms": r.latency_ms,
        }
        for r in records
        if not r.correct
    ]
    return summary


def write_outputs(
    records_by_config: dict[str, list[EvalRecord]],
    summaries: dict[str, dict[str, Any]],
    questions: Sequence[dict[str, Any]],
    out_dir: Path,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for config, records in records_by_config.items():
        path = out_dir / f"{config}.jsonl"
        with path.open("w", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record.to_dict(), ensure_ascii=False, default=str) + "\n")
        print(f"已写入 {path}", flush=True)

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "db": str(DB_PATH),
        "dataset": {
            "n": len(questions),
            "difficulty": dict(Counter(q["difficulty"] for q in questions)),
            "tags": dict(Counter(q["tags"][0] for q in questions)),
        },
        "configs": summaries,
    }
    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写入 {summary_path}", flush=True)


def print_table(summaries: dict[str, dict[str, Any]]) -> None:
    header = (
        f"\n{'config':<10}{'n':>4}{'执行准确率':>10}{'关键列命中':>10}{'SQL合法率':>10}{'自修复率':>9}"
        f"{'平均尝试':>9}{'平均token':>10}{'平均成本':>10}{'P50(ms)':>9}{'P95(ms)':>9}"
    )
    print(header)
    print("-" * len(header))
    for config, summary in summaries.items():
        if not summary.get("n"):
            print(f"{config:<10}{'--':>4}  {summary.get('error', '未运行')}")
            continue
        repair = summary.get("self_repair_success_rate")
        repair_text = f"{repair * 100:.1f}%" if repair is not None else "N/A"
        print(
            f"{config:<10}{summary['n']:>4}"
            f"{summary['execution_accuracy'] * 100:>9.1f}%"
            f"{summary.get('projection_accuracy', 0.0) * 100:>9.1f}%"
            f"{summary['sql_valid_rate'] * 100:>9.1f}%"
            f"{repair_text:>9}"
            f"{summary['avg_attempts']:>9.2f}"
            f"{summary['avg_total_tokens']:>10.0f}"
            f"{summary['avg_cost_cny']:>10.4f}"
            f"{summary['p50_latency_ms']:>9.0f}"
            f"{summary['p95_latency_ms']:>9.0f}"
        )
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Insight Agent 评测：graph vs baseline")
    parser.add_argument("--config", choices=["graph", "baseline", "both"], default="both")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 题（冒烟用）")
    parser.add_argument("--out-dir", default=str(EVAL_OUT_DIR))
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--questions", default=str(QUESTIONS_PATH))
    args = parser.parse_args(argv)

    questions = load_questions(Path(args.questions), args.limit)
    if not questions:
        print("没有可跑的题目", file=sys.stderr)
        return 1

    print(f"加载 {len(questions)} 题，数据库 {args.db}", flush=True)
    gold = execute_gold(Path(args.db), questions)
    print("gold SQL 全部执行通过", flush=True)

    records_by_config: dict[str, list[EvalRecord]] = {}
    summaries: dict[str, dict[str, Any]] = {}
    failed_configs: list[str] = []
    started = time.perf_counter()

    if args.config in ("baseline", "both"):
        baseline_records = evaluate_baseline(questions, gold, Path(args.db))
        records_by_config["baseline"] = baseline_records
        summaries["baseline"] = summarize(baseline_records)
        failed = [r for r in baseline_records if not r.correct]
        print(f"baseline 完成：{len(baseline_records) - len(failed)}/{len(baseline_records)} 正确", flush=True)

    if args.config in ("graph", "both"):
        try:
            graph_records = evaluate_graph(questions, gold)
        except ImportError as exc:
            message = f"insight.service 未就绪：{exc}"
            print(f"[graph] {message}", file=sys.stderr, flush=True)
            summaries["graph"] = {"n": 0, "error": message}
            failed_configs.append("graph")
        else:
            records_by_config["graph"] = graph_records
            summaries["graph"] = summarize(graph_records)
            failed = [r for r in graph_records if not r.correct]
            print(f"graph 完成：{len(graph_records) - len(failed)}/{len(graph_records)} 正确", flush=True)

    elapsed = time.perf_counter() - started
    write_outputs(records_by_config, summaries, questions, Path(args.out_dir))

    # 控制台汇总按 graph、baseline 顺序展示，和报告一致
    ordered = {name: summaries[name] for name in ("graph", "baseline") if name in summaries}
    print_table(ordered)
    print(f"总耗时 {elapsed:.1f}s", flush=True)

    if len(ordered) == 2 and ordered["graph"].get("n") and ordered["baseline"].get("n"):
        gap = ordered["graph"]["execution_accuracy"] - ordered["baseline"]["execution_accuracy"]
        print(f"graph 相对 baseline 执行准确率提升 {gap * 100:+.1f} 个百分点\n", flush=True)

    return 1 if failed_configs else 0


if __name__ == "__main__":
    raise SystemExit(main())
