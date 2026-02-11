"""Pod-level chaos experiments: kill, restart, CPU stress, memory stress."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from kubernetes import client
from kubernetes.stream import stream

from chaos_monkey.experiments.base import (
    BlastRadius,
    ChaosExperiment,
    ExperimentResult,
    ExperimentStatus,
)

logger = logging.getLogger(__name__)


class PodKill(ChaosExperiment):
    name = "pod-kill"
    description = "Delete a pod to test restart resilience"
    category = "pod"
    blast_radius = BlastRadius.LOW
    reversible = True

    async def execute(self, target, namespace, k8s_client, params=None):
        core = client.CoreV1Api(k8s_client)
        core.delete_namespaced_pod(target, namespace)
        logger.info("Killed pod %s/%s", namespace, target)
        return ExperimentResult(
            experiment_name=self.name,
            status=ExperimentStatus.COMPLETED,
            target=target,
            namespace=namespace,
            details={"action": "deleted"},
        )

    async def rollback(self, target, namespace, k8s_client, context=None):
        # Kubernetes controllers (Deployment, RS) recreate the pod automatically
        logger.info("Pod %s/%s will be recreated by its controller", namespace, target)

    def validate_target(self, target, namespace, topology):
        for ns in topology.get("namespaces", []):
            if ns["name"] == namespace:
                return any(p["name"] == target for p in ns.get("pods", []))
        return False


class PodRestart(ChaosExperiment):
    name = "pod-restart"
    description = "Restart a deployment by scaling to 0 then back"
    category = "pod"
    blast_radius = BlastRadius.MEDIUM
    reversible = True

    async def execute(self, target, namespace, k8s_client, params=None):
        apps = client.AppsV1Api(k8s_client)
        # Read current replicas
        dep = apps.read_namespaced_deployment(target, namespace)
        original_replicas = dep.spec.replicas or 1

        # Scale down
        apps.patch_namespaced_deployment_scale(
            target, namespace, {"spec": {"replicas": 0}}
        )
        logger.info("Scaled %s/%s to 0", namespace, target)

        # Scale back up
        apps.patch_namespaced_deployment_scale(
            target, namespace, {"spec": {"replicas": original_replicas}}
        )
        logger.info("Scaled %s/%s back to %d", namespace, target, original_replicas)

        return ExperimentResult(
            experiment_name=self.name,
            status=ExperimentStatus.COMPLETED,
            target=target,
            namespace=namespace,
            details={"original_replicas": original_replicas},
        )

    async def rollback(self, target, namespace, k8s_client, context=None):
        replicas = (context or {}).get("original_replicas", 1)
        apps = client.AppsV1Api(k8s_client)
        apps.patch_namespaced_deployment_scale(
            target, namespace, {"spec": {"replicas": replicas}}
        )

    def validate_target(self, target, namespace, topology):
        for ns in topology.get("namespaces", []):
            if ns["name"] == namespace:
                return any(d["name"] == target for d in ns.get("deployments", []))
        return False


class CpuStress(ChaosExperiment):
    name = "cpu-stress"
    description = "Inject CPU stress into a pod using stress-ng"
    category = "pod"
    blast_radius = BlastRadius.LOW
    reversible = True

    async def execute(self, target, namespace, k8s_client, params=None):
        params = params or {}
        duration = params.get("duration_seconds", 30)
        workers = params.get("workers", 1)

        core = client.CoreV1Api(k8s_client)
        # Get the first container name
        pod = core.read_namespaced_pod(target, namespace)
        container = pod.spec.containers[0].name

        cmd = ["stress-ng", "--cpu", str(workers), "--timeout", f"{duration}s"]
        resp = stream(
            core.connect_get_namespaced_pod_exec,
            target, namespace,
            command=cmd,
            container=container,
            stderr=True, stdout=True, stdin=False, tty=False,
        )
        logger.info("CPU stress on %s/%s: %s", namespace, target, resp)

        return ExperimentResult(
            experiment_name=self.name,
            status=ExperimentStatus.COMPLETED,
            target=target,
            namespace=namespace,
            details={"duration": duration, "workers": workers, "output": resp},
        )

    async def rollback(self, target, namespace, k8s_client, context=None):
        # stress-ng is time-bounded; kill any lingering process
        core = client.CoreV1Api(k8s_client)
        try:
            pod = core.read_namespaced_pod(target, namespace)
            container = pod.spec.containers[0].name
            stream(
                core.connect_get_namespaced_pod_exec,
                target, namespace,
                command=["pkill", "-f", "stress-ng"],
                container=container,
                stderr=True, stdout=True, stdin=False, tty=False,
            )
        except Exception:
            logger.debug("pkill stress-ng failed (process may have already exited)")

    def validate_target(self, target, namespace, topology):
        for ns in topology.get("namespaces", []):
            if ns["name"] == namespace:
                return any(p["name"] == target for p in ns.get("pods", []))
        return False


class MemoryStress(ChaosExperiment):
    name = "memory-stress"
    description = "Inject memory stress into a pod using stress-ng"
    category = "pod"
    blast_radius = BlastRadius.MEDIUM
    reversible = True

    async def execute(self, target, namespace, k8s_client, params=None):
        params = params or {}
        duration = params.get("duration_seconds", 30)
        workers = params.get("workers", 1)
        vm_bytes = params.get("vm_bytes", "256M")

        core = client.CoreV1Api(k8s_client)
        pod = core.read_namespaced_pod(target, namespace)
        container = pod.spec.containers[0].name

        cmd = [
            "stress-ng", "--vm", str(workers),
            "--vm-bytes", vm_bytes,
            "--timeout", f"{duration}s",
        ]
        resp = stream(
            core.connect_get_namespaced_pod_exec,
            target, namespace,
            command=cmd,
            container=container,
            stderr=True, stdout=True, stdin=False, tty=False,
        )
        logger.info("Memory stress on %s/%s: %s", namespace, target, resp)

        return ExperimentResult(
            experiment_name=self.name,
            status=ExperimentStatus.COMPLETED,
            target=target,
            namespace=namespace,
            details={"duration": duration, "vm_bytes": vm_bytes, "output": resp},
        )

    async def rollback(self, target, namespace, k8s_client, context=None):
        core = client.CoreV1Api(k8s_client)
        try:
            pod = core.read_namespaced_pod(target, namespace)
            container = pod.spec.containers[0].name
            stream(
                core.connect_get_namespaced_pod_exec,
                target, namespace,
                command=["pkill", "-f", "stress-ng"],
                container=container,
                stderr=True, stdout=True, stdin=False, tty=False,
            )
        except Exception:
            logger.debug("pkill stress-ng failed (process may have already exited)")

    def validate_target(self, target, namespace, topology):
        for ns in topology.get("namespaces", []):
            if ns["name"] == namespace:
                return any(p["name"] == target for p in ns.get("pods", []))
        return False
