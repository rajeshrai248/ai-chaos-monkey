"""I/O chaos experiments: disk stress and disk fill."""

from __future__ import annotations

import logging
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


class DiskStress(ChaosExperiment):
    name = "disk-stress"
    description = "Inject disk I/O stress into a pod using stress-ng"
    category = "io"
    blast_radius = BlastRadius.MEDIUM
    reversible = True

    async def execute(self, target, namespace, k8s_client, params=None):
        params = params or {}
        duration = params.get("duration_seconds", 30)
        workers = params.get("workers", 1)

        core = client.CoreV1Api(k8s_client)
        pod = core.read_namespaced_pod(target, namespace)
        container = pod.spec.containers[0].name

        cmd = ["stress-ng", "--hdd", str(workers), "--timeout", f"{duration}s"]
        resp = stream(
            core.connect_get_namespaced_pod_exec,
            target, namespace, command=cmd, container=container,
            stderr=True, stdout=True, stdin=False, tty=False,
        )
        logger.info("Disk stress on %s/%s: %s", namespace, target, resp)

        return ExperimentResult(
            experiment_name=self.name,
            status=ExperimentStatus.COMPLETED,
            target=target, namespace=namespace,
            details={"duration": duration, "workers": workers, "output": resp},
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


class DiskFill(ChaosExperiment):
    name = "disk-fill"
    description = "Fill disk space in a pod using fallocate"
    category = "io"
    blast_radius = BlastRadius.MEDIUM
    reversible = True

    FILL_PATH = "/tmp/chaos-monkey-fill"

    async def execute(self, target, namespace, k8s_client, params=None):
        params = params or {}
        size_mb = params.get("size_mb", 100)

        core = client.CoreV1Api(k8s_client)
        pod = core.read_namespaced_pod(target, namespace)
        container = pod.spec.containers[0].name

        cmd = ["fallocate", "-l", f"{size_mb}M", self.FILL_PATH]
        resp = stream(
            core.connect_get_namespaced_pod_exec,
            target, namespace, command=cmd, container=container,
            stderr=True, stdout=True, stdin=False, tty=False,
        )
        logger.info("Disk fill %dMB on %s/%s: %s", size_mb, namespace, target, resp)

        return ExperimentResult(
            experiment_name=self.name,
            status=ExperimentStatus.COMPLETED,
            target=target, namespace=namespace,
            details={"size_mb": size_mb, "fill_path": self.FILL_PATH},
        )

    async def rollback(self, target, namespace, k8s_client, context=None):
        core = client.CoreV1Api(k8s_client)
        try:
            pod = core.read_namespaced_pod(target, namespace)
            container = pod.spec.containers[0].name
            stream(
                core.connect_get_namespaced_pod_exec,
                target, namespace,
                command=["rm", "-f", self.FILL_PATH],
                container=container,
                stderr=True, stdout=True, stdin=False, tty=False,
            )
            logger.info("Removed fill file from %s/%s", namespace, target)
        except Exception:
            logger.debug("Cleanup of fill file failed")

    def validate_target(self, target, namespace, topology):
        for ns in topology.get("namespaces", []):
            if ns["name"] == namespace:
                return any(p["name"] == target for p in ns.get("pods", []))
        return False
