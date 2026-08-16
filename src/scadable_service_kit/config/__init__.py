"""Configuration loading, shared by every SCADABLE Python service.

Every variable is namespaced by the service prefix (TEMPLATE_DB_HOST). Two
deliberate exceptions, and they are not negotiable: PORT, which App Platform
injects unprefixed, and OTEL_*, which the OpenTelemetry SDK reads itself.

Process settings (port, log level, timeouts) fail fast: a malformed value stops
startup. Backing systems (database, cache, objects) fail soft into three states,
absent / working / broken, so a broken block becomes a readiness check that
always fails rather than a service that will not boot.
"""
