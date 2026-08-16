"""Logging and telemetry.

JSON to stdout, always, with trace_id and span_id injected into every line from
the active span context. OpenTelemetry over OTLP push only.

There is no Prometheus dependency and no /metrics endpoint anywhere in the
fleet. App Platform replicas have separate filesystems and a load-balanced
scrape would reach exactly one of them, so pull-based metrics cannot work here.
"""
