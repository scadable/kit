"""Configuration loading. Import from ``kit.config``."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

DEFAULT_PORT = 8080
DEFAULT_LOG_LEVEL = "info"
DEFAULT_SHUTDOWN_SECONDS = 10.0
DEFAULT_MIGRATION_SECONDS = 120.0
DEFAULT_ENVIRONMENT = "development"
DEFAULT_VERSION = "dev"

DEFAULT_POOL_MAX = 10
DEFAULT_POOL_MIN = 0
DEFAULT_POOL_LIFETIME_SECONDS = 1800.0
DEFAULT_POOL_IDLE_SECONDS = 300.0
DEFAULT_HEALTH_TIMEOUT_SECONDS = 5.0

_UNPREFIXED = ("PORT",)
_UNPREFIXED_PREFIX = "OTEL_"


class ConfigError(ValueError):
    """A setting that cannot be used.

    Whether this stops the process depends on what it describes. Process
    settings raise; backing systems record it and keep serving.
    """


@dataclass(frozen=True, slots=True)
class Service:
    """What a process tells the kit about itself."""

    name: str
    env_prefix: str
    """Namespaces every setting this service reads, trailing underscore
    included, for example ``POLICY_``.

    Prefixing exists because one environment file covers several services, so an
    unprefixed ``DB_HOST`` is ambiguous the moment a second service wants a
    database.
    """


class Lookup(Protocol):
    """Reads one namespaced setting, with a fallback when it is unset."""

    def __call__(self, key: str, fallback: str = "") -> str: ...


def scoped(env_prefix: str, environ: dict[str, str] | None = None) -> Lookup:
    """A reader that namespaces every name except the two we do not own.

    ``PORT`` is injected unprefixed by the platform to say which port to listen
    on, and every ``OTEL_`` name is defined by the OpenTelemetry specification
    and read directly by its SDK, so renaming ours would leave the two halves of
    the exporter configuration disagreeing.
    """
    source = dict(os.environ) if environ is None else environ

    def read(key: str, fallback: str = "") -> str:
        name = key if key in _UNPREFIXED or key.startswith(_UNPREFIXED_PREFIX) else env_prefix + key
        # Trimmed, and empty-after-trim counts as unset. An operator who cleared
        # a variable by blanking it meant to unset it.
        value = source.get(name, "").strip()
        return value or fallback

    return read


@dataclass(frozen=True, slots=True)
class Process:
    """Settings that describe the process itself. These fail fast.

    A malformed port has no subsystem to degrade into: there is nowhere for the
    failure to surface except a refusal to start.
    """

    port: int = DEFAULT_PORT
    log_level: str = DEFAULT_LOG_LEVEL
    shutdown_seconds: float = DEFAULT_SHUTDOWN_SECONDS
    migration_seconds: float = DEFAULT_MIGRATION_SECONDS
    environment: str = DEFAULT_ENVIRONMENT
    version: str = DEFAULT_VERSION
    service_name: str = "unknown-service"
    """Deliberately ugly. Telemetry from several services lands in one backend,
    and this appearing on a dashboard is a bug report, not a default worth living
    with."""


@dataclass(frozen=True, slots=True)
class Database:
    """PostgreSQL settings, which fail SOFT."""

    url: str = ""
    pool_max: int = DEFAULT_POOL_MAX
    pool_min: int = DEFAULT_POOL_MIN
    pool_lifetime_seconds: float = DEFAULT_POOL_LIFETIME_SECONDS
    pool_idle_seconds: float = DEFAULT_POOL_IDLE_SECONDS
    health_timeout_seconds: float = DEFAULT_HEALTH_TIMEOUT_SECONDS
    error: ConfigError | None = None

    @property
    def enabled(self) -> bool:
        """Whether the operator ATTEMPTED to configure a database.

        True even when the attempt was invalid, and that is the point. A
        subsystem has three states, not two: absent, working and broken. A typo
        in the host must not make the subsystem vanish, because "configured but
        failing" and "deliberately not configured" would then be
        indistinguishable, and a service storing nothing would look identical to
        a healthy one.
        """
        return bool(self.url) or self.error is not None


@dataclass(frozen=True, slots=True)
class Cache:
    """Valkey settings. Cache only, never a source of truth, and fail soft."""

    url: str = ""
    key_prefix: str = ""
    health_timeout_seconds: float = DEFAULT_HEALTH_TIMEOUT_SECONDS
    error: ConfigError | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.url) or self.error is not None


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything the kit reads, in one object built once at startup."""

    service: Service
    process: Process = field(default_factory=Process)
    database: Database = field(default_factory=Database)
    cache: Cache = field(default_factory=Cache)
    _read: Lookup | None = None

    def value(self, key: str, fallback: str = "") -> str:
        """Read a setting this service owns, through the same namespace.

        The escape hatch that stops the shared config object growing a field per
        service.
        """
        if self._read is None:
            return fallback
        return self._read(key, fallback)


def _positive_int(read: Lookup, key: str, fallback: int) -> int:
    raw = read(key)
    if not raw:
        return fallback
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{key} must be a whole number, got {raw!r}") from None
    if value < 1:
        raise ConfigError(f"{key} must be at least 1, got {value}")
    return value


def _seconds(read: Lookup, key: str, fallback: float) -> float:
    raw = read(key)
    if not raw:
        return fallback
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{key} must be a number of seconds, got {raw!r}") from None
    if value <= 0:
        raise ConfigError(f"{key} must be greater than zero, got {value}")
    return value


def validate_database_url(raw: str) -> str:
    """Check the URL without ever repeating it.

    The parse error is NOT included, and that is the point. A URL parser's
    message quotes the whole input, this value is a connection string with a
    password in it, and the error is logged at startup while the service keeps
    serving so readiness can report it. One typo would then write a credential
    into the log system and to everyone who can read it. Nothing else has to go
    wrong.

    Not even the underlying reason is passed through, because that reason
    routinely quotes a fragment of the password itself.
    """
    try:
        parsed = urlsplit(raw)
        # Read inside the try, not after it. urlsplit is LAZY: it accepts a
        # malformed authority and raises when .hostname or .port is first
        # touched. Reading these outside would let that ValueError escape
        # unwrapped, and its message quotes the whole URL, which is the exact
        # leak this function exists to prevent.
        scheme = parsed.scheme
        hostname = parsed.hostname
        path = parsed.path
        # Reading .port is what actually validates the authority: urlsplit does
        # not check it until asked. Checked here rather than left to the driver
        # so a bad port fails at startup with a readable message instead of at
        # first connection with the driver's.
        _port_is_valid = parsed.port
        del _port_is_valid
    except ValueError:
        raise ConfigError(
            "DATABASE_URL is not a valid URL. Its contents are not repeated here "
            "because it carries a password; check the escaping in the password, "
            "the port, and that the value has no spaces or line breaks"
        ) from None

    if scheme not in ("postgres", "postgresql"):
        raise ConfigError("DATABASE_URL must use the postgres or postgresql scheme")
    if not hostname:
        raise ConfigError("DATABASE_URL names no host")
    if not path.strip("/"):
        raise ConfigError("DATABASE_URL names no database")
    return raw


def load(service: Service, environ: dict[str, str] | None = None) -> Settings:
    """Read everything, failing fast on the process and soft on the stores."""
    read = scoped(service.env_prefix, environ)

    process = Process(
        port=_positive_int(read, "PORT", DEFAULT_PORT),
        log_level=read("LOG_LEVEL", DEFAULT_LOG_LEVEL).lower(),
        shutdown_seconds=_seconds(read, "SHUTDOWN_TIMEOUT", DEFAULT_SHUTDOWN_SECONDS),
        migration_seconds=_seconds(read, "MIGRATION_TIMEOUT", DEFAULT_MIGRATION_SECONDS),
        environment=read("DEPLOYMENT_ENVIRONMENT", DEFAULT_ENVIRONMENT),
        version=read("SERVICE_VERSION", DEFAULT_VERSION),
        service_name=service.name or "unknown-service",
    )

    # Backing systems fail SOFT. A malformed block records its error instead of
    # aborting startup, so one typo cannot take the service down. The error is
    # not swallowed: `enabled` stays true, the caller registers a readiness check
    # that always fails with it, and readiness reports that dependency as error.
    # Booting and going red is both survivable and honest, where crashing is
    # neither survivable nor more informative.
    try:
        url = read("DATABASE_URL")
        database = Database(
            url=validate_database_url(url) if url else "",
            pool_max=_positive_int(read, "DB_POOL_MAX", DEFAULT_POOL_MAX),
            pool_lifetime_seconds=_seconds(read, "DB_POOL_LIFETIME", DEFAULT_POOL_LIFETIME_SECONDS),
            health_timeout_seconds=_seconds(
                read, "DB_HEALTH_TIMEOUT", DEFAULT_HEALTH_TIMEOUT_SECONDS
            ),
        )
    except ConfigError as error:
        # The defaults are carried through, so a consumer that reads a pool size
        # before checking the error does not build a pool of zero connections.
        database = Database(error=error)

    try:
        cache = Cache(
            url=read("CACHE_URL"),
            key_prefix=read("CACHE_KEY_PREFIX", service.name),
            health_timeout_seconds=_seconds(
                read, "CACHE_HEALTH_TIMEOUT", DEFAULT_HEALTH_TIMEOUT_SECONDS
            ),
        )
    except ConfigError as error:
        cache = Cache(key_prefix=service.name, error=error)

    return Settings(
        service=service,
        process=process,
        database=database,
        cache=cache,
        _read=read,
    )
