"""LLM-powered resilience weakness analysis."""

from __future__ import annotations

import json
import logging
from typing import Any

import litellm

from chaos_monkey.config import LLMConfig

logger = logging.getLogger(__name__)

ANALYSIS_SYSTEM_PROMPT = """\
You are a Kubernetes resilience expert. Given a cluster topology, identify \
potential resilience weaknesses that could cause outages or degraded service.

For each weakness, provide:
- title: short name
- severity: low, medium, or high
- category: one of (single-point-of-failure, missing-redundancy, resource-risk, \
network-risk, config-risk, observability-gap)
- description: brief explanation
- affected_resources: list of resource names
- suggested_experiments: list of chaos experiment names that would test this weakness

Respond ONLY with a JSON array of weakness objects. No markdown fences."""


class WeaknessAnalyzer:
    """Uses an LLM to analyze cluster topology and identify resilience weaknesses."""

    def __init__(self, llm_config: LLMConfig) -> None:
        self.llm_config = llm_config

    async def analyze(self, topology: dict[str, Any]) -> list[dict[str, Any]]:
        """Analyze topology and return a list of weakness dicts."""
        topology_summary = self._summarize_topology(topology)

        response = await litellm.acompletion(
            model=self.llm_config.model,
            temperature=self.llm_config.temperature,
            messages=[
                {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        "Analyze the following Kubernetes cluster topology for "
                        "resilience weaknesses:\n\n"
                        f"{json.dumps(topology_summary, indent=2)}"
                    ),
                },
            ],
        )

        raw = response.choices[0].message.content.strip()
        try:
            weaknesses = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("LLM returned non-JSON; attempting extraction")
            # Try extracting JSON array from possible markdown fence
            start = raw.find("[")
            end = raw.rfind("]") + 1
            if start >= 0 and end > start:
                weaknesses = json.loads(raw[start:end])
            else:
                weaknesses = [{"title": "Parse error", "raw_response": raw}]

        logger.info("Identified %d weaknesses", len(weaknesses))
        return weaknesses

    def _summarize_topology(self, topology: dict[str, Any]) -> dict[str, Any]:
        """Create a focused summary to fit within LLM context."""
        summary: dict[str, Any] = {"namespaces": []}
        for ns in topology.get("namespaces", []):
            ns_summary: dict[str, Any] = {
                "name": ns["name"],
                "deployments": [],
                "services": [],
                "pods_summary": {},
                "statefulsets": ns.get("statefulsets", []),
                "configmaps_count": len(ns.get("configmaps", [])),
                "secrets_count": len(ns.get("secrets", [])),
            }

            for dep in ns.get("deployments", []):
                ns_summary["deployments"].append({
                    "name": dep["name"],
                    "replicas": dep["replicas"],
                    "ready_replicas": dep["ready_replicas"],
                    "strategy": dep["strategy"],
                    "has_pdb": dep.get("has_pdb", False),
                })

            for svc in ns.get("services", []):
                ns_summary["services"].append({
                    "name": svc["name"],
                    "type": svc["type"],
                    "ports": svc["ports"],
                })

            pods = ns.get("pods", [])
            ns_summary["pods_summary"] = {
                "total": len(pods),
                "running": sum(1 for p in pods if p.get("status") == "Running"),
                "not_ready": sum(1 for p in pods if not p.get("ready")),
                "high_restarts": [
                    p["name"] for p in pods if p.get("restart_count", 0) > 5
                ],
                "without_resource_limits": [
                    p["name"] for p in pods
                    if any(
                        not c.get("resources", {}).get("limits")
                        for c in p.get("containers", [])
                    )
                ],
            }

            summary["namespaces"].append(ns_summary)

        summary["cluster_summary"] = topology.get("summary", {})
        return summary
