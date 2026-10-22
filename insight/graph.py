"""图的装配。

节点职责单一、边只做路由判断，把"控制流"和"模型调用"彻底分开：

    intake ─┬─ clarify ─────────────┐
            ├─ reject_answer         │
            └─ explore → plan → gen_sql → guard ─┬─ execute ─┬─ visualize → summarize
                                                │            └─ repair ─┐
                                                └─ repair ────────────┴→ gen_sql
"""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from .nodes import (
    AgentState,
    clarify,
    execute,
    explore,
    gen_sql,
    guard,
    intake,
    plan,
    reject_answer,
    repair,
    route_after_execute,
    route_after_guard,
    route_intake,
    summarize,
    visualize,
)


def build_graph(checkpointer=None):
    """构建并编译图。传 checkpointer 才有跨轮记忆和中断恢复能力。"""
    g = StateGraph(AgentState)

    g.add_node("intake", intake)
    g.add_node("clarify", clarify)
    g.add_node("reject_answer", reject_answer)
    g.add_node("explore", explore)
    g.add_node("plan", plan)
    g.add_node("gen_sql", gen_sql)
    g.add_node("guard", guard)
    g.add_node("execute", execute)
    g.add_node("repair", repair)
    g.add_node("visualize", visualize)
    g.add_node("summarize", summarize)

    g.add_edge(START, "intake")
    g.add_conditional_edges(
        "intake",
        route_intake,
        {"clarify": "clarify", "reject_answer": "reject_answer", "explore": "explore"},
    )
    g.add_edge("clarify", "explore")
    g.add_edge("explore", "plan")
    g.add_edge("plan", "gen_sql")
    g.add_edge("gen_sql", "guard")
    g.add_conditional_edges(
        "guard",
        route_after_guard,
        {"execute": "execute", "repair": "repair", "summarize": "summarize"},
    )
    g.add_conditional_edges(
        "execute",
        route_after_execute,
        {"visualize": "visualize", "repair": "repair", "summarize": "summarize"},
    )
    g.add_edge("repair", "gen_sql")
    g.add_edge("visualize", "summarize")
    g.add_edge("summarize", END)
    g.add_edge("reject_answer", END)

    return g.compile(checkpointer=checkpointer)
