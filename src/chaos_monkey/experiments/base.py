"""Abstract base class for chaos experiments."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class BlastRadius(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ExperimentStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    SKIPPED = "skipped"


@dataclass
class ExperimentResult:
    experiment_name: str
    status: ExperimentStatus
    target: str
    namespace: str
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None
    duration_seconds: float = 0.0
    observations: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    rollback_performed: bool = False
    dry_run: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "experiment_name": self.experiment_name,
            "status": self.status.value,
            "target": self.target,
            "namespace": self.namespace,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "duration_seconds": self.duration_seconds,
            "observations": self.observations,
            "error": self.error,
            "rollback_performed": self.rollback_performed,
            "dry_run": self.dry_run,
            "details": self.details,
        }


class ChaosExperiment(ABC):
    """Base class for all chaos experiments.

    Subclasses are automatically registered via the experiment registry.
    """

    name: str
    description: str
    category: str  # pod, network, node, io, app, config
    blast_radius: BlastRadius
    reversible: bool = True

    @abstractmethod
    async def execute(
        self,
        target: str,
        namespace: str,
        k8s_client: Any,
        params: dict[str, Any] | None = None,
    ) -> ExperimentResult:
        """Execute the chaos experiment against the target."""
        ...

    @abstractmethod
    async def rollback(
        self,
        target: str,
        namespace: str,
        k8s_client: Any,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Rollback / undo the chaos experiment."""
        ...

    @abstractmethod
    def validate_target(
        self, target: str, namespace: str, topology: dict
    ) -> bool:
        """Check whether the target is valid for this experiment."""
        ...

    def info(self) -> dict[str, Any]:
        """Return metadata about this experiment type."""
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "blast_radius": self.blast_radius.value,
            "reversible": self.reversible,
        }
