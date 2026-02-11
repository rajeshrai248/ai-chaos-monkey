"""Node-level chaos experiments: drain and cordon."""

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


class NodeDrain(ChaosExperiment):
    name = "node-drain"
    description = "Drain a node to evict all pods"
    category = "node"
    blast_radius = BlastRadius.HIGH
    reversible = True

    async def execute(self, target, namespace, k8s_client, params=None):
        core = client.CoreV1Api(k8s_client)

        # Cordon the node first
        body = {"spec": {"unschedulable": True}}
        core.patch_node(target, body)
        logger.info("Cordoned node %s", target)

        # Evict pods (excluding daemonsets and mirror pods)
        pods = core.list_pod_for_all_namespaces(field_selector=f"spec.nodeName={target}")
        evicted = []
        for pod in pods.items:
            # Skip daemonset-managed pods and mirror pods
            owner_refs = pod.metadata.owner_references or []
            if any(ref.kind == "DaemonSet" for ref in owner_refs):
                continue
            if pod.metadata.annotations and "kubernetes.io/config.mirror" in pod.metadata.annotations:
                continue

            eviction = client.V1Eviction(
                metadata=client.V1ObjectMeta(
                    name=pod.metadata.name,
                    namespace=pod.metadata.namespace,
                ),
            )
            try:
                core.create_namespaced_pod_eviction(
                    pod.metadata.name, pod.metadata.namespace, eviction
                )
                evicted.append(f"{pod.metadata.namespace}/{pod.metadata.name}")
            except client.ApiException as e:
                logger.warning("Could not evict %s/%s: %s",
                               pod.metadata.namespace, pod.metadata.name, e.reason)

        logger.info("Drained node %s, evicted %d pods", target, len(evicted))
        return ExperimentResult(
            experiment_name=self.name,
            status=ExperimentStatus.COMPLETED,
            target=target, namespace=namespace,
            details={"evicted_pods": evicted},
        )

    async def rollback(self, target, namespace, k8s_client, context=None):
        core = client.CoreV1Api(k8s_client)
        body = {"spec": {"unschedulable": False}}
        core.patch_node(target, body)
        logger.info("Uncordoned node %s", target)

    def validate_target(self, target, namespace, topology):
        # Target is a node name; we check if any pod runs on it
        for ns in topology.get("namespaces", []):
            for pod in ns.get("pods", []):
                if pod.get("node") == target:
                    return True
        return False


class NodeCordon(ChaosExperiment):
    name = "node-cordon"
    description = "Cordon a node to prevent new pod scheduling"
    category = "node"
    blast_radius = BlastRadius.MEDIUM
    reversible = True

    async def execute(self, target, namespace, k8s_client, params=None):
        core = client.CoreV1Api(k8s_client)
        body = {"spec": {"unschedulable": True}}
        core.patch_node(target, body)
        logger.info("Cordoned node %s", target)

        return ExperimentResult(
            experiment_name=self.name,
            status=ExperimentStatus.COMPLETED,
            target=target, namespace=namespace,
            details={"action": "cordoned"},
        )

    async def rollback(self, target, namespace, k8s_client, context=None):
        core = client.CoreV1Api(k8s_client)
        body = {"spec": {"unschedulable": False}}
        core.patch_node(target, body)
        logger.info("Uncordoned node %s", target)

    def validate_target(self, target, namespace, topology):
        for ns in topology.get("namespaces", []):
            for pod in ns.get("pods", []):
                if pod.get("node") == target:
                    return True
        return False
