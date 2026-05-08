# OpenTelemetry Java/Tomcat Automation

This workspace contains:

- `otel_tomcat_setup.py`: prechecks, OpenTelemetry Java agent configuration, service restart, and post-restart verification.
- `otel_collector_prometheus_config.yaml`: sample OpenTelemetry Collector config exposing metrics on port `9464` for Prometheus.
- `grafana_golden_metrics_dashboard.json`: importable Grafana dashboard for golden metrics from Prometheus.

## Prechecks

Run this first on the Ubuntu application server:

```bash
python3 otel_tomcat_setup.py \
  --service-name my-springboot-service \
  --java-agent /opt/opentelemetry/opentelemetry-javaagent.jar \
  --collector-service auto \
  --precheck-only
```

The two required prechecks are:

- Java agent present at `--java-agent`.
- OpenTelemetry Collector present and running as `otelcol`, `otelcol-contrib`, or `opentelemetry-collector`.

## Configure Tomcat and Restart

For a normal Ubuntu Tomcat 9 install:

```bash
sudo python3 otel_tomcat_setup.py \
  --service-name my-springboot-service \
  --java-agent /opt/opentelemetry/opentelemetry-javaagent.jar \
  --collector-service auto \
  --app-service tomcat9 \
  --tomcat-bin /var/lib/tomcat9/bin \
  --otel-endpoint http://localhost:4317 \
  --otel-protocol grpc \
  --collector-health-url http://localhost:13133/ \
  --app-health-url http://localhost:8080/actuator/health \
  --prometheus-url http://localhost:9090
```

The script creates or updates `/var/lib/tomcat9/bin/setenv.sh` with an idempotent managed block:

```bash
export CATALINA_OPTS="$CATALINA_OPTS -javaagent:/opt/opentelemetry/opentelemetry-javaagent.jar ..."
```

Then it restarts `tomcat9` and verifies that the service is active. If `--app-health-url` is provided, it also waits for the application health endpoint.

## Configure a Spring Boot systemd Service

If your Spring Boot app runs directly as a systemd service rather than inside Tomcat:

```bash
sudo python3 otel_tomcat_setup.py \
  --config-mode systemd \
  --service-name my-springboot-service \
  --java-agent /opt/opentelemetry/opentelemetry-javaagent.jar \
  --app-service my-app.service \
  --otel-endpoint http://localhost:4317 \
  --app-health-url http://localhost:8080/actuator/health
```

This writes `/etc/systemd/system/my-app.service.d/otel.conf` and runs `systemctl daemon-reload`.

## Collector to Prometheus

Your OpenTelemetry Collector should expose metrics for Prometheus to scrape. A ready-to-use sample is included in `otel_collector_prometheus_config.yaml`.

It contains this shape:

```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318

exporters:
  prometheus:
    endpoint: 0.0.0.0:9464

service:
  extensions: [health_check]
  pipelines:
    metrics:
      receivers: [otlp]
      exporters: [prometheus]
```

Example collector start command:

```bash
otelcol-contrib --config ./otel_collector_prometheus_config.yaml
```

Prometheus scrape config example:

```yaml
scrape_configs:
  - job_name: otel-collector
    static_configs:
      - targets: ["localhost:9464"]
```

## Grafana Dashboard

Import `grafana_golden_metrics_dashboard.json` in Grafana and select your Prometheus datasource.

The dashboard includes:

- Traffic: request rate.
- Errors: HTTP 5xx ratio.
- Latency: p95 and p99.
- Availability.
- Service up.
- JVM memory.
- JVM CPU.

Metric names vary by OpenTelemetry Java agent and collector translation settings. If your Prometheus labels use `service_name`, the dashboard should work as-is. If your labels are different, update the PromQL label selector in each panel.
