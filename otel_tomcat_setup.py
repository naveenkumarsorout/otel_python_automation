#!/usr/bin/env python3
"""
Precheck and configure OpenTelemetry Java agent for a Tomcat/Spring Boot service.

This script is intentionally conservative:
  * it checks whether the Java agent file exists
  * it checks whether the OpenTelemetry Collector service is installed/running
  * it writes an idempotent managed config block for Tomcat or systemd
  * it restarts the application service
  * it verifies service health after restart
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterable, List, Optional, Tuple


MANAGED_BEGIN = "# BEGIN MANAGED BY otel_tomcat_setup.py"
MANAGED_END = "# END MANAGED BY otel_tomcat_setup.py"


class SetupError(Exception):
    """Raised for expected setup failures."""


def log(message: str) -> None:
    print(message, flush=True)


def run(command: List[str], check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def require_root_for_writes(dry_run: bool) -> None:
    if dry_run:
        return
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        raise SetupError("Configuration writes and service restarts usually require sudo/root.")


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def systemctl_available() -> bool:
    return command_exists("systemctl")


def systemd_service_exists(service: str) -> bool:
    if not systemctl_available():
        return False
    result = run(["systemctl", "status", service])
    return result.returncode in (0, 3)


def systemd_service_active(service: str) -> bool:
    if not systemctl_available():
        return False
    result = run(["systemctl", "is-active", "--quiet", service])
    return result.returncode == 0


def first_existing_service(candidates: Iterable[str]) -> Optional[str]:
    for service in candidates:
        if service and systemd_service_exists(service):
            return service
    return None


def http_get(url: str, timeout: int = 5) -> Tuple[bool, str]:
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "otel-precheck/1.0"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(200).decode("utf-8", errors="replace")
            return 200 <= response.status < 400, "HTTP %s %s" % (response.status, body.strip())
    except urllib.error.HTTPError as exc:
        return False, "HTTP %s" % exc.code
    except Exception as exc:
        return False, str(exc)


def prometheus_query(prometheus_url: str, query: str, timeout: int = 5) -> Tuple[bool, str]:
    base = prometheus_url.rstrip("/")
    encoded = urllib.parse.urlencode({"query": query})
    ok, message = http_get("%s/api/v1/query?%s" % (base, encoded), timeout=timeout)
    return ok, message


def build_otel_java_options(args: argparse.Namespace) -> List[str]:
    options = [
        "-javaagent:%s" % args.java_agent,
        "-Dotel.service.name=%s" % args.service_name,
        "-Dotel.traces.exporter=%s" % args.traces_exporter,
        "-Dotel.metrics.exporter=%s" % args.metrics_exporter,
        "-Dotel.logs.exporter=%s" % args.logs_exporter,
        "-Dotel.exporter.otlp.endpoint=%s" % args.otel_endpoint,
        "-Dotel.exporter.otlp.protocol=%s" % args.otel_protocol,
    ]
    if args.resource_attributes:
        options.append("-Dotel.resource.attributes=%s" % args.resource_attributes)
    if args.extra_otel_property:
        for item in args.extra_otel_property:
            key, value = parse_key_value(item, "--extra-otel-property")
            options.append("-D%s=%s" % (key, value))
    return options


def shell_join_options(options: Iterable[str]) -> str:
    return " ".join(options)


def parse_key_value(raw: str, flag_name: str) -> Tuple[str, str]:
    if "=" not in raw:
        raise SetupError("%s must be in key=value format: %s" % (flag_name, raw))
    key, value = raw.split("=", 1)
    if not key.strip():
        raise SetupError("%s has an empty key: %s" % (flag_name, raw))
    return key.strip(), value.strip()


def replace_managed_block(existing: str, block: str) -> str:
    if MANAGED_BEGIN in existing and MANAGED_END in existing:
        before, rest = existing.split(MANAGED_BEGIN, 1)
        _, after = rest.split(MANAGED_END, 1)
        return before.rstrip() + "\n\n" + block.rstrip() + "\n" + after.lstrip()
    suffix = "" if existing.endswith("\n") or not existing else "\n"
    return existing + suffix + "\n" + block.rstrip() + "\n"


def write_file_idempotent(path: Path, content: str, mode: int, dry_run: bool) -> bool:
    existing = path.read_text() if path.exists() else None
    if existing == content:
        log("No change needed: %s" % path)
        return False
    if dry_run:
        log("DRY RUN: would write %s" % path)
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    os.chmod(str(path), mode)
    log("Updated: %s" % path)
    return True


def configure_tomcat_setenv(args: argparse.Namespace) -> bool:
    tomcat_bin = Path(args.tomcat_bin)
    setenv = tomcat_bin / "setenv.sh"
    if not tomcat_bin.exists():
        raise SetupError("Tomcat bin directory not found: %s" % tomcat_bin)

    options = shell_join_options(build_otel_java_options(args))
    managed_block = "\n".join(
        [
            MANAGED_BEGIN,
            'export OTEL_RESOURCE_ATTRIBUTES="%s"' % (args.resource_attributes or ""),
            'export CATALINA_OPTS="$CATALINA_OPTS %s"' % options,
            MANAGED_END,
        ]
    )
    existing = setenv.read_text() if setenv.exists() else "#!/bin/sh\n"
    return write_file_idempotent(setenv, replace_managed_block(existing, managed_block), 0o755, args.dry_run)


def configure_systemd_override(args: argparse.Namespace) -> bool:
    override_dir = Path("/etc/systemd/system/%s.d" % args.app_service)
    override_file = override_dir / "otel.conf"
    options = shell_join_options(build_otel_java_options(args))
    managed_block = "\n".join(
        [
            "[Service]",
            'Environment="JAVA_TOOL_OPTIONS=%s"' % options,
        ]
    )
    changed = write_file_idempotent(override_file, managed_block + "\n", 0o644, args.dry_run)
    if changed and not args.dry_run:
        run(["systemctl", "daemon-reload"], check=True)
        log("Reloaded systemd daemon")
    return changed


def restart_service(service: str, dry_run: bool) -> None:
    if dry_run:
        log("DRY RUN: would restart %s" % service)
        return
    log("Restarting service: %s" % service)
    result = run(["systemctl", "restart", service])
    if result.returncode != 0:
        raise SetupError("Failed to restart %s: %s" % (service, result.stderr.strip()))


def wait_for_service(service: str, timeout_seconds: int) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if systemd_service_active(service):
            return True
        time.sleep(2)
    return False


def check_preconditions(args: argparse.Namespace) -> Tuple[bool, Optional[str]]:
    all_ok = True

    java_agent_path = Path(args.java_agent)
    if java_agent_path.exists():
        log("OK: Java agent is present: %s" % java_agent_path)
    else:
        log("NOT PRESENT: Java agent is not present: %s" % java_agent_path)
        all_ok = False

    collector_service = args.collector_service
    if collector_service == "auto":
        collector_service = first_existing_service(["otelcol", "otelcol-contrib", "opentelemetry-collector"])

    if collector_service:
        if systemd_service_exists(collector_service):
            if systemd_service_active(collector_service):
                log("OK: OpenTelemetry Collector service is running: %s" % collector_service)
            else:
                log("NOT RUNNING: OpenTelemetry Collector service exists but is not active: %s" % collector_service)
                all_ok = False
        else:
            log("NOT PRESENT: OpenTelemetry Collector service is not present: %s" % collector_service)
            all_ok = False
    elif command_exists("otelcol") or command_exists("otelcol-contrib"):
        log("OK: OpenTelemetry Collector binary is present")
    else:
        log("NOT PRESENT: OpenTelemetry Collector service/binary is not present")
        all_ok = False

    if args.collector_health_url:
        ok, message = http_get(args.collector_health_url)
        if ok:
            log("OK: Collector health endpoint responded: %s" % message)
        else:
            log("NOT READY: Collector health endpoint failed: %s" % message)
            all_ok = False

    return all_ok, collector_service


def verify_after_restart(args: argparse.Namespace) -> None:
    if wait_for_service(args.app_service, args.restart_timeout):
        log("OK: Application service is active after restart: %s" % args.app_service)
    else:
        raise SetupError("Application service is not active after restart: %s" % args.app_service)

    if args.app_health_url:
        deadline = time.time() + args.restart_timeout
        last_message = ""
        while time.time() < deadline:
            ok, last_message = http_get(args.app_health_url)
            if ok:
                log("OK: Application health endpoint responded: %s" % last_message)
                break
            time.sleep(2)
        else:
            raise SetupError("Application health endpoint did not recover: %s" % last_message)

    if args.prometheus_url:
        ok, message = prometheus_query(args.prometheus_url, 'up{job=~".*otel.*|.*collector.*|.*%s.*"}' % args.service_name)
        if ok:
            log("OK: Prometheus query endpoint is reachable: %s" % message)
        else:
            log("WARNING: Prometheus query endpoint check failed: %s" % message)


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Precheck and configure OpenTelemetry Java agent for Tomcat/Spring Boot."
    )
    parser.add_argument("--java-agent", default="/opt/opentelemetry/opentelemetry-javaagent.jar")
    parser.add_argument("--collector-service", default="auto", help="Use auto, otelcol, otelcol-contrib, etc.")
    parser.add_argument("--collector-health-url", default=None, help="Example: http://localhost:13133/")
    parser.add_argument("--app-service", default="tomcat9", help="systemd service to restart, for example tomcat9")
    parser.add_argument("--tomcat-bin", default="/var/lib/tomcat9/bin", help="Directory containing setenv.sh")
    parser.add_argument("--config-mode", choices=["tomcat-setenv", "systemd"], default="tomcat-setenv")
    parser.add_argument("--service-name", required=True, help="OpenTelemetry service name shown in Prometheus/Grafana")
    parser.add_argument("--otel-endpoint", default="http://localhost:4317")
    parser.add_argument("--otel-protocol", choices=["grpc", "http/protobuf"], default="grpc")
    parser.add_argument("--traces-exporter", default="otlp")
    parser.add_argument("--metrics-exporter", default="otlp")
    parser.add_argument("--logs-exporter", default="none")
    parser.add_argument("--resource-attributes", default="deployment.environment=prod")
    parser.add_argument("--extra-otel-property", action="append", default=[])
    parser.add_argument("--app-health-url", default=None, help="Example: http://localhost:8080/actuator/health")
    parser.add_argument("--prometheus-url", default=None, help="Example: http://localhost:9090")
    parser.add_argument("--restart-timeout", type=int, default=90)
    parser.add_argument("--precheck-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)

    if not systemctl_available():
        log("WARNING: systemctl was not found. Service checks and restarts need systemd on Ubuntu.")

    precheck_ok, _collector_service = check_preconditions(args)
    if args.precheck_only:
        return 0 if precheck_ok else 2
    if not precheck_ok:
        return 2

    require_root_for_writes(args.dry_run)

    if args.config_mode == "tomcat-setenv":
        configure_tomcat_setenv(args)
    else:
        configure_systemd_override(args)

    restart_service(args.app_service, args.dry_run)
    if not args.dry_run:
        verify_after_restart(args)

    log("DONE: OpenTelemetry configuration and restart verification completed.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SetupError as exc:
        log("ERROR: %s" % exc)
        sys.exit(1)
