"""LLM tool definitions for the chaos monkey agent."""

from __future__ import annotations

from typing import Any

from langchain_core.tools import tool

from chaos_monkey.experiments.registry import ExperimentRegistry


@tool
def list_experiments() -> list[dict[str, Any]]:
    """List all available chaos experiment types with their metadata."""
    registry = ExperimentRegistry()
    return registry.list_all()


@tool
def get_experiment_info(name: str) -> dict[str, Any] | str:
    """Get detailed information about a specific chaos experiment type."""
    registry = ExperimentRegistry()
    cls = registry.get(name)
    if cls is None:
        return f"Unknown experiment: {name}"
    return cls().info()


@tool
def validate_experiment_target(
    experiment_name: str, target: str, namespace: str, topology: dict
) -> bool:
    """Check if a target is valid for a given experiment type."""
    registry = ExperimentRegistry()
    cls = registry.get(experiment_name)
    if cls is None:
        return False
    return cls().validate_target(target, namespace, topology)


@tool
def suggest_experiments_for_weakness(weakness: dict) -> list[str]:
    """Given a weakness dict, return experiment names that could test it.

    Uses the weakness's 'suggested_experiments' field if present,
    otherwise maps category to relevant experiments.
    """
    if "suggested_experiments" in weakness:
        return weakness["suggested_experiments"]

    category = weakness.get("category", "")
    category_map = {
        "single-point-of-failure": ["pod-kill", "pod-restart", "node-drain"],
        "missing-redundancy": ["pod-kill", "node-cordon", "network-partition"],
        "resource-risk": ["cpu-stress", "memory-stress", "disk-fill"],
        "network-risk": ["network-latency", "packet-loss", "dns-failure"],
        "config-risk": ["configmap-mutation", "secret-deletion"],
        "observability-gap": ["pod-kill", "network-latency"],
    }
    return category_map.get(category, ["pod-kill"])


ALL_TOOLS = [
    list_experiments,
    get_experiment_info,
    validate_experiment_target,
    suggest_experiments_for_weakness,
]
