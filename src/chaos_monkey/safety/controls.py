"""Safety controls for chaos experiments — blast radius, dry-run, rollback, exclusions."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from chaos_monkey.config import SafetyConfig
from chaos_monkey.experiments.base import BlastRadius, ChaosExperiment

logger = logging.getLogger(__name__)

BLAST_RADIUS_ORDER = {
    BlastRadius.LOW: 0,
    BlastRadius.MEDIUM: 1,
    BlastRadius.HIGH: 2,
}


@dataclass
class ValidationResult:
    approved: bool
    reason: str


class SafetyController:
    """Enforces safety rules before, during, and after chaos experiments."""

    def __init__(self, config: SafetyConfig) -> None:
        self.config = config
        self._active_experiments: list[dict[str, Any]] = []

    @property
    def excluded_namespaces(self) -> list[str]:
        return self.config.excluded_namespaces

    @property
    def dry_run(self) -> bool:
        return self.config.dry_run

    def validate_experiment(
        self,
        experiment: ChaosExperiment,
        target: str,
        namespace: str,
    ) -> ValidationResult:
        """Validate whether an experiment is safe to execute."""
        # Check excluded namespaces
        if namespace in self.config.excluded_namespaces:
            return ValidationResult(
                approved=False,
                reason=f"Namespace '{namespace}' is in the exclusion list",
            )

        # Check blast radius
        max_allowed = BlastRadius(self.config.max_blast_radius)
        if BLAST_RADIUS_ORDER[experiment.blast_radius] > BLAST_RADIUS_ORDER[max_allowed]:
            return ValidationResult(
                approved=False,
                reason=(
                    f"Experiment blast radius '{experiment.blast_radius.value}' "
                    f"exceeds maximum allowed '{max_allowed.value}'"
                ),
            )

        # Check concurrent experiment limit
        if len(self._active_experiments) >= self.config.max_concurrent_experiments:
            return ValidationResult(
                approved=False,
                reason=(
                    f"Concurrent experiment limit reached "
                    f"({self.config.max_concurrent_experiments})"
                ),
            )

        return ValidationResult(approved=True, reason="All safety checks passed")

    def register_active(self, experiment_name: str, target: str, namespace: str) -> None:
        self._active_experiments.append({
            "name": experiment_name,
            "target": target,
            "namespace": namespace,
        })

    def unregister_active(self, experiment_name: str, target: str) -> None:
        self._active_experiments = [
            e for e in self._active_experiments
            if not (e["name"] == experiment_name and e["target"] == target)
        ]

    async def enforce_rollback(
        self,
        experiment: ChaosExperiment,
        target: str,
        namespace: str,
        k8s_client: Any,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Force rollback of an experiment with a timeout."""
        if not self.config.auto_rollback:
            logger.warning("Auto-rollback is disabled; skipping rollback for %s", experiment.name)
            return

        try:
            await asyncio.wait_for(
                experiment.rollback(target, namespace, k8s_client, context),
                timeout=self.config.rollback_timeout_seconds,
            )
            logger.info("Rollback completed for %s on %s/%s", experiment.name, namespace, target)
        except asyncio.TimeoutError:
            logger.error(
                "Rollback timed out after %ds for %s on %s/%s",
                self.config.rollback_timeout_seconds,
                experiment.name,
                namespace,
                target,
            )
        except Exception:
            logger.exception("Rollback failed for %s on %s/%s", experiment.name, namespace, target)
        finally:
            self.unregister_active(experiment.name, target)

    def validate_plan(self, plan: list[dict[str, Any]]) -> list[ValidationResult]:
        """Validate an entire experiment plan, returning results per experiment entry."""
        from chaos_monkey.experiments.registry import ExperimentRegistry

        registry = ExperimentRegistry()
        results: list[ValidationResult] = []
        for entry in plan:
            exp_cls = registry.get(entry.get("experiment", ""))
            if exp_cls is None:
                results.append(ValidationResult(
                    approved=False,
                    reason=f"Unknown experiment type: {entry.get('experiment')}",
                ))
                continue
            experiment = exp_cls()
            results.append(
                self.validate_experiment(
                    experiment,
                    entry.get("target", ""),
                    entry.get("namespace", "default"),
                )
            )
        return results
