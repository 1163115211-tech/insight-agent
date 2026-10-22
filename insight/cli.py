"""命令行入口：python -m insight ..."""
from __future__ import annotations

import json
import time

import typer
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from . import config

app = typer.Typer(add_completion=False, help="问数 Insight Agent — 自然语言数据分析")
console = Console()

_ICON = {
    "start": "[dim]·[/dim]",
    "ok": "[green]✓[/green]",
    "error": "[red]✗[/red]",
    "retry": "[yellow]↻[/yellow]",
    "reject": "[red]⊘[/red]",
    "interrupt": "[yellow]?[/yellow]",
}


def _render(event) -> None:
    icon = _ICON.get(event.status, "·")
    d = event.data or {}
    node = event.node

    if node == "intake" and event.status == "ok":
        console.print(f"{icon} 分诊：{d.get('intent')}　[dim]{d.get('reason', '')}[/dim]")
    elif node == "explore" and event.status == "ok":
        console.print(f"{icon} 选表：{', '.join(d.get('tables', []))}　[dim]{d.get('ddl_chars')} 字符 DDL[/dim]")
    elif node == "plan" and event.status == "ok":
        for i, s in enumerate(d.get("steps", []), 1):
            console.print(f"{icon} 计划 {i}. {s}")
    elif node == "gen_sql" and event.status == "ok":
        console.print(f"{icon} 生成 SQL（第 {d.get('attempt')} 次）")
    elif node == "guard" and event.status == "ok":
        console.print(f"{icon} 校验通过：[dim]{' / '.join(d.get('checks', []))}[/dim]")
    elif node == "guard" and event.status == "error":
        console.print(f"{icon} 校验拦截：[red]{d.get('reason')}[/red]")
    elif node == "execute" and event.status == "ok":
        console.print(
            f"{icon} 执行完成：{d.get('row_count')} 行 / {d.get('elapsed_ms')} ms"
            + ("　[dim](已截断)[/dim]" if d.get("truncated") else "")
        )
    elif node == "execute" and event.status == "error":
        console.print(f"{icon} 执行失败：[red]{d.get('error')}[/red]")
    elif node == "repair":
        console.print(f"{icon} 自修复第 {d.get('attempt')} 次：[yellow]{str(d.get('error'))[:120]}[/yellow]")
    elif node == "visualize":
        console.print(f"{icon} 出图：" + ("已生成" if d.get("chart") else f"跳过（{d.get('skip')}）"))
    elif node == "summarize" and event.status == "ok":
        console.print(Panel(d.get("answer", ""), title="结论", border_style="cyan"))
    elif node == "clarify" and event.status == "interrupt":
        console.print(Panel(d.get("question", ""), title="需要澄清", border_style="yellow"))
        for opt in d.get("options", []):
            console.print(f"   [dim]·[/dim] {opt}")
    elif node == "meta" and event.status == "done":
        u = d.get("usage", {})
        console.print(
            f"[dim]耗时 {d.get('latency_ms')} ms · 尝试 {d.get('attempts')} 次 · "
            f"{u.get('total_tokens', 0)} tokens · ¥{u.get('cost_cny', 0):.4f}[/dim]"
        )
    elif node == "meta" and event.status == "error":
        console.print(f"[red]运行失败：{d.get('error')}[/red]")


@app.command()
def ask(
    question: str = typer.Argument(..., help="自然语言问题"),
    thread: str = typer.Option(None, "--thread", "-t", help="沿用已有会话（多轮追问）"),
    show_events: bool = typer.Option(False, "--events", help="打印原始事件 JSON"),
    json_out: bool = typer.Option(False, "--json", help="输出完整 JSON 结果"),
):
    """问一个问题。"""
    from . import service

    result = service.run(question, thread_id=thread)
    events = result.trace

    if json_out:
        console.print_json(json.dumps(result.to_dict(), ensure_ascii=False))
        return

    for raw in events:
        from .types import StepEvent

        _render(StepEvent(**raw))

    if result.sql and not result.interrupted:
        console.print()
        console.print(Syntax(result.sql, "sql", theme="monokai", word_wrap=True))

    if result.rows:
        table = Table(show_header=True, header_style="bold cyan")
        for c in result.columns:
            table.add_column(str(c), overflow="fold")
        for row in result.rows[:15]:
            table.add_row(*[str(v) for v in row])
        console.print(table)
        if len(result.rows) > 15:
            console.print(f"[dim]… 共 {len(result.rows)} 行[/dim]")

    if result.interrupted and result.interrupt_payload:
        answer = typer.prompt("请回答澄清问题（输入后继续）")
        result = service.resume(result.thread_id, answer)
        for raw in result.trace:
            from .types import StepEvent

            _render(StepEvent(**raw))
        if result.answer:
            console.print(Panel(result.answer, title="结论", border_style="cyan"))
        console.print(f"[dim]会话 ID：{result.thread_id}（追问时加 -t 沿用）[/dim]")


def _find_free_port(host: str, start: int, tries: int = 10) -> int:
    """从 start 开始找一个能绑的端口。8000 常被别的软件占用（本机就被 Manager.exe 占了）。"""
    import socket

    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    return start


@app.command()
def serve(
    port: int = typer.Option(8000, "--port", "-p"),
    host: str = typer.Option("127.0.0.1"),
    strict_port: bool = typer.Option(False, "--strict-port", help="端口被占用就直接失败，不自动换"),
):
    """启动 Web 演示（FastAPI + SSE）。端口被占用时自动顺延。"""
    import sys

    import uvicorn

    root = str(config.PROJECT_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)

    chosen = port if strict_port else _find_free_port(host, port)
    if chosen != port:
        console.print(f"[yellow]端口 {port} 被占用，改用 {chosen}[/yellow]")
    console.print(f"[green]→ http://{host}:{chosen}[/green]")
    uvicorn.run("web.app:app", host=host, port=chosen, reload=False)


@app.command()
def initdb(force: bool = typer.Option(False, "--force", help="覆盖已有数据库")):
    """生成/重建示例电商库。"""
    from .db import seed

    seed.build(force=force)
    console.print(f"[green]✓[/green] 数据库就绪：{config.DB_PATH}")


@app.command()
def doctor():
    """自检：环境、密钥、数据库、模型连通性。"""
    from . import service, tools

    table = Table(title="环境自检", show_header=True, header_style="bold cyan")
    table.add_column("检查项")
    table.add_column("结果")

    table.add_row("Python", f"{__import__('sys').version.split()[0]}")
    table.add_row("数据库", f"{config.DB_PATH}"
                  + ("" if config.DB_PATH.exists() else " [red](不存在，先跑 initdb)[/red]"))
    try:
        tables = tools.list_tables()
        table.add_row("表", ", ".join(f"{t['name']}({t['rows']})" for t in tables))
    except Exception as e:  # noqa: BLE001
        table.add_row("表", f"[red]{e}[/red]")

    try:
        from .config import get_llm
        from langchain_core.messages import HumanMessage

        t0 = time.perf_counter()
        r = get_llm(max_tokens=8).invoke([HumanMessage(content="回复:ok")])
        table.add_row("模型", f"{config.MODEL_NAME} ✓ {int((time.perf_counter() - t0) * 1000)}ms")
    except Exception as e:  # noqa: BLE001
        table.add_row("模型", f"[red]{e}[/red]")

    try:
        g = service.get_graph()
        table.add_row("图", f"{len(g.get_graph().nodes)} 个节点 ✓")
    except Exception as e:  # noqa: BLE001
        table.add_row("图", f"[red]{e}[/red]")

    table.add_row("沙箱", f"只读 · {config.SQL_MAX_ROWS} 行上限 · {config.SQL_TIMEOUT_MS}ms 超时")
    try:
        import os

        table.add_row("LangSmith", "已开启" if os.getenv("LANGSMITH_TRACING") else "未开启")
    except Exception:  # noqa: BLE001
        pass
    console.print(table)


@app.command("eval")
def eval_cmd(
    config_name: str = typer.Option("both", "--config", "-c", help="graph | baseline | both"),
    limit: int = typer.Option(0, "--limit", "-n", help="只跑前 N 题（0=全部）"),
):
    """跑评测集并输出报告。"""
    from .eval import run_eval

    run_eval.main(config=config_name, limit=limit or None)


if __name__ == "__main__":
    app()
