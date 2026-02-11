"""Resilience report generation — markdown and JSON formats."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import litellm

from chaos_monkey.config import LLMConfig, ReportConfig

logger = logging.getLogger(__name__)

REPORT_SYSTEM_PROMPT = """\
You are a Kubernetes resilience report writer. Given chaos experiment results, \
write a clear, actionable resilience report in Markdown format.

Structure:
1. **Executive Summary** — one paragraph overview
2. **Cluster Overview** — topology summary
3. **Weaknesses Identified** — bulleted list with severity
4. **Experiments Executed** — table of experiment name, target, status, duration
5. **Observations** — key findings during experiments
6. **Recommendations** — prioritized list of resilience improvements
7. **Risk Score** — overall resilience rating (1-10) with justification

Be specific, cite resource names, and provide actionable recommendations."""


class ReportGenerator:
    """Generates resilience reports in markdown or JSON."""

    def __init__(self, report_config: ReportConfig, llm_config: LLMConfig) -> None:
        self.report_config = report_config
        self.llm_config = llm_config

    async def generate(self, data: dict[str, Any]) -> dict[str, Any]:
        """Generate a report from experiment data. Returns dict with 'content' and 'file'."""
        if self.report_config.format == "json":
            return self._generate_json(data)
        return await self._generate_markdown(data)

    async def _generate_markdown(self, data: dict[str, Any]) -> dict[str, Any]:
        """Use the LLM to create a markdown resilience report."""
        compact_data = self._compact_for_llm(data)

        response = await litellm.acompletion(
            model=self.llm_config.model,
            temperature=self.llm_config.temperature,
            messages=[
                {"role": "system", "content": REPORT_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        "Generate a resilience report from the following "
                        "chaos engineering session:\n\n"
                        f"{json.dumps(compact_data, indent=2, default=str)}"
                    ),
                },
            ],
        )

        content = response.choices[0].message.content.strip()
        file_path = self._write_report(content, "md")
        logger.info("Markdown report written to %s", file_path)
        return {"content": content, "file": file_path, "format": "markdown"}

    def _generate_json(self, data: dict[str, Any]) -> dict[str, Any]:
        """Produce a structured JSON report (no LLM needed)."""
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "cluster_summary": data.get("topology", {}).get("summary", {}),
            "weaknesses": data.get("weaknesses", []),
            "experiment_plan": data.get("experiment_plan", []),
            "experiment_results": data.get("experiment_results", []),
            "safety_violations": data.get("safety_violations", []),
            "observations_count": len(data.get("observations", [])),
        }
        content = json.dumps(report, indent=2, default=str)
        file_path = self._write_report(content, "json")
        logger.info("JSON report written to %s", file_path)
        return {"content": content, "file": file_path, "format": "json"}

    def _write_report(self, content: str, ext: str) -> str:
        """Write report to the configured output directory."""
        out_dir = Path(self.report_config.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        file_path = out_dir / f"resilience_report_{timestamp}.{ext}"
        file_path.write_text(content, encoding="utf-8")
        return str(file_path)

    def _compact_for_llm(self, data: dict[str, Any]) -> dict[str, Any]:
        """Trim observation data to keep the LLM prompt within context limits."""
        compact = dict(data)
        observations = compact.get("observations", [])
        if len(observations) > 20:
            # Keep first 10 and last 10
            compact["observations"] = observations[:10] + observations[-10:]
            compact["observations_trimmed"] = True
            compact["total_observation_count"] = len(observations)
        return compact
