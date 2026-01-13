# TwinOps

[![CI](https://github.com/rwth-ias/twinops/actions/workflows/ci.yml/badge.svg)](https://github.com/rwth-ias/twinops/actions/workflows/ci.yml)
[![Security](https://github.com/rwth-ias/twinops/actions/workflows/security.yml/badge.svg)](https://github.com/rwth-ias/twinops/actions/workflows/security.yml)
[![codecov](https://codecov.io/gh/rwth-ias/twinops/branch/main/graph/badge.svg)](https://codecov.io/gh/rwth-ias/twinops)
[![PyPI version](https://badge.fury.io/py/twinops.svg)](https://badge.fury.io/py/twinops)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Docker](https://img.shields.io/badge/docker-ready-blue.svg)](https://github.com/rwth-ias/twinops/pkgs/container/twinops-agent)

**Production-Grade AI Agents for BaSyx Digital Twins**

A reference architecture for event-driven, safety-governed industrial AI that interacts with Asset Administration Shell (AAS) runtimes.

## Overview

TwinOps provides a complete framework for deploying AI agents that safely interact with industrial digital twins. Unlike demonstration prototypes, this architecture addresses critical engineering gaps required for production deployment:

- **Split-brain state prevention** through MQTT-driven Shadow Twin synchronization
- **Asynchronous operation handling** via the Command-Monitor job pattern
- **Context window management** through semantic capability indexing
- **Industrial-grade safety** with RBAC, interlocks, simulation forcing, and HITL approval gates
- **Tamper-evident governance** via cryptographically signed Policy-as-AAS (CovenantTwin)
- **Immutable audit logging** with hash-chained entries

## Quick Start

### Prerequisites

- Docker and Docker Compose
- Python 3.11+ (for local development)

### Run with Docker Compose

```bash
# Start all services (sandbox mode - no API key required)
docker compose up --build

# Send a command
curl -s http://localhost:8080/chat \
  -H 'Content-Type: application/json' \
  -H 'X-Roles: operator' \
  -d '{"message":"Set speed to 1200 RPM"}' | jq

# Response shows simulation was forced (HIGH risk operation)
```

### Run with Real LLM

```bash
# Set your API key
export ANTHROPIC_API_KEY=your-key-here

# Update docker-compose.yml to use anthropic provider
# Or set environment variable:
docker compose up -e TWINOPS_LLM_PROVIDER=anthropic -e TWINOPS_ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY
```

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         AI Agent                                 │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────────┐  │
│  │ LLM Client  │  │ Capability  │  │    Safety Kernel        │  │
│  │ (Anthropic/ │  │   Index     │  │ - RBAC                  │  │
│  │  OpenAI/    │  │ (TF-IDF)    │  │ - Interlocks            │  │
│  │  Rules)     │  │             │  │ - Simulation Forcing    │  │
│  └──────┬──────┘  └──────┬──────┘  │ - HITL Approval         │  │
│         │                │         │ - Audit Logging         │  │
│         └───────┬────────┘         └───────────┬─────────────┘  │
│                 │                              │                 │
│         ┌───────┴──────────────────────────────┴───────┐        │
│         │              Orchestrator                     │        │
│         └───────────────────┬──────────────────────────┘        │
└─────────────────────────────┼───────────────────────────────────┘
                              │
          ┌───────────────────┼───────────────────┐
          │                   │                   │
    ┌─────┴─────┐      ┌──────┴──────┐     ┌──────┴──────┐
    │  Shadow   │      │   Twin      │     │  Operation  │
    │   Twin    │◄────►│   Client    │     │   Service   │
    │  Manager  │      │   (HTTP)    │     │ (Delegated) │
    └─────┬─────┘      └─────────────┘     └─────────────┘
          │                   │
    ┌─────┴─────┐      ┌──────┴──────┐
    │   MQTT    │      │    AAS      │
    │  Broker   │      │ Repository  │
    └───────────┘      └─────────────┘
```

## Components

### Core Services

| Service | Port | Description |
|---------|------|-------------|
| `agent` | 8080 | AI agent HTTP API |
| `twin-sandbox` | 8081 | Local AAS mock server |
| `opservice` | 8087 | Operation delegation service |
| `mqtt` | 1883 | MQTT broker for events |

### Key Modules

- **Shadow Twin Manager** (`agent/shadow.py`): Live synchronized AAS state via MQTT
- **Schema Generator** (`agent/schema_gen.py`): AAS Operation → LLM Tool conversion
- **Capability Index** (`agent/capabilities.py`): Semantic tool retrieval
- **Safety Kernel** (`agent/safety.py`): Multi-layer defense model
- **Policy Signing** (`agent/policy_signing.py`): CovenantTwin Ed25519 verification

## Safety Model

TwinOps implements a five-layer defense model:

1. **RBAC**: Role-based access control per operation
2. **Interlocks**: Predicate-based safety checks against live state
3. **Simulation Forcing**: Automatic dry-run for high-risk operations
4. **HITL Approval**: Human approval gates for critical operations
5. **Audit Logging**: Hash-chained tamper-evident logs

### Risk Levels

| Level | Simulation | Approval | Examples |
|-------|------------|----------|----------|
| LOW | No | No | Status queries |
| MEDIUM | No | No | Minor setpoint changes |
| HIGH | **Yes** | No | Equipment actuation |
| CRITICAL | **Yes** | **Yes** | Safety-critical ops |

## CovenantTwin

CovenantTwin embeds cryptographically signed safety policies directly within the AAS:

```json
{
  "require_simulation_for_risk": "HIGH",
  "require_approval_for_risk": "CRITICAL",
  "role_bindings": {
    "operator": { "allow": ["StartPump", "StopPump", "SetSpeed"] },
    "viewer": { "allow": ["GetStatus"] },
    "maintenance": { "allow": ["*"] }
  },
  "interlocks": [
    {
      "id": "temp-high",
      "deny_when": {
        "submodel": "urn:example:submodel:operational",
        "path": "CurrentTemperature",
        "op": ">",
        "value": 95
      },
      "message": "Temperature too high"
    }
  ]
}
```

### Signing Policies

```bash
# Generate key pair
python scripts/generate_policy_keypair.py --output keys/

# Sign policy
python scripts/sign_policy.py \
  --policy-file models/policy.json \
  --private-key keys/policy_private.pem \
  --output models/policy_signed.json
```

## CLI Usage

```bash
# List pending approval tasks
twinops --base-url http://localhost:8081 list-tasks

# Approve a task
twinops approve --task-id task-abc123

# Reject a task
twinops reject --task-id task-abc123 --reason "Maintenance window"

# Verify audit log integrity
twinops verify-audit --log-path audit_logs/audit.jsonl

# Show recent audit entries
twinops show-audit --last 20 --filter-event executed
```

## Deployment

### Docker Compose (Development)

```bash
docker compose up --build
```

### Docker Compose (BaSyx Integration)

```bash
docker compose -f docker-compose.basyx.yml up --build
```

### Kubernetes

```bash
# Apply with kustomize
kubectl apply -k deploy/k8s/

# Or individual resources
kubectl apply -f deploy/k8s/namespace.yaml
kubectl apply -f deploy/k8s/
```

## Configuration

Environment variables (prefix: `TWINOPS_`):

| Variable | Default | Description |
|----------|---------|-------------|
| `TWIN_BASE_URL` | `http://localhost:8081` | AAS repository URL |
| `MQTT_BROKER_HOST` | `localhost` | MQTT broker hostname |
| `MQTT_BROKER_PORT` | `1883` | MQTT broker port |
| `LLM_PROVIDER` | `rules` | LLM provider (rules/anthropic/openai) |
| `ANTHROPIC_API_KEY` | - | Anthropic API key |
| `AAS_ID` | `urn:example:aas:pump-001` | Target AAS identifier |
| `REPO_ID` | `default` | Repository ID for MQTT topics |

## API Reference

### POST /chat

Send a natural language command to the agent.

```bash
curl -X POST http://localhost:8080/chat \
  -H 'Content-Type: application/json' \
  -H 'X-Roles: operator' \
  -d '{"message": "Start the pump"}'
```

Response:
```json
{
  "reply": "Simulation completed for 'StartPump'. To execute for real, re-issue with simulate=false.",
  "tool_results": [{
    "tool": "StartPump",
    "success": true,
    "simulated": true,
    "status": "simulated_only"
  }],
  "pending_approval": false,
  "task_id": null
}
```

### GET /health

Health check endpoint.

### POST /reset

Reset conversation history.

## Development

### Local Setup

```bash
# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Type checking
mypy src/twinops

# Linting
ruff check src/
```

### Project Structure

```
twinops/
├── src/twinops/
│   ├── agent/           # AI agent components
│   │   ├── shadow.py    # Shadow Twin Manager
│   │   ├── schema_gen.py # Tool schema generation
│   │   ├── capabilities.py # Capability index
│   │   ├── safety.py    # Safety kernel
│   │   ├── policy_signing.py # CovenantTwin
│   │   ├── orchestrator.py # Main agent loop
│   │   └── llm/         # LLM integrations
│   ├── sandbox/         # Local AAS mock
│   ├── opservice/       # Operation delegation
│   ├── common/          # Shared utilities
│   └── cli.py           # CLI tool
├── models/              # Sample AAS data
├── scripts/             # Utility scripts
├── docker/              # Dockerfiles
├── deploy/k8s/          # Kubernetes manifests
└── infra/               # Infrastructure configs
```

## License

MIT License - see LICENSE file.

## References

- [BaSyx Wiki - MQTT Feature](https://wiki.basyx.org/en/latest/content/user_documentation/basyx_components/v2/aas_repository/features/mqtt.html)
- [BaSyx Wiki - Operation Delegation](https://wiki.basyx.org/en/latest/content/user_documentation/basyx_components/v2/submodel_repository/features/operation-delegation.html)
- [IDTA-01001-3-0-1: AAS Metamodel](https://industrialdigitaltwin.org/content-hub/aasspecifications)

---

*Developed by RWTH Aachen University - Chair of Information and Automation Systems*
