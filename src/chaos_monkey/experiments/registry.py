"""Auto-discovery experiment plugin registry."""

from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import Any

from chaos_monkey.experiments.base import ChaosExperiment

logger = logging.getLogger(__name__)


class ExperimentRegistry:
    """Discovers and provides access to all ChaosExperiment subclasses.

    Experiment modules are auto-imported from the `chaos_monkey.experiments` package.
    Any concrete subclass of ChaosExperiment is automatically available.
    """

    _discovered = False

    def __init__(self) -> None:
        self._ensure_discovered()

    @classmethod
    def _ensure_discovered(cls) -> None:
        """Import all modules in the experiments package to trigger class registration."""
        if cls._discovered:
            return
        import chaos_monkey.experiments as pkg

        for _importer, modname, _ispkg in pkgutil.iter_modules(pkg.__path__):
            if modname in ("base", "registry", "executor"):
                continue
            try:
                importlib.import_module(f"chaos_monkey.experiments.{modname}")
            except Exception:
                logger.exception("Failed to import experiment module: %s", modname)
        cls._discovered = True

    def _all_subclasses(self) -> list[type[ChaosExperiment]]:
        """Recursively collect all concrete subclasses of ChaosExperiment."""
        result: list[type[ChaosExperiment]] = []
        stack = list(ChaosExperiment.__subclasses__())
        while stack:
            cls = stack.pop()
            if not getattr(cls, "__abstractmethods__", set()):
                result.append(cls)
            stack.extend(cls.__subclasses__())
        return result

    def get(self, name: str) -> type[ChaosExperiment] | None:
        """Look up an experiment class by its name attribute."""
        for cls in self._all_subclasses():
            if cls.name == name:
                return cls
        return None

    def list_all(self) -> list[dict[str, Any]]:
        """Return info dicts for every registered experiment."""
        return [cls().info() for cls in self._all_subclasses()]

    def list_names(self) -> list[str]:
        return [cls.name for cls in self._all_subclasses()]
