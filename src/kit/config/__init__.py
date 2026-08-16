"""Configuration loading, shared by every SCADABLE service.

Every variable is namespaced by the service prefix (``BILLING_DATABASE_URL``).
Two deliberate exceptions, and they are not negotiable: ``PORT``, which the
platform injects unprefixed to say which port to listen on, and ``OTEL_*``,
which the OpenTelemetry SDK reads itself.

Process settings (port, log level, timeouts) fail fast: a malformed value stops
startup, because a bad port has no subsystem to degrade into. Backing systems
fail soft into three states, absent / working / broken, so a broken block becomes
a readiness check that always fails rather than a service that will not boot.

That third state is the whole design. A typo in a host must not make the
subsystem vanish, or "configured but failing" and "deliberately not configured"
become indistinguishable, and a service storing nothing looks exactly like a
healthy one.
"""

from kit.config._settings import (
    Cache,
    ConfigError,
    Database,
    Process,
    Service,
    Settings,
    load,
    scoped,
    validate_database_url,
)

__all__ = [
    "Cache",
    "ConfigError",
    "Database",
    "Process",
    "Service",
    "Settings",
    "load",
    "scoped",
    "validate_database_url",
]
