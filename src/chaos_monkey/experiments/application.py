"""Application-level chaos experiments: HTTP error injection."""

from __future__ import annotations

import logging
from typing import Any

from kubernetes import client

from chaos_monkey.experiments.base import (
    BlastRadius,
    ChaosExperiment,
    ExperimentResult,
    ExperimentStatus,
)

logger = logging.getLogger(__name__)


class HttpErrorInjection(ChaosExperiment):
    name = "http-error-injection"
    description = "Inject HTTP errors by setting error-triggering env vars on a deployment"
    category = "app"
    blast_radius = BlastRadius.LOW
    reversible = True

    ENV_NAME = "CHAOS_HTTP_ERROR_RATE"

    async def execute(self, target, namespace, k8s_client, params=None):
        params = params or {}
        error_rate = params.get("error_rate", "0.5")
        error_code = params.get("error_code", "500")

        apps = client.AppsV1Api(k8s_client)
        dep = apps.read_namespaced_deployment(target, namespace)

        # Store original env for rollback
        original_env = []
        container = dep.spec.template.spec.containers[0]
        if container.env:
            original_env = [
                {"name": e.name, "value": e.value}
                for e in container.env
            ]

        # Patch env vars to trigger error injection
        env_patch = [
            {"name": self.ENV_NAME, "value": str(error_rate)},
            {"name": "CHAOS_HTTP_ERROR_CODE", "value": str(error_code)},
        ]
        patch_body = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [{
                            "name": container.name,
                            "env": (container.env or []) + [
                                client.V1EnvVar(name=e["name"], value=e["value"])
                                for e in env_patch
                            ],
                        }],
                    }
                }
            }
        }
        apps.patch_namespaced_deployment(target, namespace, patch_body)
        logger.info("HTTP error injection on %s/%s: rate=%s code=%s",
                     namespace, target, error_rate, error_code)

        return ExperimentResult(
            experiment_name=self.name,
            status=ExperimentStatus.COMPLETED,
            target=target, namespace=namespace,
            details={
                "error_rate": error_rate,
                "error_code": error_code,
                "original_env": original_env,
            },
        )

    async def rollback(self, target, namespace, k8s_client, context=None):
        apps = client.AppsV1Api(k8s_client)
        dep = apps.read_namespaced_deployment(target, namespace)
        container = dep.spec.template.spec.containers[0]

        # Remove chaos env vars
        if container.env:
            clean_env = [
                e for e in container.env
                if e.name not in (self.ENV_NAME, "CHAOS_HTTP_ERROR_CODE")
            ]
            patch_body = {
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [{
                                "name": container.name,
                                "env": clean_env if clean_env else None,
                            }],
                        }
                    }
                }
            }
            apps.patch_namespaced_deployment(target, namespace, patch_body)
            logger.info("Removed HTTP error injection from %s/%s", namespace, target)

    def validate_target(self, target, namespace, topology):
        for ns in topology.get("namespaces", []):
            if ns["name"] == namespace:
                return any(d["name"] == target for d in ns.get("deployments", []))
        return False
