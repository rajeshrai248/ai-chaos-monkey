"""Kubernetes cluster topology discovery."""

from __future__ import annotations

import logging
from typing import Any

from kubernetes import client, config as k8s_config

from chaos_monkey.config import KubernetesConfig, SafetyConfig, TargetConfig

logger = logging.getLogger(__name__)


def _load_k8s(cfg: KubernetesConfig) -> client.ApiClient:
    """Load kubernetes configuration and return an API client."""
    if cfg.kubeconfig:
        k8s_config.load_kube_config(
            config_file=cfg.kubeconfig,
            context=cfg.context,
        )
    else:
        k8s_config.load_incluster_config()
    return client.ApiClient()


class ClusterDiscovery:
    """Discovers Kubernetes cluster topology: namespaces, deployments, pods, services, etc."""

    def __init__(
        self,
        k8s_cfg: KubernetesConfig,
        target_cfg: TargetConfig,
        safety_cfg: SafetyConfig,
    ) -> None:
        self.k8s_cfg = k8s_cfg
        self.target_cfg = target_cfg
        self.safety_cfg = safety_cfg

    async def discover(self) -> dict[str, Any]:
        """Discover full cluster topology and return structured data."""
        api_client = _load_k8s(self.k8s_cfg)
        core = client.CoreV1Api(api_client)
        apps = client.AppsV1Api(api_client)
        networking = client.NetworkingV1Api(api_client)

        namespaces = self._get_target_namespaces(core)
        topology: dict[str, Any] = {
            "namespaces": [],
            "summary": {},
        }

        total_pods = 0
        total_deployments = 0
        total_services = 0

        for ns in namespaces:
            ns_data = await self._discover_namespace(core, apps, networking, ns)
            topology["namespaces"].append(ns_data)
            total_pods += len(ns_data.get("pods", []))
            total_deployments += len(ns_data.get("deployments", []))
            total_services += len(ns_data.get("services", []))

        topology["summary"] = {
            "namespace_count": len(namespaces),
            "total_pods": total_pods,
            "total_deployments": total_deployments,
            "total_services": total_services,
        }

        logger.info(
            "Discovered %d namespaces, %d pods, %d deployments, %d services",
            len(namespaces), total_pods, total_deployments, total_services,
        )
        return topology

    def _get_target_namespaces(self, core: client.CoreV1Api) -> list[str]:
        """Return namespaces to scan, respecting target and safety config."""
        if self.target_cfg.namespaces:
            return [
                ns for ns in self.target_cfg.namespaces
                if ns not in self.safety_cfg.excluded_namespaces
            ]

        all_ns = core.list_namespace()
        return [
            ns.metadata.name
            for ns in all_ns.items
            if ns.metadata.name not in self.safety_cfg.excluded_namespaces
        ]

    async def _discover_namespace(
        self,
        core: client.CoreV1Api,
        apps: client.AppsV1Api,
        networking: client.NetworkingV1Api,
        namespace: str,
    ) -> dict[str, Any]:
        """Discover resources within a single namespace."""
        label_selector = ",".join(
            f"{k}={v}" for k, v in self.target_cfg.label_selectors.items()
        ) if self.target_cfg.label_selectors else ""

        pods = self._list_pods(core, namespace, label_selector)
        deployments = self._list_deployments(apps, namespace, label_selector)
        services = self._list_services(core, namespace, label_selector)
        statefulsets = self._list_statefulsets(apps, namespace, label_selector)
        ingresses = self._list_ingresses(networking, namespace)
        configmaps = self._list_configmaps(core, namespace)
        secrets = self._list_secret_names(core, namespace)

        return {
            "name": namespace,
            "pods": pods,
            "deployments": deployments,
            "services": services,
            "statefulsets": statefulsets,
            "ingresses": ingresses,
            "configmaps": configmaps,
            "secrets": secrets,
        }

    # ── resource listing helpers ────────────────────────────────────────

    def _list_pods(self, core: client.CoreV1Api, ns: str, selector: str) -> list[dict]:
        items = core.list_namespaced_pod(ns, label_selector=selector).items
        return [
            {
                "name": p.metadata.name,
                "status": p.status.phase,
                "node": p.spec.node_name,
                "labels": p.metadata.labels or {},
                "containers": [
                    {
                        "name": c.name,
                        "image": c.image,
                        "resources": {
                            "requests": (c.resources.requests or {}) if c.resources else {},
                            "limits": (c.resources.limits or {}) if c.resources else {},
                        },
                    }
                    for c in (p.spec.containers or [])
                ],
                "restart_count": sum(
                    cs.restart_count for cs in (p.status.container_statuses or [])
                ),
                "ready": all(
                    cs.ready for cs in (p.status.container_statuses or [])
                ) if p.status.container_statuses else False,
            }
            for p in items
        ]

    def _list_deployments(self, apps: client.AppsV1Api, ns: str, selector: str) -> list[dict]:
        items = apps.list_namespaced_deployment(ns, label_selector=selector).items
        return [
            {
                "name": d.metadata.name,
                "replicas": d.spec.replicas,
                "ready_replicas": d.status.ready_replicas or 0,
                "strategy": d.spec.strategy.type if d.spec.strategy else "RollingUpdate",
                "labels": d.metadata.labels or {},
                "selector": d.spec.selector.match_labels or {},
                "has_pdb": False,  # enriched later if PDB exists
            }
            for d in items
        ]

    def _list_services(self, core: client.CoreV1Api, ns: str, selector: str) -> list[dict]:
        items = core.list_namespaced_service(ns, label_selector=selector).items
        return [
            {
                "name": s.metadata.name,
                "type": s.spec.type,
                "cluster_ip": s.spec.cluster_ip,
                "ports": [
                    {"port": p.port, "target_port": p.target_port, "protocol": p.protocol}
                    for p in (s.spec.ports or [])
                ],
                "selector": s.spec.selector or {},
            }
            for s in items
        ]

    def _list_statefulsets(self, apps: client.AppsV1Api, ns: str, selector: str) -> list[dict]:
        items = apps.list_namespaced_stateful_set(ns, label_selector=selector).items
        return [
            {
                "name": ss.metadata.name,
                "replicas": ss.spec.replicas,
                "ready_replicas": ss.status.ready_replicas or 0,
                "service_name": ss.spec.service_name,
            }
            for ss in items
        ]

    def _list_ingresses(self, networking: client.NetworkingV1Api, ns: str) -> list[dict]:
        items = networking.list_namespaced_ingress(ns).items
        return [
            {
                "name": ing.metadata.name,
                "rules": [
                    {
                        "host": rule.host,
                        "paths": [
                            p.path for p in (rule.http.paths if rule.http else [])
                        ],
                    }
                    for rule in (ing.spec.rules or [])
                ],
            }
            for ing in items
        ]

    def _list_configmaps(self, core: client.CoreV1Api, ns: str) -> list[dict]:
        items = core.list_namespaced_config_map(ns).items
        return [
            {"name": cm.metadata.name, "keys": list((cm.data or {}).keys())}
            for cm in items
        ]

    def _list_secret_names(self, core: client.CoreV1Api, ns: str) -> list[str]:
        items = core.list_namespaced_secret(ns).items
        return [s.metadata.name for s in items]
