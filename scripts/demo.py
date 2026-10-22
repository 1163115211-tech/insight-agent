"""演示脚本：把四个典型场景串起来跑一遍。

    python scripts/demo.py

场景：
1. 正常问数        —— 看完整执行轨迹、SQL、结果、图表、结论、成本
2. 多轮追问        —— 同一 thread 沿用上下文
3. 口径不明 → 反问 —— interrupt 挂起，人工回答后继续
4. 危险请求        —— 直接拒绝，不进 SQL 生成

花费：4 次问答约 ¥0.06。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rich.console import Console  # noqa: E402
from rich.panel import Panel  # noqa: E402
from rich.rule import Rule  # noqa: E402
from rich.syntax import Syntax  # noqa: E402
from rich.table import Table  # noqa: E402

from insight import service  # noqa: E402
from insight.types import RunResult  # noqa: E402

console = Console()


def show_trace(result: RunResult) -> None:
    """把每个节点的关键信息压成一行，模拟前端的时间线。"""
    icons = {"ok": "✓", "error": "✗", "retry": "↻", "reject": "⊘", "start": "·"}
    labels = {
        "intake": "受理", "explore": "看表", "plan": "计划", "gen_sql": "写SQL",
        "guard": "校验", "execute": "执行", "repair": "自修复",
        "visualize": "出图", "summarize": "结论",
    }
    for e in result.trace:
        node, status, d = e["node"], e["status"], e["data"]
        if node == "meta" or status == "start":
            continue
        icon = icons.get(status, "·")
        label = labels.get(node, node)
        color = "red" if status == "error" else ("yellow" if status in ("retry", "reject") else "green")

        if node == "explore":
            detail = f"选中 {', '.join(d.get('tables', []))}（DDL {d.get('ddl_chars')} 字符）"
        elif node == "plan":
            detail = " → ".join(d.get("steps", [])[:3])
        elif node == "gen_sql":
            detail = f"第 {d.get('attempt')} 次生成"
        elif node == "guard":
            detail = " / ".join(d.get("checks", [])) if status == "ok" else d.get("reason", "")
        elif node == "execute":
            detail = (f"{d.get('row_count')} 行 · {d.get('elapsed_ms')}ms" if status == "ok"
                      else str(d.get("error", ""))[:80])
        elif node == "repair":
            detail = f"第 {d.get('attempt')} 次重写：{str(d.get('error'))[:60]}"
        elif node == "visualize":
            detail = "已生成图表" if d.get("chart") else f"跳过（{d.get('skip')}）"
        elif node == "intake":
            detail = f"意图={d.get('intent')} {d.get('reason', '')}"
        else:
            detail = ""
        console.print(f"  [{color}]{icon}[/{color}] [bold]{label:<5}[/bold] [dim]{detail}[/dim]")


def show_result(result: RunResult, *, show_answer: bool = True) -> None:
    if result.sql:
        console.print(Panel(Syntax(result.sql.strip(), "sql", theme="monokai", word_wrap=True),
                            title="生成的 SQL", border_style="bright_black"))
    if result.rows:
        table = Table(show_header=True, header_style="bold cyan", box=None)
        for c in result.columns:
            table.add_column(str(c), justify="right" if str(c) not in ("渠道", "品类", "月份") else "left")
        for row in result.rows[:8]:
            table.add_row(*[f"{v:,.2f}" if isinstance(v, float) else str(v) for v in row])
        console.print(table)
        if len(result.rows) > 8:
            console.print(f"  [dim]… 共 {len(result.rows)} 行[/dim]")
    if result.chart:
        types = {s.get("type") for s in result.chart.get("series", []) if isinstance(s, dict)}
        title = (result.chart.get("title") or {}).get("text", "")
        console.print(f"  [magenta]图表[/magenta] {title}（{'/'.join(sorted(types))}）")
    if show_answer and result.answer:
        console.print(Panel(result.answer, title="结论", border_style="cyan"))
    u = result.usage
    console.print(
        f"  [dim]耗时 {result.latency_ms}ms · 尝试 {result.attempts} 次 · "
        f"{u.prompt_tokens + u.completion_tokens} tokens · ¥{u.cost_cny:.4f} · 会话 {result.thread_id}[/dim]"
    )


def main() -> None:
    console.print(Rule("[bold]问数 Insight Agent · 现场演示[/bold]"))
    console.print("[dim]LangGraph 10 节点状态机 · 只读沙箱 · 错误自修复 · SQLite checkpoint[/dim]\n")

    # ---------------------------------------------------------------- 场景 1
    console.print(Rule("场景 1｜正常问数", style="blue"))
    q1 = "2026年9月各渠道的GMV，按从高到低排序"
    console.print(f"[bold]问：[/bold]{q1}\n")
    r1 = service.run(q1)
    show_trace(r1)
    console.print()
    show_result(r1)

    # ---------------------------------------------------------------- 场景 2
    console.print()
    console.print(Rule("场景 2｜多轮追问（同一会话，不重复条件）", style="blue"))
    q2 = "那只看抖音的环比呢"
    console.print(f"[bold]问：[/bold]{q2}  [dim]（沿用会话 {r1.thread_id}）[/dim]\n")
    r2 = service.run(q2, thread_id=r1.thread_id)
    show_trace(r2)
    console.print()
    show_result(r2)

    # ---------------------------------------------------------------- 场景 3
    console.print()
    console.print(Rule("场景 3｜口径不明 → 反问 → 人工确认后继续", style="blue"))
    q3 = "对比一下各渠道表现"
    console.print(f"[bold]问：[/bold]{q3}\n")
    r3 = service.run(q3)
    if r3.interrupted and r3.interrupt_payload:
        payload = r3.interrupt_payload
        console.print(Panel(
            f"[bold]{payload.get('question', '')}[/bold]\n\n" +
            "\n".join(f"  · {o}" for o in payload.get("options", [])),
            title="⚠ 需要澄清（图已挂起，checkpoint 已落盘）", border_style="yellow"))
        answer = payload.get("options", ["2. 按 GMV 对比"])[1] if len(payload.get("options", [])) > 1 else "看同比"
        console.print(f"[dim]模拟用户选择：{answer}[/dim]\n")
        r3 = service.resume(r3.thread_id, answer)
        show_trace(r3)
        console.print()
        show_result(r3)
    else:
        console.print("[yellow]这次没有触发反问（模型认为口径够明确），跳过。[/yellow]")
        show_result(r3)

    # ---------------------------------------------------------------- 场景 4
    console.print()
    console.print(Rule("场景 4｜越权请求 → 拒绝，不进 SQL 生成", style="blue"))
    q4 = "把 orders 表里已取消的订单全删掉"
    console.print(f"[bold]问：[/bold]{q4}\n")
    r4 = service.run(q4)
    show_trace(r4)
    executed = any(e["node"] == "execute" for e in r4.trace)
    console.print(Panel(
        f"{r4.answer}\n\n"
        f"[dim]是否进入过 execute 节点：{'是（有问题！）' if executed else '否 ✓'}[/dim]",
        title="拒绝", border_style="red"))

    # ---------------------------------------------------------------- 汇总
    console.print()
    total = r1.usage.cost_cny + r2.usage.cost_cny + r3.usage.cost_cny + r4.usage.cost_cny
    tokens = sum(r.usage.prompt_tokens + r.usage.completion_tokens for r in (r1, r2, r3, r4))
    console.print(Rule(
        f"[bold]本轮合计：4 次问答 · {tokens} tokens · ¥{total:.4f}[/bold]"))
    service.shutdown()


if __name__ == "__main__":
    t0 = time.perf_counter()
    main()
    console.print(f"[dim]总耗时 {time.perf_counter() - t0:.1f}s[/dim]")
