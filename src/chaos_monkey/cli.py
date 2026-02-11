"""Typer CLI entrypoint for AI Chaos Monkey."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from chaos_monkey.config import load_config

app = typer.Typer(
    name="chaos-monkey",
    help="AI Chaos Monkey - Autonomous Kubernetes chaos engineering agent.",
    no_args_is_help=True,
)
console = Console()


def _load(config_path: str | None) -> "AppConfig":  # noqa: F821
    try:
        return load_config(config_path)
    except FileNotFoundError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc


# ── discover ────────────────────────────────────────────────────────────
@app.command()
def discover(
    config: Optional[str] = typer.Option(None, "--config", "-c", help="Path to config YAML"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Write JSON to file"),
) -> None:
    """Map cluster topology."""
    from chaos_monkey.discovery.cluster import ClusterDiscovery

    cfg = _load(config)
    disc = ClusterDiscovery(cfg.kubernetes, cfg.target, cfg.safety)
    topology = asyncio.run(disc.discover())

    if output:
        Path(output).write_text(json.dumps(topology, indent=2, default=str))
        console.print(f"[green]Topology written to {output}[/green]")
    else:
        console.print_json(json.dumps(topology, default=str))


# ── analyze ─────────────────────────────────────────────────────────────
@app.command()
def analyze(
    config: Optional[str] = typer.Option(None, "--config", "-c"),
) -> None:
    """Discover cluster and identify resilience weaknesses."""
    from chaos_monkey.agent.graph import build_graph

    cfg = _load(config)
    graph = build_graph(cfg)
    result = asyncio.run(graph.ainvoke({"dry_run": cfg.safety.dry_run}, stop_after=["analyze"]))
    weaknesses = result.get("weaknesses", [])
    console.print(f"\n[bold]Found {len(weaknesses)} potential weakness(es):[/bold]")
    for w in weaknesses:
        console.print(f"  • {w.get('title', w)}")


# ── plan ────────────────────────────────────────────────────────────────
@app.command()
def plan(
    config: Optional[str] = typer.Option(None, "--config", "-c"),
) -> None:
    """Discover, analyze, and generate an experiment plan."""
    from chaos_monkey.agent.graph import build_graph

    cfg = _load(config)
    graph = build_graph(cfg)
    result = asyncio.run(graph.ainvoke({"dry_run": cfg.safety.dry_run}, stop_after=["validate"]))
    plan_data = result.get("experiment_plan", [])
    console.print(f"\n[bold]Experiment plan ({len(plan_data)} step(s)):[/bold]")
    for i, step in enumerate(plan_data, 1):
        console.print(f"  {i}. [{step.get('experiment')}] → {step.get('target')} "
                       f"in {step.get('namespace', 'default')}")


# ── execute ─────────────────────────────────────────────────────────────
@app.command()
def execute(
    experiment: str = typer.Option(..., "--experiment", "-e", help="Experiment type name"),
    target: str = typer.Option(..., "--target", "-t", help="Target resource name"),
    namespace: str = typer.Option("default", "--namespace", "-n"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
) -> None:
    """Run a single chaos experiment."""
    from chaos_monkey.experiments.executor import ExperimentExecutor
    from chaos_monkey.experiments.registry import ExperimentRegistry
    from chaos_monkey.safety.controls import SafetyController

    cfg = _load(config)
    registry = ExperimentRegistry()
    safety = SafetyController(cfg.safety)
    executor = ExperimentExecutor(registry, safety, cfg.kubernetes)

    entry = {"experiment": experiment, "target": target, "namespace": namespace}
    result = asyncio.run(executor.run_one(entry, dry_run=dry_run or cfg.safety.dry_run))
    console.print_json(json.dumps(result.to_dict(), default=str))


# ── run ─────────────────────────────────────────────────────────────────
@app.command()
def run(
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Full autonomous loop: discover -> analyze -> plan -> execute -> observe -> report."""
    from chaos_monkey.agent.graph import build_graph

    cfg = _load(config)
    if dry_run:
        cfg.safety.dry_run = True
    graph = build_graph(cfg)
    result = asyncio.run(graph.ainvoke({"dry_run": cfg.safety.dry_run}))

    report_data = result.get("report")
    if report_data and report_data.get("file"):
        console.print(f"\n[green]Report saved to {report_data['file']}[/green]")
    console.print("[bold green]Run complete.[/bold green]")


# ── report ──────────────────────────────────────────────────────────────
@app.command()
def report(
    results_file: str = typer.Option(..., "--results", "-r", help="Path to results JSON"),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    output: Optional[str] = typer.Option(None, "--output", "-o"),
) -> None:
    """Generate a resilience report from past experiment results."""
    from chaos_monkey.reporter.report import ReportGenerator

    cfg = _load(config)
    raw = json.loads(Path(results_file).read_text())
    gen = ReportGenerator(cfg.report, cfg.llm)
    report_data = asyncio.run(gen.generate(raw))

    out_path = output or str(Path(cfg.report.output_dir) / "report.md")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(report_data["content"])
    console.print(f"[green]Report saved to {out_path}[/green]")


# ── experiments ─────────────────────────────────────────────────────────
@app.command()
def experiments() -> None:
    """List all available experiment types."""
    from chaos_monkey.experiments.registry import ExperimentRegistry

    registry = ExperimentRegistry()
    table = Table(title="Available Chaos Experiments")
    table.add_column("Name", style="cyan")
    table.add_column("Category", style="magenta")
    table.add_column("Blast Radius", style="yellow")
    table.add_column("Reversible", style="green")
    table.add_column("Description")

    for info in sorted(registry.list_all(), key=lambda x: (x["category"], x["name"])):
        table.add_row(
            info["name"],
            info["category"],
            info["blast_radius"],
            "yes" if info["reversible"] else "no",
            info["description"],
        )

    console.print(table)


if __name__ == "__main__":
    app()
