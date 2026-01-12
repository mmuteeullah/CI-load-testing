#!/usr/bin/env python3
"""
CI Load Test Runner

Single script that orchestrates:
1. Wait for K8s resources to be ready
2. Run Vegeta load tests (foo, bar, combined)
3. Collect Prometheus metrics
4. Format results as Markdown for PR comment

Usage:
    python scripts/run-load-test.py
    python scripts/run-load-test.py --skip-metrics
    python scripts/run-load-test.py --rate 200 --duration 60s
"""

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import urllib.request
import urllib.error
import urllib.parse


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class Config:
    """Load test configuration."""
    namespace: str = "echo-test"
    rate: int = 100
    duration: str = "30s"
    output_dir: Path = field(default_factory=lambda: Path("./results"))
    timeout: int = 180
    skip_metrics: bool = False
    prometheus_port: int = 9090
    fail_rate: int = 0  # Percentage of requests to non-existent endpoint (0-100)


# =============================================================================
# Result Classes
# =============================================================================

@dataclass
class TestResult:
    """Result of a single load test."""
    name: str
    success: bool
    requests: int = 0
    rate: float = 0.0
    duration_s: float = 0.0
    success_rate: float = 0.0
    latency_mean_ms: float = 0.0
    latency_p50_ms: float = 0.0
    latency_p90_ms: float = 0.0
    latency_p95_ms: float = 0.0
    latency_p99_ms: float = 0.0
    latency_max_ms: float = 0.0
    status_codes: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    error_message: str = ""


@dataclass
class ResourceMetrics:
    """Resource utilization metrics."""
    pod: str
    cpu_percent: float = 0.0
    memory_mb: float = 0.0


@dataclass
class LoadTestReport:
    """Complete load test report."""
    foo_result: Optional[TestResult] = None
    bar_result: Optional[TestResult] = None
    combined_result: Optional[TestResult] = None
    resource_metrics: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    errors: list = field(default_factory=list)


# =============================================================================
# Utility Functions
# =============================================================================

def log(message: str, level: str = "INFO") -> None:
    """Print a log message with level."""
    colors = {
        "INFO": "\033[94m",    # Blue
        "OK": "\033[92m",      # Green
        "WARN": "\033[93m",    # Yellow
        "ERROR": "\033[91m",   # Red
        "RESET": "\033[0m"
    }
    color = colors.get(level, "")
    reset = colors["RESET"]
    print(f"{color}[{level}]{reset} {message}", flush=True)


def run_cmd(
    cmd: list[str],
    timeout: int = 60,
    capture: bool = True,
    check: bool = True
) -> subprocess.CompletedProcess:
    """Run a command with timeout and error handling."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
            timeout=timeout,
            check=check
        )
        return result
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"Command timed out after {timeout}s: {' '.join(cmd)}") from e
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\n{e.stderr}") from e
    except FileNotFoundError:
        raise RuntimeError(f"Command not found: {cmd[0]}")


def check_command_exists(cmd: str) -> bool:
    """Check if a command is available in PATH."""
    try:
        subprocess.run(
            ["which", cmd],
            capture_output=True,
            check=True
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


# =============================================================================
# Phase 1: Validation
# =============================================================================

def validate_prerequisites() -> list[str]:
    """Validate all required tools are available. Returns list of errors."""
    errors = []

    required_commands = ["kubectl", "vegeta", "curl"]
    for cmd in required_commands:
        if not check_command_exists(cmd):
            errors.append(f"Required command not found: {cmd}")

    # Check kubectl can connect to cluster
    if "kubectl" not in [e.split()[-1] for e in errors]:
        try:
            run_cmd(["kubectl", "cluster-info"], timeout=10)
        except RuntimeError as e:
            errors.append(f"Cannot connect to Kubernetes cluster: {e}")

    return errors


def validate_output_dir(config: Config) -> None:
    """Ensure output directory exists and is writable."""
    try:
        config.output_dir.mkdir(parents=True, exist_ok=True)
        test_file = config.output_dir / ".write-test"
        test_file.write_text("test")
        test_file.unlink()
    except (OSError, IOError) as e:
        raise RuntimeError(f"Output directory not writable: {config.output_dir}: {e}")


# =============================================================================
# Phase 2: Wait for Resources
# =============================================================================

def wait_for_resource(
    resource_type: str,
    name: str,
    namespace: str,
    condition: str,
    timeout: int
) -> bool:
    """Wait for a K8s resource to meet a condition."""
    cmd = [
        "kubectl", "wait",
        f"--namespace={namespace}",
        f"--for={condition}",
        f"{resource_type}/{name}",
        f"--timeout={timeout}s"
    ]
    try:
        run_cmd(cmd, timeout=timeout + 10)
        return True
    except RuntimeError:
        return False


def wait_for_selector(
    namespace: str,
    selector: str,
    condition: str,
    timeout: int
) -> bool:
    """Wait for pods matching a selector."""
    cmd = [
        "kubectl", "wait",
        f"--namespace={namespace}",
        f"--for={condition}",
        "pod",
        f"--selector={selector}",
        f"--timeout={timeout}s"
    ]
    try:
        run_cmd(cmd, timeout=timeout + 10)
        return True
    except RuntimeError:
        return False


def get_pod_status(namespace: str) -> str:
    """Get pod status for debugging."""
    try:
        result = run_cmd([
            "kubectl", "get", "pods",
            f"--namespace={namespace}",
            "-o", "wide"
        ])
        return result.stdout
    except RuntimeError:
        return "Could not get pod status"


def test_connectivity(host: str, max_retries: int = 10, delay: float = 2.0) -> bool:
    """Test HTTP connectivity to a host via ingress."""
    for attempt in range(1, max_retries + 1):
        try:
            result = run_cmd([
                "curl", "-s", "-o", "/dev/null",
                "-w", "%{http_code}",
                "-H", f"Host: {host}",
                "--max-time", "5",
                "http://localhost/"
            ], check=False)

            if result.stdout.strip() == "200":
                return True

            log(f"Attempt {attempt}/{max_retries}: {host} returned {result.stdout.strip()}", "WARN")
        except RuntimeError as e:
            log(f"Attempt {attempt}/{max_retries}: Connection error - {e}", "WARN")

        if attempt < max_retries:
            time.sleep(delay)

    return False


def wait_for_resources(config: Config, report: LoadTestReport) -> bool:
    """Wait for all resources to be ready. Returns True if successful."""
    log("Waiting for NGINX Ingress Controller...")
    if not wait_for_selector(
        "ingress-nginx",
        "app.kubernetes.io/component=controller",
        "condition=ready",
        config.timeout
    ):
        report.errors.append("NGINX Ingress Controller not ready")
        return False
    log("NGINX Ingress Controller ready", "OK")

    # Wait for Prometheus (optional)
    log("Checking for Prometheus...")
    try:
        run_cmd(["kubectl", "get", "namespace", "monitoring"], timeout=10)
        if wait_for_selector(
            "monitoring",
            "app.kubernetes.io/name=prometheus",
            "condition=ready",
            60  # Shorter timeout for optional component
        ):
            log("Prometheus ready", "OK")
        else:
            report.warnings.append("Prometheus not ready, metrics may be unavailable")
            log("Prometheus not ready, continuing without metrics", "WARN")
    except RuntimeError:
        report.warnings.append("Prometheus not installed, skipping metrics")
        log("Prometheus not installed, skipping metrics", "WARN")
        config.skip_metrics = True

    # Wait for app deployments
    log(f"Waiting for foo deployment in {config.namespace}...")
    if not wait_for_resource("deployment", "foo", config.namespace, "condition=available", config.timeout):
        report.errors.append(f"Deployment 'foo' not ready. Pod status:\n{get_pod_status(config.namespace)}")
        return False
    log("foo deployment ready", "OK")

    log(f"Waiting for bar deployment in {config.namespace}...")
    if not wait_for_resource("deployment", "bar", config.namespace, "condition=available", config.timeout):
        report.errors.append(f"Deployment 'bar' not ready. Pod status:\n{get_pod_status(config.namespace)}")
        return False
    log("bar deployment ready", "OK")

    # Test connectivity
    log("Testing connectivity to foo.localhost...")
    if not test_connectivity("foo.localhost"):
        report.errors.append("Cannot reach foo.localhost via ingress")
        return False
    log("foo.localhost responding", "OK")

    log("Testing connectivity to bar.localhost...")
    if not test_connectivity("bar.localhost"):
        report.errors.append("Cannot reach bar.localhost via ingress")
        return False
    log("bar.localhost responding", "OK")

    return True


# =============================================================================
# Phase 3: Load Testing
# =============================================================================

def parse_vegeta_json(json_path: Path) -> TestResult:
    """Parse Vegeta JSON output into TestResult."""
    name = json_path.stem.replace("results-", "")

    try:
        with open(json_path) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        return TestResult(name=name, success=False, error_message=str(e))

    latencies = data.get("latencies", {})

    return TestResult(
        name=name,
        success=True,
        requests=data.get("requests", 0),
        rate=round(data.get("rate", 0), 2),
        duration_s=round(data.get("duration", 0) / 1e9, 1),
        success_rate=round(data.get("success", 0) * 100, 2),
        latency_mean_ms=round(latencies.get("mean", 0) / 1e6, 2),
        latency_p50_ms=round(latencies.get("50th", 0) / 1e6, 2),
        latency_p90_ms=round(latencies.get("90th", 0) / 1e6, 2),
        latency_p95_ms=round(latencies.get("95th", 0) / 1e6, 2),
        latency_p99_ms=round(latencies.get("99th", 0) / 1e6, 2),
        latency_max_ms=round(latencies.get("max", 0) / 1e6, 2),
        status_codes=data.get("status_codes", {}),
        errors=data.get("errors", [])
    )


def run_vegeta_test(
    name: str,
    host: str,
    config: Config
) -> TestResult:
    """Run a single Vegeta load test."""
    log(f"Running load test: {name} ({config.rate} req/s for {config.duration})")

    json_path = config.output_dir / f"results-{name}.json"
    bin_path = config.output_dir / f"results-{name}.bin"

    try:
        # Build vegeta command
        echo_cmd = f"GET http://localhost/\nHost: {host}"

        # Run vegeta attack
        attack = subprocess.Popen(
            ["vegeta", "attack",
             "-rate", str(config.rate),
             "-duration", config.duration,
             "-name", name],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )

        stdout, stderr = attack.communicate(input=echo_cmd.encode(), timeout=300)

        if attack.returncode != 0:
            return TestResult(
                name=name,
                success=False,
                error_message=f"Vegeta attack failed: {stderr.decode()}"
            )

        # Save binary results
        bin_path.write_bytes(stdout)

        # Generate JSON report
        report_result = subprocess.run(
            ["vegeta", "report", "-type=json"],
            input=stdout,
            capture_output=True,
            timeout=60
        )

        if report_result.returncode != 0:
            return TestResult(
                name=name,
                success=False,
                error_message=f"Vegeta report failed: {report_result.stderr.decode()}"
            )

        # Save JSON results
        json_path.write_bytes(report_result.stdout)

        # Parse and return
        result = parse_vegeta_json(json_path)
        log(f"Completed {name}: {result.requests} requests, {result.success_rate}% success", "OK")
        return result

    except subprocess.TimeoutExpired:
        return TestResult(name=name, success=False, error_message="Vegeta timed out")
    except Exception as e:
        return TestResult(name=name, success=False, error_message=str(e))


def generate_targets(config: Config) -> str:
    """Generate weighted targets based on fail_rate."""
    targets = []

    # Calculate weights: fail_rate% go to fail.localhost
    # Remaining split evenly between foo and bar
    fail_weight = config.fail_rate
    success_weight = 100 - fail_weight
    foo_weight = success_weight // 2
    bar_weight = success_weight - foo_weight

    # Add targets proportionally (using 100 entries for percentage accuracy)
    for _ in range(foo_weight):
        targets.append("GET http://localhost/\nHost: foo.localhost\n")

    for _ in range(bar_weight):
        targets.append("GET http://localhost/\nHost: bar.localhost\n")

    for _ in range(fail_weight):
        targets.append("GET http://localhost/\nHost: fail.localhost\n")

    return "\n".join(targets)


def run_combined_test(config: Config) -> TestResult:
    """Run combined test with alternating foo/bar targets and optional failures."""
    fail_info = f", {config.fail_rate}% fail" if config.fail_rate > 0 else ""
    log(f"Running combined load test ({config.rate} req/s for {config.duration}{fail_info})")

    name = "combined"
    json_path = config.output_dir / f"results-{name}.json"
    bin_path = config.output_dir / f"results-{name}.bin"
    targets_path = config.output_dir / "targets-combined.txt"

    try:
        # Create targets file with optional failure injection
        if config.fail_rate > 0:
            targets_content = generate_targets(config)
        else:
            targets_content = """GET http://localhost/
Host: foo.localhost

GET http://localhost/
Host: bar.localhost
"""
        targets_path.write_text(targets_content)

        # Run vegeta attack
        attack_result = subprocess.run(
            ["vegeta", "attack",
             "-rate", str(config.rate),
             "-duration", config.duration,
             "-targets", str(targets_path),
             "-name", name],
            capture_output=True,
            timeout=300
        )

        if attack_result.returncode != 0:
            return TestResult(
                name=name,
                success=False,
                error_message=f"Vegeta attack failed: {attack_result.stderr.decode()}"
            )

        # Save binary results
        bin_path.write_bytes(attack_result.stdout)

        # Generate JSON report
        report_result = subprocess.run(
            ["vegeta", "report", "-type=json"],
            input=attack_result.stdout,
            capture_output=True,
            timeout=60
        )

        if report_result.returncode != 0:
            return TestResult(
                name=name,
                success=False,
                error_message=f"Vegeta report failed: {report_result.stderr.decode()}"
            )

        # Save JSON results
        json_path.write_bytes(report_result.stdout)

        # Parse and return
        result = parse_vegeta_json(json_path)
        log(f"Completed {name}: {result.requests} requests, {result.success_rate}% success", "OK")
        return result

    except subprocess.TimeoutExpired:
        return TestResult(name=name, success=False, error_message="Vegeta timed out")
    except Exception as e:
        return TestResult(name=name, success=False, error_message=str(e))


def run_load_tests(config: Config, report: LoadTestReport) -> None:
    """Run all load tests sequentially."""
    log("=" * 50)
    log("Starting Load Tests")
    log("=" * 50)

    # Test 1: foo
    report.foo_result = run_vegeta_test("foo", "foo.localhost", config)

    # Test 2: bar
    report.bar_result = run_vegeta_test("bar", "bar.localhost", config)

    # Test 3: combined
    report.combined_result = run_combined_test(config)


# =============================================================================
# Phase 4: Collect Metrics
# =============================================================================

def query_prometheus(port: int, query: str) -> list:
    """Query Prometheus and return results."""
    try:
        url = f"http://localhost:{port}/api/v1/query"
        data = urllib.parse.urlencode({"query": query}).encode()

        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as response:
            result = json.loads(response.read().decode())
            return result.get("data", {}).get("result", [])
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError):
        return []


def collect_metrics(config: Config, report: LoadTestReport) -> None:
    """Collect resource metrics from Prometheus."""
    if config.skip_metrics:
        log("Skipping metrics collection", "WARN")
        return

    log("Collecting resource metrics from Prometheus...")

    # Start port-forward
    port_forward = None
    try:
        port_forward = subprocess.Popen(
            ["kubectl", "port-forward",
             "-n", "monitoring",
             "svc/prometheus-kube-prometheus-prometheus",
             f"{config.prometheus_port}:9090"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

        # Wait for port-forward to be ready
        time.sleep(5)

        # Query CPU usage
        cpu_results = query_prometheus(
            config.prometheus_port,
            f'sum(rate(container_cpu_usage_seconds_total{{namespace="{config.namespace}"}}[2m])) by (pod)'
        )

        # Query memory usage
        mem_results = query_prometheus(
            config.prometheus_port,
            f'sum(container_memory_working_set_bytes{{namespace="{config.namespace}"}}) by (pod)'
        )

        # Combine results
        pods_data = {}
        for item in cpu_results:
            pod = item.get("metric", {}).get("pod", "unknown")
            value = float(item.get("value", [0, 0])[1])
            pods_data.setdefault(pod, {})["cpu"] = round(value * 100, 2)

        for item in mem_results:
            pod = item.get("metric", {}).get("pod", "unknown")
            value = float(item.get("value", [0, 0])[1])
            pods_data.setdefault(pod, {})["mem"] = round(value / 1024 / 1024, 1)

        for pod, metrics in pods_data.items():
            report.resource_metrics.append(ResourceMetrics(
                pod=pod,
                cpu_percent=metrics.get("cpu", 0),
                memory_mb=metrics.get("mem", 0)
            ))

        if report.resource_metrics:
            log(f"Collected metrics for {len(report.resource_metrics)} pods", "OK")
        else:
            report.warnings.append("No resource metrics available")
            log("No resource metrics available", "WARN")

    except Exception as e:
        report.warnings.append(f"Failed to collect metrics: {e}")
        log(f"Failed to collect metrics: {e}", "WARN")
    finally:
        if port_forward:
            port_forward.terminate()
            port_forward.wait()


# =============================================================================
# Phase 5: Format Results
# =============================================================================

def format_test_result(result: Optional[TestResult]) -> str:
    """Format a single test result as Markdown."""
    if not result:
        return "*No data available*\n"

    if not result.success:
        return f"*Test failed: {result.error_message}*\n"

    # Calculate failed requests
    total_success = sum(v for k, v in result.status_codes.items() if k.startswith("2"))
    failed = result.requests - total_success

    output = f"""| Metric | Value |
|--------|-------|
| Total Requests | {result.requests} |
| Duration | {result.duration_s}s |
| Requests/sec | {result.rate} |
| Success Rate | {result.success_rate}% |
| Failed Requests | {failed} |

**Latency**

| Percentile | Value |
|------------|-------|
| Mean | {result.latency_mean_ms}ms |
| P50 | {result.latency_p50_ms}ms |
| P90 | {result.latency_p90_ms}ms |
| P95 | {result.latency_p95_ms}ms |
| P99 | {result.latency_p99_ms}ms |
| Max | {result.latency_max_ms}ms |

**Status Codes**

| Code | Count |
|------|-------|
"""
    for code, count in sorted(result.status_codes.items()):
        output += f"| {code} | {count} |\n"

    if result.errors:
        output += f"\n**Errors** ({len(result.errors)} total)\n\n```\n"
        for err in result.errors[:5]:
            output += f"{err}\n"
        if len(result.errors) > 5:
            output += f"... and {len(result.errors) - 5} more\n"
        output += "```\n"

    return output


def format_report(report: LoadTestReport) -> str:
    """Format the complete report as Markdown."""
    output = "## Load Test Results\n\n"

    # Warnings
    if report.warnings:
        output += "### Warnings\n\n"
        for warning in report.warnings:
            output += f"- {warning}\n"
        output += "\n"

    # Errors
    if report.errors:
        output += "### Errors\n\n"
        for error in report.errors:
            output += f"- {error}\n"
        output += "\n"

    # Test results
    output += "### foo.localhost\n\n"
    output += format_test_result(report.foo_result)
    output += "\n---\n\n"

    output += "### bar.localhost\n\n"
    output += format_test_result(report.bar_result)
    output += "\n---\n\n"

    output += "### Combined (foo + bar)\n\n"
    output += format_test_result(report.combined_result)
    output += "\n---\n\n"

    # Resource metrics
    output += "### Resource Utilization\n\n"
    if report.resource_metrics:
        output += "| Pod | CPU | Memory |\n|-----|-----|--------|\n"
        for m in sorted(report.resource_metrics, key=lambda x: x.pod):
            output += f"| {m.pod} | {m.cpu_percent}% | {m.memory_mb}MB |\n"
    else:
        output += "*No metrics available*\n"

    return output


# =============================================================================
# Main
# =============================================================================

def parse_args() -> Config:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="CI Load Test Runner")
    parser.add_argument("--namespace", default="echo-test", help="Kubernetes namespace")
    parser.add_argument("--rate", type=int, default=100, help="Requests per second")
    parser.add_argument("--duration", default="30s", help="Test duration (e.g., 30s, 1m)")
    parser.add_argument("--output-dir", default="./results", help="Output directory")
    parser.add_argument("--timeout", type=int, default=180, help="Wait timeout in seconds")
    parser.add_argument("--skip-metrics", action="store_true", help="Skip Prometheus metrics")
    parser.add_argument("--fail-rate", type=int, default=0,
                        help="Percentage of requests to fail (0-100), sent to non-existent endpoint")

    args = parser.parse_args()

    # Validate fail_rate
    fail_rate = max(0, min(100, args.fail_rate))

    return Config(
        namespace=args.namespace,
        rate=args.rate,
        duration=args.duration,
        output_dir=Path(args.output_dir),
        timeout=args.timeout,
        skip_metrics=args.skip_metrics,
        fail_rate=fail_rate
    )


def main() -> int:
    """Main entry point. Returns exit code."""
    config = parse_args()
    report = LoadTestReport()

    log("=" * 50)
    log("CI Load Test Runner")
    log("=" * 50)
    log(f"Namespace: {config.namespace}")
    log(f"Rate: {config.rate} req/s")
    log(f"Duration: {config.duration}")
    log(f"Fail Rate: {config.fail_rate}%")
    log(f"Output: {config.output_dir}")
    log("")

    # Phase 1: Validation
    log("Phase 1: Validating prerequisites...")
    errors = validate_prerequisites()
    if errors:
        for error in errors:
            log(error, "ERROR")
        return 1

    try:
        validate_output_dir(config)
    except RuntimeError as e:
        log(str(e), "ERROR")
        return 1
    log("Prerequisites validated", "OK")

    # Phase 2: Wait for resources
    log("")
    log("Phase 2: Waiting for resources...")
    if not wait_for_resources(config, report):
        log("Resource readiness check failed", "ERROR")
        for error in report.errors:
            log(error, "ERROR")
        return 1

    # Phase 3: Run load tests
    log("")
    log("Phase 3: Running load tests...")
    run_load_tests(config, report)

    # Phase 4: Collect metrics
    log("")
    log("Phase 4: Collecting metrics...")
    collect_metrics(config, report)

    # Phase 5: Format and output report
    log("")
    log("Phase 5: Generating report...")
    markdown_report = format_report(report)

    # Save report
    report_path = config.output_dir / "report.md"
    report_path.write_text(markdown_report)
    log(f"Report saved to {report_path}", "OK")

    # Print report to stdout (for CI to capture)
    print("\n" + "=" * 50)
    print("REPORT OUTPUT (for PR comment)")
    print("=" * 50 + "\n")
    print(markdown_report)

    # Determine exit code
    all_tests_passed = all([
        report.foo_result and report.foo_result.success,
        report.bar_result and report.bar_result.success,
        report.combined_result and report.combined_result.success
    ])

    if all_tests_passed:
        log("All tests completed successfully", "OK")
        return 0
    else:
        log("Some tests failed - PR merge will be blocked", "ERROR")
        return 1  # Non-zero exit blocks PR merge


if __name__ == "__main__":
    sys.exit(main())
