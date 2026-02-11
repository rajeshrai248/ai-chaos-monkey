"""LangGraph workflow definition for the chaos monkey agent."""

from __future__ import annotations

import logging
from typing import Any

from langgraph.graph import END, StateGraph

from chaos_monkey.agent.state import AgentState
from chaos_monkey.analysis.analyzer import WeaknessAnalyzer
from chaos_monkey.config import AppConfig
from chaos_monkey.discovery.cluster import ClusterDiscovery
from chaos_monkey.experiments.executor import ExperimentExecutor
from chaos_monkey.experiments.registry import ExperimentRegistry
from chaos_monkey.observer.monitor import HealthMonitor
from chaos_monkey.planner.planner import ExperimentPlanner
from chaos_monkey.reporter.report import ReportGenerator
from chaos_monkey.safety.controls import SafetyController

logger = logging.getLogger(__name__)


def build_graph(config: AppConfig) -> StateGraph:
    """Build and compile the LangGraph workflow."""

    discovery = ClusterDiscovery(config.kubernetes, config.target, config.safety)
    analyzer = WeaknessAnalyzer(config.llm)
    planner = ExperimentPlanner(config.llm)
    safety = SafetyController(config.safety)
    registry = ExperimentRegistry()
    executor = ExperimentExecutor(registry, safety, config.kubernetes)
    monitor = HealthMonitor(config.kubernetes, config.observer)
    reporter = ReportGenerator(config.report, config.llm)

    # ── Node functions ──────────────────────────────────────────────────

    async def discover_node(state: AgentState) -> dict[str, Any]:
        logger.info("Discovering cluster topology...")
        topology = await discovery.discover()
        return {"cluster_topology": topology}

    async def analyze_node(state: AgentState) -> dict[str, Any]:
        logger.info("Analyzing topology for weaknesses...")
        topology = state["cluster_topology"]
        weaknesses = await analyzer.analyze(topology)
        return {"weaknesses": weaknesses}

    async def plan_node(state: AgentState) -> dict[str, Any]:
        logger.info("Planning chaos experiments...")
        topology = state["cluster_topology"]
        weaknesses = state["weaknesses"]
        experiment_plan = await planner.plan(topology, weaknesses)
        return {"experiment_plan": experiment_plan}

    async def validate_node(state: AgentState) -> dict[str, Any]:
        logger.info("Validating experiment plan against safety rules...")
        plan = state.get("experiment_plan", [])
        violations: list[str] = []
        approved_plan: list[dict] = []

        results = safety.validate_plan(plan)
        for entry, result in zip(plan, results):
            if result.approved:
                approved_plan.append(entry)
            else:
                violations.append(
                    f"{entry.get('experiment')} on {entry.get('target')}: {result.reason}"
                )

        if violations:
            logger.warning("Safety violations: %s", violations)

        return {
            "experiment_plan": approved_plan,
            "safety_violations": violations,
        }

    def should_replan(state: AgentState) -> str:
        """Route after validate: if everything was rejected, go back to plan."""
        plan = state.get("experiment_plan", [])
        if not plan and state.get("safety_violations"):
            return "plan"
        return "execute"

    async def execute_node(state: AgentState) -> dict[str, Any]:
        logger.info("Executing experiments...")
        plan = state.get("experiment_plan", [])
        dry_run = state.get("dry_run", False)
        results = await executor.run_plan(plan, dry_run=dry_run)
        return {
            "experiment_results": state.get("experiment_results", [])
            + [r.to_dict() for r in results],
        }

    async def observe_node(state: AgentState) -> dict[str, Any]:
        logger.info("Observing cluster health...")
        # Determine namespaces from the plan
        namespaces = {
            e.get("namespace", "default")
            for e in state.get("experiment_plan", [])
        }
        all_observations: list[dict] = state.get("observations", [])

        for ns in namespaces:
            obs = await monitor.monitor_during_experiment(ns)
            all_observations.extend(obs)

        return {"observations": all_observations}

    def has_more_experiments(state: AgentState) -> str:
        """Check if there are pending experiments in the plan."""
        # In this flow, execute_node runs the full plan at once,
        # so we always proceed to report after observe.
        return "report"

    async def report_node(state: AgentState) -> dict[str, Any]:
        logger.info("Generating resilience report...")
        report_input = {
            "topology": state.get("cluster_topology", {}),
            "weaknesses": state.get("weaknesses", []),
            "experiment_plan": state.get("experiment_plan", []),
            "experiment_results": state.get("experiment_results", []),
            "observations": state.get("observations", []),
            "safety_violations": state.get("safety_violations", []),
        }
        report = await reporter.generate(report_input)
        return {"report": report}

    # ── Build graph ─────────────────────────────────────────────────────

    graph = StateGraph(AgentState)

    graph.add_node("discover", discover_node)
    graph.add_node("analyze", analyze_node)
    graph.add_node("plan", plan_node)
    graph.add_node("validate", validate_node)
    graph.add_node("execute", execute_node)
    graph.add_node("observe", observe_node)
    graph.add_node("report", report_node)

    graph.set_entry_point("discover")
    graph.add_edge("discover", "analyze")
    graph.add_edge("analyze", "plan")
    graph.add_edge("plan", "validate")
    graph.add_conditional_edges("validate", should_replan, {"plan": "plan", "execute": "execute"})
    graph.add_edge("execute", "observe")
    graph.add_conditional_edges("observe", has_more_experiments, {"report": "report"})
    graph.add_edge("report", END)

    return graph.compile()
