"""Network chaos experiments: latency, packet loss, partition, DNS failure."""

from __future__ import annotations

import json
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


class LatencyInjection(ChaosExperiment):
    name = "network-latency"
    description = "Inject network latency into a pod using tc netem"
    category = "network"
    blast_radius = BlastRadius.MEDIUM
    reversible = True

    async def execute(self, target, namespace, k8s_client, params=None):
        params = params or {}
        delay_ms = params.get("delay_ms", 200)
        jitter_ms = params.get("jitter_ms", 50)
        interface = params.get("interface", "eth0")
        duration = params.get("duration_seconds", 30)

        core = client.CoreV1Api(k8s_client)
        pod = core.read_namespaced_pod(target, namespace)
        container = pod.spec.containers[0].name

        cmd = [
            "tc", "qdisc", "add", "dev", interface, "root", "netem",
            "delay", f"{delay_ms}ms", f"{jitter_ms}ms",
        ]
        resp = stream(
            core.connect_get_namespaced_pod_exec,
            target, namespace, command=cmd, container=container,
            stderr=True, stdout=True, stdin=False, tty=False,
        )
        logger.info("Latency injection on %s/%s (%dms ±%dms): %s",
                     namespace, target, delay_ms, jitter_ms, resp)

        return ExperimentResult(
            experiment_name=self.name,
            status=ExperimentStatus.COMPLETED,
            target=target, namespace=namespace,
            details={"delay_ms": delay_ms, "jitter_ms": jitter_ms,
                      "interface": interface, "duration": duration},
        )

    async def rollback(self, target, namespace, k8s_client, context=None):
        interface = (context or {}).get("interface", "eth0")
        core = client.CoreV1Api(k8s_client)
        pod = core.read_namespaced_pod(target, namespace)
        container = pod.spec.containers[0].name
        stream(
            core.connect_get_namespaced_pod_exec,
            target, namespace,
            command=["tc", "qdisc", "del", "dev", interface, "root"],
            container=container,
            stderr=True, stdout=True, stdin=False, tty=False,
        )

    def validate_target(self, target, namespace, topology):
        for ns in topology.get("namespaces", []):
            if ns["name"] == namespace:
                return any(p["name"] == target for p in ns.get("pods", []))
        return False


class PacketLoss(ChaosExperiment):
    name = "packet-loss"
    description = "Inject packet loss into a pod using tc netem"
    category = "network"
    blast_radius = BlastRadius.MEDIUM
    reversible = True

    async def execute(self, target, namespace, k8s_client, params=None):
        params = params or {}
        loss_percent = params.get("loss_percent", 10)
        interface = params.get("interface", "eth0")

        core = client.CoreV1Api(k8s_client)
        pod = core.read_namespaced_pod(target, namespace)
        container = pod.spec.containers[0].name

        cmd = [
            "tc", "qdisc", "add", "dev", interface, "root", "netem",
            "loss", f"{loss_percent}%",
        ]
        resp = stream(
            core.connect_get_namespaced_pod_exec,
            target, namespace, command=cmd, container=container,
            stderr=True, stdout=True, stdin=False, tty=False,
        )
        logger.info("Packet loss %d%% on %s/%s: %s",
                     loss_percent, namespace, target, resp)

        return ExperimentResult(
            experiment_name=self.name,
            status=ExperimentStatus.COMPLETED,
            target=target, namespace=namespace,
            details={"loss_percent": loss_percent, "interface": interface},
        )

    async def rollback(self, target, namespace, k8s_client, context=None):
        interface = (context or {}).get("interface", "eth0")
        core = client.CoreV1Api(k8s_client)
        pod = core.read_namespaced_pod(target, namespace)
        container = pod.spec.containers[0].name
        stream(
            core.connect_get_namespaced_pod_exec,
            target, namespace,
            command=["tc", "qdisc", "del", "dev", interface, "root"],
            container=container,
            stderr=True, stdout=True, stdin=False, tty=False,
        )

    def validate_target(self, target, namespace, topology):
        for ns in topology.get("namespaces", []):
            if ns["name"] == namespace:
                return any(p["name"] == target for p in ns.get("pods", []))
        return False


class NetworkPartition(ChaosExperiment):
    name = "network-partition"
    description = "Create a network partition using NetworkPolicy"
    category = "network"
    blast_radius = BlastRadius.HIGH
    reversible = True

    POLICY_NAME = "chaos-monkey-partition"

    async def execute(self, target, namespace, k8s_client, params=None):
        params = params or {}
        target_labels = params.get("target_labels", {"app": target})

        networking = client.NetworkingV1Api(k8s_client)
        policy = client.V1NetworkPolicy(
            metadata=client.V1ObjectMeta(name=self.POLICY_NAME, namespace=namespace),
            spec=client.V1NetworkPolicySpec(
                pod_selector=client.V1LabelSelector(match_labels=target_labels),
                policy_types=["Ingress", "Egress"],
                ingress=[],   # deny all ingress
                egress=[],    # deny all egress
            ),
        )
        networking.create_namespaced_network_policy(namespace, policy)
        logger.info("Network partition applied to %s in %s", target_labels, namespace)

        return ExperimentResult(
            experiment_name=self.name,
            status=ExperimentStatus.COMPLETED,
            target=target, namespace=namespace,
            details={"policy_name": self.POLICY_NAME, "target_labels": target_labels},
        )

    async def rollback(self, target, namespace, k8s_client, context=None):
        networking = client.NetworkingV1Api(k8s_client)
        try:
            networking.delete_namespaced_network_policy(self.POLICY_NAME, namespace)
            logger.info("Removed network partition policy from %s", namespace)
        except client.ApiException as e:
            if e.status != 404:
                raise

    def validate_target(self, target, namespace, topology):
        for ns in topology.get("namespaces", []):
            if ns["name"] == namespace:
                return len(ns.get("pods", [])) > 0
        return False


class DnsFailure(ChaosExperiment):
    name = "dns-failure"
    description = "Simulate DNS failure by modifying CoreDNS configmap"
    category = "network"
    blast_radius = BlastRadius.HIGH
    reversible = True

    async def execute(self, target, namespace, k8s_client, params=None):
        core = client.CoreV1Api(k8s_client)

        # Backup original CoreDNS config
        cm = core.read_namespaced_config_map("coredns", "kube-system")
        original_corefile = cm.data.get("Corefile", "")

        # Inject a rewrite rule that breaks resolution for the target
        broken_corefile = original_corefile.replace(
            "ready",
            f'ready\n        rewrite name {target}.{namespace}.svc.cluster.local nxdomain.invalid',
        )
        cm.data["Corefile"] = broken_corefile
        core.patch_namespaced_config_map("coredns", "kube-system", cm)
        logger.info("DNS failure injected for %s.%s", target, namespace)

        return ExperimentResult(
            experiment_name=self.name,
            status=ExperimentStatus.COMPLETED,
            target=target, namespace=namespace,
            details={"original_corefile": original_corefile},
        )

    async def rollback(self, target, namespace, k8s_client, context=None):
        original = (context or {}).get("original_corefile")
        if not original:
            logger.warning("No original CoreDNS config to restore")
            return
        core = client.CoreV1Api(k8s_client)
        cm = core.read_namespaced_config_map("coredns", "kube-system")
        cm.data["Corefile"] = original
        core.patch_namespaced_config_map("coredns", "kube-system", cm)
        logger.info("CoreDNS config restored")

    def validate_target(self, target, namespace, topology):
        # Target should be a service name
        for ns in topology.get("namespaces", []):
            if ns["name"] == namespace:
                return any(s["name"] == target for s in ns.get("services", []))
        return False
