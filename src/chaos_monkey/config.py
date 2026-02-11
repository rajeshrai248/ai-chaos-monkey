"""Pydantic configuration models and YAML loader."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class LLMConfig(BaseModel):
    model: str = "anthropic/claude-sonnet-4-5-20250929"
    temperature: float = 0.2
    api_key_env: str = "ANTHROPIC_API_KEY"


class KubernetesConfig(BaseModel):
    kubeconfig: str | None = "~/.kube/config"
    context: str | None = None


class SafetyConfig(BaseModel):
    excluded_namespaces: list[str] = Field(
        default=["kube-system", "kube-public", "kube-node-lease"]
    )
    max_blast_radius: Literal["low", "medium", "high"] = "medium"
    max_concurrent_experiments: int = 1
    require_healthy_cluster: bool = True
    dry_run: bool = False
    auto_rollback: bool = True
    rollback_timeout_seconds: int = 300


class TargetConfig(BaseModel):
    namespaces: list[str] = Field(default_factory=list)
    label_selectors: dict[str, str] = Field(default_factory=dict)


class ObserverConfig(BaseModel):
    health_check_interval_seconds: int = 5
    endpoint_timeout_seconds: int = 10
    monitor_duration_seconds: int = 60


class ReportConfig(BaseModel):
    output_dir: str = "./reports"
    format: Literal["markdown", "json"] = "markdown"


class AppConfig(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    kubernetes: KubernetesConfig = Field(default_factory=KubernetesConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    target: TargetConfig = Field(default_factory=TargetConfig)
    observer: ObserverConfig = Field(default_factory=ObserverConfig)
    report: ReportConfig = Field(default_factory=ReportConfig)


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load configuration from a YAML file, falling back to defaults."""
    if path is None:
        return AppConfig()

    config_path = Path(path).expanduser()
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path) as f:
        raw = yaml.safe_load(f) or {}

    return AppConfig.model_validate(raw)
