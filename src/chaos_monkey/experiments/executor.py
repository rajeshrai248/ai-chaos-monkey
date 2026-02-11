"""Experiment execution engine with safety checks and rollback."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from kubernetes import client, config as k8s_config

from chaos_monkey.config import KubernetesConfig
from chaos_monkey.experiments.base import (
    ChaosExperiment,
    ExperimentResult,
    ExperimentStatus,
)
from chaos_monkey.experiments.registry import ExperimentRegistry
from chaos_monkey.safety.controls import SafetyController

logger = logging.getLogger(__name__)


class ExperimentExecutor:
    """Runs chaos experiments with safety validation, execution, and rollback."""

    def __init__(
        self,
        registry: ExperimentRegistry,
        safety: SafetyController,
        k8s_cfg: KubernetesConfig,
    ) -> None:
        self.registry = registry
        self.safety = safety
        self.k8s_cfg = k8s_cfg

    def _k8s_client(self) -> client.ApiClient:
        if self.k8s_cfg.kubeconfig:
            k8s_config.load_kube_config(
                config_file=self.k8s_cfg.kubeconfig,
                context=self.k8s_cfg.context,
            )
        else:
            k8s_config.load_incluster_config()
        return client.ApiClient()

    async def run_one(
        self,
        plan_entry: dict[str, Any],
        dry_run: bool = False,
    ) -> ExperimentResult:
        """Execute a single experiment from a plan entry dict."""
        exp_name = plan_entry["experiment"]
        target = plan_entry["target"]
        namespace = plan_entry.get("namespace", "default")
        params = plan_entry.get("params", {})

        exp_cls = self.registry.get(exp_name)
        if exp_cls is None:
            return ExperimentResult(
                experiment_name=exp_name,
                status=ExperimentStatus.FAILED,
                target=target,
                namespace=namespace,
                error=f"Unknown experiment type: {exp_name}",
            )

        experiment = exp_cls()

        # Safety validation
        validation = self.safety.validate_experiment(experiment, target, namespace)
        if not validation.approved:
            logger.warning("Safety rejected %s: %s", exp_name, validation.reason)
            return ExperimentResult(
                experiment_name=exp_name,
                status=ExperimentStatus.SKIPPED,
                target=target,
                namespace=namespace,
                error=f"Safety check failed: {validation.reason}",
            )

        if dry_run:
            logger.info("[DRY RUN] Would execute %s on %s/%s", exp_name, namespace, target)
            return ExperimentResult(
                experiment_name=exp_name,
                status=ExperimentStatus.COMPLETED,
                target=target,
                namespace=namespace,
                dry_run=True,
                details={"params": params, "message": "Dry run — no changes made"},
            )

        # Execute
        k8s = self._k8s_client()
        self.safety.register_active(exp_name, target, namespace)
        start = time.monotonic()
        try:
            result = await experiment.execute(target, namespace, k8s, params)
            result.duration_seconds = round(time.monotonic() - start, 2)
            result.completed_at = datetime.now(timezone.utc)
            return result
        except Exception as exc:
            logger.exception("Experiment %s failed on %s/%s", exp_name, namespace, target)
            elapsed = round(time.monotonic() - start, 2)
            # Attempt rollback
            try:
                await self.safety.enforce_rollback(experiment, target, namespace, k8s)
                rolled_back = True
            except Exception:
                logger.exception("Rollback also failed")
                rolled_back = False
            return ExperimentResult(
                experiment_name=exp_name,
                status=ExperimentStatus.FAILED,
                target=target,
                namespace=namespace,
                duration_seconds=elapsed,
                completed_at=datetime.now(timezone.utc),
                error=str(exc),
                rollback_performed=rolled_back,
            )
        finally:
            self.safety.unregister_active(exp_name, target)

    async def run_plan(
        self,
        plan: list[dict[str, Any]],
        dry_run: bool = False,
    ) -> list[ExperimentResult]:
        """Execute a full experiment plan sequentially."""
        results: list[ExperimentResult] = []
        for entry in plan:
            result = await self.run_one(entry, dry_run=dry_run)
            results.append(result)
            if result.status == ExperimentStatus.FAILED and not dry_run:
                logger.error("Stopping plan execution due to failure: %s", result.error)
                break
        return results
