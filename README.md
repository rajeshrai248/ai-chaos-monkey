# AI Chaos Monkey

Autonomous AI agent for Kubernetes chaos engineering. It discovers your cluster topology, uses LLM reasoning to identify resilience weaknesses, plans and executes chaos experiments, monitors impact in real time, and produces actionable resilience reports.

## How It Works

AI Chaos Monkey connects to your Kubernetes cluster, maps every deployment, pod, service, and config resource, then feeds that topology to an LLM. The LLM identifies single points of failure, missing redundancy, resource risks, and other weaknesses. It then designs a targeted experiment plan — ordered from low to high blast radius — executes the experiments with safety guardrails, observes the results, and writes a resilience report with a risk score and prioritized recommendations.

```
chaos-monkey run --config config.yaml
```

One command. Full autonomous loop. Markdown report in `./reports/`.

## Architecture

### Agent Workflow

The core is a **LangGraph** state machine with seven nodes. Each node is an async function that reads from and writes to a shared `AgentState`.

```
┌──────────┐
│  START    │
└────┬─────┘
     │
┌────▼─────┐
│ discover  │  K8s API → namespaces, pods, deployments, services,
└────┬─────┘  statefulsets, ingresses, configmaps, secrets
     │
┌────▼─────┐
│ analyze   │  LLM identifies resilience weaknesses from topology
└────┬─────┘
     │
┌────▼─────┐
│  plan     │  LLM creates ordered experiment plan
└────┬─────┘
     │
┌────▼──────┐
│ validate   │──── FAIL ──→ re-plan (loops back to plan node)
└────┬──────┘
     │ PASS
┌────▼─────┐
│ execute   │  Run experiments with safety checks + rollback
└────┬─────┘
     │
┌────▼─────┐
│ observe   │  Monitor pod health + HTTP endpoints
└────┬─────┘
     │
┌────▼─────┐
│ report    │  LLM generates markdown resilience report
└────┬─────┘
     │
┌────▼─────┐
│   END     │
└──────────┘
```

**Conditional edges:**
- If the safety validator rejects every experiment in the plan, the graph loops back to the `plan` node for the LLM to generate a safer plan.
- After observation, the graph proceeds to `report` (the executor runs the full plan in sequence).

### State Model

All data flows through a single `AgentState` TypedDict:

| Field | Type | Description |
|---|---|---|
| `cluster_topology` | `dict` | Discovered K8s resources |
| `weaknesses` | `list[dict]` | LLM-identified resilience issues |
| `experiment_plan` | `list[dict]` | Planned chaos experiments |
| `current_experiment` | `dict \| None` | Currently executing experiment |
| `experiment_results` | `list[dict]` | Results from executed experiments |
| `observations` | `list[dict]` | Health/monitoring observations |
| `report` | `dict \| None` | Final resilience report |
| `messages` | `list` | LLM conversation history |
| `safety_violations` | `list[str]` | Safety rule violations |
| `dry_run` | `bool` | Dry-run mode flag |

### Project Structure

```
src/chaos_monkey/
├── cli.py                    # Typer CLI — 7 commands
├── config.py                 # Pydantic config models + YAML loader
│
├── agent/
│   ├── graph.py              # LangGraph state machine
│   ├── state.py              # AgentState TypedDict
│   └── tools.py              # LangChain tool definitions
│
├── discovery/
│   └── cluster.py            # K8s topology mapper
│
├── analysis/
│   └── analyzer.py           # LLM weakness analysis
│
├── planner/
│   └── planner.py            # LLM experiment planner
│
├── experiments/
│   ├── base.py               # ChaosExperiment ABC + ExperimentResult
│   ├── registry.py           # Auto-discovery plugin registry
│   ├── executor.py           # Execution engine with safety + rollback
│   ├── pod.py                # PodKill, PodRestart, CpuStress, MemoryStress
│   ├── network.py            # Latency, PacketLoss, Partition, DnsFailure
│   ├── node.py               # NodeDrain, NodeCordon
│   ├── io.py                 # DiskStress, DiskFill
│   ├── application.py        # HttpErrorInjection
│   └── config_chaos.py       # ConfigMapMutation, SecretDeletion
│
├── observer/
│   └── monitor.py            # Pod health checks + endpoint monitoring
│
├── reporter/
│   └── report.py             # Markdown/JSON report generation
│
└── safety/
    └── controls.py           # Blast radius, dry-run, rollback, exclusions
```

## Experiment Catalog

15 experiments across 6 categories:

| Category | Experiment | Blast Radius | Reversible | Method |
|---|---|---|---|---|
| **Pod** | `pod-kill` | low | yes | `delete_namespaced_pod` |
| **Pod** | `pod-restart` | medium | yes | Scale replicas 0 → N |
| **Pod** | `cpu-stress` | low | yes | `stress-ng --cpu` via exec |
| **Pod** | `memory-stress` | medium | yes | `stress-ng --vm` via exec |
| **Network** | `network-latency` | medium | yes | `tc qdisc netem delay` via exec |
| **Network** | `packet-loss` | medium | yes | `tc qdisc netem loss` via exec |
| **Network** | `network-partition` | high | yes | Create deny-all NetworkPolicy |
| **Network** | `dns-failure` | high | yes | Modify CoreDNS Corefile |
| **Node** | `node-drain` | high | yes | Cordon + evict pods |
| **Node** | `node-cordon` | medium | yes | Mark node unschedulable |
| **I/O** | `disk-stress` | medium | yes | `stress-ng --hdd` via exec |
| **I/O** | `disk-fill` | medium | yes | `fallocate` + cleanup |
| **App** | `http-error-injection` | low | yes | Set error env vars on deployment |
| **Config** | `configmap-mutation` | medium | yes | Patch ConfigMap + restore |
| **Config** | `secret-deletion` | high | yes | Delete Secret + restore from backup |

### Plugin System

Experiments use an auto-discovery registry. Every concrete subclass of `ChaosExperiment` is automatically registered — no manual registration needed. Adding a new experiment is just creating a new class:

```python
from chaos_monkey.experiments.base import BlastRadius, ChaosExperiment, ExperimentResult, ExperimentStatus

class MyExperiment(ChaosExperiment):
    name = "my-experiment"
    description = "Does something chaotic"
    category = "pod"
    blast_radius = BlastRadius.LOW
    reversible = True

    async def execute(self, target, namespace, k8s_client, params=None):
        # your chaos logic
        return ExperimentResult(
            experiment_name=self.name,
            status=ExperimentStatus.COMPLETED,
            target=target, namespace=namespace,
        )

    async def rollback(self, target, namespace, k8s_client, context=None):
        # undo the chaos
        pass

    def validate_target(self, target, namespace, topology):
        # check if target exists
        return True
```

Drop the file in `src/chaos_monkey/experiments/` and it's available immediately.

## Safety Controls

The `SafetyController` enforces multiple layers of protection:

| Control | Description | Default |
|---|---|---|
| **Namespace exclusions** | Never touch system namespaces | `kube-system`, `kube-public`, `kube-node-lease` |
| **Blast radius cap** | Reject experiments above configured threshold | `medium` |
| **Concurrent limit** | Max simultaneous experiments | `1` |
| **Healthy cluster gate** | Require cluster health before experiments | `true` |
| **Dry-run mode** | Full workflow without touching the cluster | `false` |
| **Auto-rollback** | Automatic rollback on failure or timeout | `true` |
| **Rollback timeout** | Max time to wait for rollback | `300s` |

Safety validation happens at the `validate` node in the workflow. If every experiment in a plan is rejected, the graph loops back to the `plan` node so the LLM can generate a less aggressive plan.

## CLI Reference

```
chaos-monkey discover    [-c config.yaml] [-o topology.json]
chaos-monkey analyze     [-c config.yaml]
chaos-monkey plan        [-c config.yaml]
chaos-monkey execute     -e <experiment> -t <target> [-n namespace] [--dry-run]
chaos-monkey run         [-c config.yaml] [--dry-run]
chaos-monkey report      -r <results.json> [-c config.yaml] [-o report.md]
chaos-monkey experiments
```

| Command | Description |
|---|---|
| `discover` | Map cluster topology (namespaces, pods, deployments, services, etc.) |
| `analyze` | Discover + LLM-identify resilience weaknesses |
| `plan` | Discover + analyze + generate experiment plan |
| `execute` | Run a single named experiment against a specific target |
| `run` | Full autonomous loop: discover → analyze → plan → execute → observe → report |
| `report` | Generate a resilience report from saved experiment results |
| `experiments` | List all 15 available experiment types in a table |

## Configuration

Copy `config.example.yaml` and customize:

```yaml
llm:
  model: "anthropic/claude-sonnet-4-5-20250929"   # Any LiteLLM model string
  temperature: 0.2
  api_key_env: "ANTHROPIC_API_KEY"

kubernetes:
  kubeconfig: "~/.kube/config"     # null for in-cluster auth
  context: null                     # null for current context

safety:
  excluded_namespaces: ["kube-system", "kube-public", "kube-node-lease"]
  max_blast_radius: "medium"        # low, medium, high
  max_concurrent_experiments: 1
  require_healthy_cluster: true
  dry_run: false
  auto_rollback: true
  rollback_timeout_seconds: 300

target:
  namespaces: []                    # empty = all non-excluded namespaces
  label_selectors: {}               # optional label filters

observer:
  health_check_interval_seconds: 5
  endpoint_timeout_seconds: 10
  monitor_duration_seconds: 60

report:
  output_dir: "./reports"
  format: "markdown"                # markdown or json
```

### LLM Provider Configuration

Uses [LiteLLM](https://docs.litellm.ai/) for provider-agnostic LLM access. Set the model string and corresponding API key env var:

| Provider | Model String | Env Var |
|---|---|---|
| Anthropic | `anthropic/claude-sonnet-4-5-20250929` | `ANTHROPIC_API_KEY` |
| OpenAI | `gpt-4o` | `OPENAI_API_KEY` |
| Ollama (local) | `ollama/llama3` | — |
| Azure OpenAI | `azure/gpt-4o` | `AZURE_API_KEY` |

## Installation

Requires **Python 3.11+** and access to a Kubernetes cluster.

```bash
# Clone
git clone https://github.com/rajeshrai248/ai-chaos-monkey.git
cd ai-chaos-monkey

# Install
pip install -e .

# Or with dev dependencies
pip install -e ".[dev]"

# Set your LLM API key
export ANTHROPIC_API_KEY="sk-..."

# Copy and edit config
cp config.example.yaml config.yaml
```

## Quick Start

```bash
# List available experiments
chaos-monkey experiments

# Discover cluster topology
chaos-monkey discover -c config.yaml

# Dry-run the full autonomous loop (no cluster changes)
chaos-monkey run -c config.yaml --dry-run

# Full run against a live cluster
chaos-monkey run -c config.yaml

# Run a single experiment
chaos-monkey execute -e pod-kill -t my-pod -n default --dry-run
```

## Tech Stack

| Component | Library | Purpose |
|---|---|---|
| Agent orchestration | [LangGraph](https://github.com/langchain-ai/langgraph) | State machine workflow with conditional routing |
| LLM access | [LiteLLM](https://github.com/BerriAI/litellm) | Provider-agnostic (Claude, GPT-4, Ollama, etc.) |
| Kubernetes API | [kubernetes](https://github.com/kubernetes-client/python) | Cluster discovery, pod exec, resource manipulation |
| CLI | [Typer](https://github.com/tiangolo/typer) | Command-line interface |
| Config & validation | [Pydantic](https://github.com/pydantic/pydantic) | Typed config models, YAML loading |
| Terminal output | [Rich](https://github.com/Textualize/rich) | Tables, colored output, JSON formatting |
| HTTP probing | [httpx](https://github.com/encode/httpx) | Async endpoint health checks |

## License

MIT
