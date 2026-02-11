"""LangGraph agent state definition."""

from __future__ import annotations

from typing import TypedDict


class AgentState(TypedDict, total=False):
    cluster_topology: dict
    weaknesses: list[dict]
    experiment_plan: list[dict]
    current_experiment: dict | None
    experiment_results: list[dict]
    observations: list[dict]
    report: dict | None
    messages: list
    safety_violations: list[str]
    dry_run: bool
