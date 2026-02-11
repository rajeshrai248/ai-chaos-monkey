"""LLM-powered experiment planner."""

from __future__ import annotations

import json
import logging
from typing import Any

import litellm

from chaos_monkey.config import LLMConfig
from chaos_monkey.experiments.registry import ExperimentRegistry

logger = logging.getLogger(__name__)

PLANNER_SYSTEM_PROMPT = """\
You are a chaos engineering planner. Given:
1. A Kubernetes cluster topology
2. Identified resilience weaknesses
3. Available chaos experiments

Create an ordered experiment plan to test the identified weaknesses.

For each experiment step, provide:
- experiment: the experiment type name (must match an available experiment)
- target: the specific resource name to target
- namespace: the Kubernetes namespace
- params: any experiment-specific parameters (object)
- rationale: why this experiment tests a specific weakness

Order experiments from lowest to highest blast radius.

Respond ONLY with a JSON array of experiment step objects. No markdown fences."""


class ExperimentPlanner:
    """Uses an LLM to create an experiment plan based on weaknesses and available experiments."""

    def __init__(self, llm_config: LLMConfig) -> None:
        self.llm_config = llm_config

    async def plan(
        self,
        topology: dict[str, Any],
        weaknesses: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Generate an experiment plan from topology and weaknesses."""
        registry = ExperimentRegistry()
        available = registry.list_all()

        response = await litellm.acompletion(
            model=self.llm_config.model,
            temperature=self.llm_config.temperature,
            messages=[
                {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        "## Available Experiments\n"
                        f"{json.dumps(available, indent=2)}\n\n"
                        "## Cluster Topology\n"
                        f"{json.dumps(topology, indent=2)}\n\n"
                        "## Identified Weaknesses\n"
                        f"{json.dumps(weaknesses, indent=2)}\n\n"
                        "Create an experiment plan."
                    ),
                },
            ],
        )

        raw = response.choices[0].message.content.strip()
        try:
            plan = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("LLM returned non-JSON plan; attempting extraction")
            start = raw.find("[")
            end = raw.rfind("]") + 1
            if start >= 0 and end > start:
                plan = json.loads(raw[start:end])
            else:
                plan = []

        # Validate that all experiment names are known
        known_names = set(registry.list_names())
        validated_plan = []
        for step in plan:
            if step.get("experiment") in known_names:
                validated_plan.append(step)
            else:
                logger.warning("Skipping unknown experiment in plan: %s", step.get("experiment"))

        logger.info("Generated plan with %d steps", len(validated_plan))
        return validated_plan
