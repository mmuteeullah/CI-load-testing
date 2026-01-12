# CI Load Testing

[![Load Test](https://github.com/mmuteeullah/CI-load-testing/actions/workflows/load-test.yaml/badge.svg)](https://github.com/mmuteeullah/CI-load-testing/actions/workflows/load-test.yaml)

Automated Kubernetes load testing for pull requests using GitHub Actions, k3d, and Vegeta.

## Overview

This project provisions a multi-node Kubernetes cluster on every PR, deploys http-echo services, runs load tests, and posts results as a PR comment. PRs cannot be merged until load tests pass.

```
PR opened → k3d cluster → Deploy apps → Run Vegeta → Post results → Pass/Fail
```

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  GitHub Actions Runner                                      │
├─────────────────────────────────────────────────────────────┤
│  ┌─────────────────────────────────────────────────────┐    │
│  │  k3d Cluster (1 server + 2 agents)                  │    │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  │    │
│  │  │   Server    │  │   Agent 1   │  │   Agent 2   │  │    │
│  │  └─────────────┘  └─────────────┘  └─────────────┘  │    │
│  │                                                      │    │
│  │  ┌─────────────────────────────────────────────┐    │    │
│  │  │  NGINX Ingress Controller                   │    │    │
│  │  │  foo.localhost → foo service                │    │    │
│  │  │  bar.localhost → bar service                │    │    │
│  │  └─────────────────────────────────────────────┘    │    │
│  │                                                      │    │
│  │  ┌──────────────┐  ┌──────────────┐                 │    │
│  │  │  foo (x2)    │  │  bar (x2)    │                 │    │
│  │  │  "foo"       │  │  "bar"       │                 │    │
│  │  └──────────────┘  └──────────────┘                 │    │
│  │                                                      │    │
│  │  ┌─────────────────────────────────────────────┐    │    │
│  │  │  Prometheus (optional)                      │    │    │
│  │  │  CPU/Memory metrics                         │    │    │
│  │  └─────────────────────────────────────────────┘    │    │
│  └─────────────────────────────────────────────────────┘    │
│                                                             │
│  Vegeta ──→ foo.localhost ──→ results-foo.json             │
│         ──→ bar.localhost ──→ results-bar.json             │
│         ──→ combined      ──→ results-combined.json        │
└─────────────────────────────────────────────────────────────┘
```

## Project Structure

```
.
├── .github/workflows/
│   └── load-test.yaml           # CI workflow
├── k3d/
│   └── cluster-config.yaml      # Multi-node cluster config
├── k8s/
│   ├── base/
│   │   ├── http-echo/           # Reusable deployment template
│   │   │   ├── deployment.yaml
│   │   │   ├── service.yaml
│   │   │   └── kustomization.yaml
│   │   └── namespace.yaml
│   ├── apps/
│   │   ├── foo/kustomization.yaml   # foo app config
│   │   └── bar/kustomization.yaml   # bar app config
│   └── overlays/
│       └── ci/
│           ├── kustomization.yaml   # Combines everything
│           └── ingress.yaml         # Localhost routing
├── monitoring/
│   └── prometheus-values.yaml   # Prometheus Helm values
├── scripts/
│   └── run-load-test.py         # Unified test runner
└── README.md
```

## How It Works

### 1. Trigger

The workflow runs on:
- Pull requests to `main`
- Manual dispatch (with configurable rate/duration)

### 2. Setup

Installs: k3d, kubectl, helm, vegeta, kustomize

### 3. Cluster Creation

```bash
k3d cluster create --config k3d/cluster-config.yaml
```

Creates a 3-node cluster (1 server + 2 agents) with:
- Ports 80/443 mapped to loadbalancer
- Traefik disabled (using NGINX Ingress instead)

### 4. Infrastructure

- NGINX Ingress Controller for routing
- Prometheus (optional) for resource metrics

### 5. Application Deployment

```bash
kustomize build k8s/overlays/ci | kubectl apply -f -
```

Deploys:
- `foo` deployment (2 replicas) → responds with "foo"
- `bar` deployment (2 replicas) → responds with "bar"
- Ingress routing `foo.localhost` and `bar.localhost`

### 6. Load Testing

Runs 3 sequential Vegeta tests:
1. `foo.localhost` only
2. `bar.localhost` only
3. Combined (alternating)

### 7. Results

Posts a comment on the PR with:
- Request metrics (total, rate, success %)
- Latency distribution (mean, P50, P90, P95, P99, max)
- HTTP status codes breakdown
- Resource utilization (CPU/memory per pod)
- Errors (if any)

### 8. Pass/Fail

- Tests pass → PR can be merged
- Tests fail → PR merge blocked

## Usage

### Automatic (PR)

Open a PR to `main` and the workflow runs automatically.

### Manual

1. Go to Actions → K8s Load Test
2. Click "Run workflow"
3. Configure rate and duration
4. Run

### Local Testing

```bash
# Create cluster
k3d cluster create --config k3d/cluster-config.yaml

# Install NGINX Ingress
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.9.5/deploy/static/provider/cloud/deploy.yaml

# Deploy app
kustomize build k8s/overlays/ci | kubectl apply -f -

# Run tests
python scripts/run-load-test.py --rate 50 --duration 10s

# Cleanup
k3d cluster delete load-test-cluster
```

## Configuration

### Load Test Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `--rate` | 100 | Requests per second |
| `--duration` | 30s | Test duration |
| `--namespace` | echo-test | Kubernetes namespace |
| `--skip-metrics` | false | Skip Prometheus metrics |

### Resource Limits

Configured in `k8s/base/http-echo/deployment.yaml`:

```yaml
resources:
  requests:
    cpu: 50m
    memory: 64Mi
  limits:
    cpu: 200m
    memory: 128Mi
```

## Branch Protection

To enforce load test passing before merge:

1. Go to Settings → Branches
2. Add rule for `main`
3. Enable "Require status checks to pass"
4. Select "load-test" as required

## Example PR Comment

```markdown
## Load Test Results

### foo.localhost

| Metric | Value |
|--------|-------|
| Total Requests | 3000 |
| Duration | 30s |
| Requests/sec | 100 |
| Success Rate | 100% |
| Failed Requests | 0 |

**Latency**

| Percentile | Value |
|------------|-------|
| Mean | 2.5ms |
| P50 | 2.1ms |
| P90 | 4.2ms |
| P95 | 5.1ms |
| P99 | 8.3ms |
| Max | 15.2ms |

...
```

## Tech Stack

| Component | Tool |
|-----------|------|
| CI | GitHub Actions |
| Cluster | k3d (k3s in Docker) |
| Ingress | NGINX Ingress Controller |
| Manifests | Kustomize |
| Load Testing | Vegeta |
| Monitoring | kube-prometheus-stack |
| Scripting | Python 3.11 |

## Troubleshooting

### Tests fail with "Cannot reach foo.localhost"

Ingress not ready. Check:
```bash
kubectl get pods -n ingress-nginx
kubectl get ingress -n echo-test
```

### Prometheus metrics unavailable

Prometheus install is optional. Tests continue without metrics.

### Cluster creation fails

Docker must be running. Check:
```bash
docker info
k3d cluster list
```
# Test fail-rate feature
