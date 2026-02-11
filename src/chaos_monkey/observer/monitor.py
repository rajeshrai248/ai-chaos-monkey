"""Health checks, pod status, and endpoint monitoring."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx
from kubernetes import client, config as k8s_config

from chaos_monkey.config import KubernetesConfig, ObserverConfig

logger = logging.getLogger(__name__)


class HealthMonitor:
    """Monitors cluster and application health during chaos experiments."""

    def __init__(self, k8s_cfg: KubernetesConfig, observer_cfg: ObserverConfig) -> None:
        self.k8s_cfg = k8s_cfg
        self.observer_cfg = observer_cfg
        self._stop_event = asyncio.Event()

    def _core_api(self) -> client.CoreV1Api:
        if self.k8s_cfg.kubeconfig:
            k8s_config.load_kube_config(
                config_file=self.k8s_cfg.kubeconfig,
                context=self.k8s_cfg.context,
            )
        else:
            k8s_config.load_incluster_config()
        return client.CoreV1Api()

    async def check_pod_health(self, namespace: str, label_selector: str = "") -> list[dict]:
        """Return health status of pods in a namespace."""
        core = self._core_api()
        pods = core.list_namespaced_pod(namespace, label_selector=label_selector)
        results = []
        for p in pods.items:
            statuses = p.status.container_statuses or []
            results.append({
                "name": p.metadata.name,
                "namespace": namespace,
                "phase": p.status.phase,
                "ready": all(cs.ready for cs in statuses) if statuses else False,
                "restart_count": sum(cs.restart_count for cs in statuses),
                "conditions": [
                    {"type": c.type, "status": c.status}
                    for c in (p.status.conditions or [])
                ],
            })
        return results

    async def check_endpoint(self, url: str) -> dict[str, Any]:
        """Probe an HTTP endpoint and return latency/status."""
        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self.observer_cfg.endpoint_timeout_seconds) as c:
                resp = await c.get(url)
            elapsed = time.monotonic() - start
            return {
                "url": url,
                "status_code": resp.status_code,
                "latency_ms": round(elapsed * 1000, 1),
                "healthy": 200 <= resp.status_code < 500,
            }
        except httpx.RequestError as exc:
            elapsed = time.monotonic() - start
            return {
                "url": url,
                "status_code": None,
                "latency_ms": round(elapsed * 1000, 1),
                "healthy": False,
                "error": str(exc),
            }

    async def monitor_during_experiment(
        self,
        namespace: str,
        label_selector: str = "",
        endpoints: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Continuously monitor health for the configured duration, returning all observations."""
        observations: list[dict[str, Any]] = []
        self._stop_event.clear()
        interval = self.observer_cfg.health_check_interval_seconds
        duration = self.observer_cfg.monitor_duration_seconds
        deadline = time.monotonic() + duration

        while time.monotonic() < deadline and not self._stop_event.is_set():
            ts = time.time()

            pod_health = await self.check_pod_health(namespace, label_selector)
            observation: dict[str, Any] = {
                "timestamp": ts,
                "pods": pod_health,
            }

            if endpoints:
                endpoint_results = await asyncio.gather(
                    *(self.check_endpoint(url) for url in endpoints)
                )
                observation["endpoints"] = list(endpoint_results)

            observations.append(observation)
            await asyncio.sleep(interval)

        return observations

    def stop(self) -> None:
        """Signal the monitor to stop early."""
        self._stop_event.set()

    async def get_cluster_health_summary(self, namespaces: list[str]) -> dict[str, Any]:
        """Quick cluster-wide health snapshot."""
        core = self._core_api()
        summary: dict[str, Any] = {"namespaces": {}, "healthy": True}

        for ns in namespaces:
            pods = core.list_namespaced_pod(ns)
            total = len(pods.items)
            not_ready = sum(
                1 for p in pods.items
                if not all(
                    cs.ready for cs in (p.status.container_statuses or [])
                )
            )
            ns_healthy = not_ready == 0
            summary["namespaces"][ns] = {
                "total_pods": total,
                "not_ready": not_ready,
                "healthy": ns_healthy,
            }
            if not ns_healthy:
                summary["healthy"] = False

        return summary
