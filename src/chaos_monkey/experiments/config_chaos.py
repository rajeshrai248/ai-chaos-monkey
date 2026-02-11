"""Configuration chaos experiments: ConfigMap mutation and Secret deletion."""

from __future__ import annotations

import copy
import json
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


class ConfigMapMutation(ChaosExperiment):
    name = "configmap-mutation"
    description = "Mutate a ConfigMap to test configuration resilience"
    category = "config"
    blast_radius = BlastRadius.MEDIUM
    reversible = True

    async def execute(self, target, namespace, k8s_client, params=None):
        params = params or {}
        key = params.get("key")
        value = params.get("value", "CHAOS_MONKEY_MUTATED")

        core = client.CoreV1Api(k8s_client)
        cm = core.read_namespaced_config_map(target, namespace)

        # Backup original data
        original_data = copy.deepcopy(cm.data or {})

        if key and key in cm.data:
            cm.data[key] = value
        elif cm.data:
            # Mutate the first key
            first_key = next(iter(cm.data))
            cm.data[first_key] = value
            key = first_key
        else:
            return ExperimentResult(
                experiment_name=self.name,
                status=ExperimentStatus.SKIPPED,
                target=target, namespace=namespace,
                error="ConfigMap has no data keys to mutate",
            )

        core.patch_namespaced_config_map(target, namespace, cm)
        logger.info("Mutated ConfigMap %s/%s key=%s", namespace, target, key)

        return ExperimentResult(
            experiment_name=self.name,
            status=ExperimentStatus.COMPLETED,
            target=target, namespace=namespace,
            details={"mutated_key": key, "original_data": original_data},
        )

    async def rollback(self, target, namespace, k8s_client, context=None):
        original_data = (context or {}).get("original_data")
        if not original_data:
            logger.warning("No original data to restore for ConfigMap %s/%s", namespace, target)
            return
        core = client.CoreV1Api(k8s_client)
        cm = core.read_namespaced_config_map(target, namespace)
        cm.data = original_data
        core.patch_namespaced_config_map(target, namespace, cm)
        logger.info("Restored ConfigMap %s/%s", namespace, target)

    def validate_target(self, target, namespace, topology):
        for ns in topology.get("namespaces", []):
            if ns["name"] == namespace:
                return any(cm["name"] == target for cm in ns.get("configmaps", []))
        return False


class SecretDeletion(ChaosExperiment):
    name = "secret-deletion"
    description = "Delete a Secret to test secret-loss resilience"
    category = "config"
    blast_radius = BlastRadius.HIGH
    reversible = True

    async def execute(self, target, namespace, k8s_client, params=None):
        core = client.CoreV1Api(k8s_client)

        # Backup the secret before deletion
        secret = core.read_namespaced_secret(target, namespace)
        backup = {
            "metadata": {
                "name": secret.metadata.name,
                "namespace": secret.metadata.namespace,
                "labels": secret.metadata.labels,
                "annotations": secret.metadata.annotations,
            },
            "type": secret.type,
            "data": secret.data,
        }

        core.delete_namespaced_secret(target, namespace)
        logger.info("Deleted Secret %s/%s", namespace, target)

        return ExperimentResult(
            experiment_name=self.name,
            status=ExperimentStatus.COMPLETED,
            target=target, namespace=namespace,
            details={"backup": backup},
        )

    async def rollback(self, target, namespace, k8s_client, context=None):
        backup = (context or {}).get("backup")
        if not backup:
            logger.warning("No backup to restore for Secret %s/%s", namespace, target)
            return

        core = client.CoreV1Api(k8s_client)
        secret = client.V1Secret(
            metadata=client.V1ObjectMeta(
                name=backup["metadata"]["name"],
                namespace=backup["metadata"]["namespace"],
                labels=backup["metadata"].get("labels"),
                annotations=backup["metadata"].get("annotations"),
            ),
            type=backup.get("type", "Opaque"),
            data=backup.get("data"),
        )
        core.create_namespaced_secret(namespace, secret)
        logger.info("Restored Secret %s/%s", namespace, target)

    def validate_target(self, target, namespace, topology):
        for ns in topology.get("namespaces", []):
            if ns["name"] == namespace:
                return target in ns.get("secrets", [])
        return False
