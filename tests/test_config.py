"""Configuration: what fails fast, what degrades, and what never gets logged."""

from __future__ import annotations

import pytest

from kit.config import ConfigError, Service, load, scoped

SERVICE = Service(name="kit-service", env_prefix="KIT_")
"""Deliberately not the name of a real service, so an assertion can never pass
because a default was hardcoded to whichever service was being tested."""


def test_every_setting_is_namespaced_except_the_two_we_do_not_own() -> None:
    read = scoped(
        "KIT_",
        {
            "KIT_LOG_LEVEL": "debug",
            "LOG_LEVEL": "error",
            "PORT": "9090",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "https://collector.example.com",
        },
    )

    assert read("LOG_LEVEL") == "debug", "unprefixed LOG_LEVEL leaked in"
    assert read("PORT") == "9090"
    assert read("OTEL_EXPORTER_OTLP_ENDPOINT") == "https://collector.example.com"


def test_a_blanked_variable_counts_as_unset() -> None:
    """An operator who cleared a variable by blanking it meant to unset it."""
    read = scoped("KIT_", {"KIT_LOG_LEVEL": "   "})

    assert read("LOG_LEVEL", "info") == "info"


def test_defaults() -> None:
    settings = load(SERVICE, {})

    assert settings.process.port == 8080
    assert settings.process.log_level == "info"
    assert settings.process.environment == "development"
    assert settings.process.version == "dev"
    assert settings.database.enabled is False
    assert settings.cache.key_prefix == "kit-service"


@pytest.mark.parametrize("port", ["http", "0", "-1"])
def test_a_bad_port_stops_startup(port: str) -> None:
    """Fail fast: a bad port has no subsystem to degrade into."""
    with pytest.raises(ConfigError, match="PORT"):
        load(SERVICE, {"PORT": port})


def test_a_broken_database_block_boots_and_goes_red() -> None:
    """Fail soft. Booting and going red is survivable and honest; crashing is
    neither survivable nor more informative."""
    settings = load(SERVICE, {"KIT_DATABASE_URL": "mysql://host/db"})

    assert settings.database.error is not None
    assert settings.database.enabled is True, (
        "a misconfigured database vanished from readiness instead of reporting red"
    )


def test_a_degraded_block_still_carries_its_defaults() -> None:
    """So a consumer that reads a pool size before checking the error does not
    build a pool of zero connections."""
    settings = load(SERVICE, {"KIT_DATABASE_URL": "mysql://host/db"})

    assert settings.database.pool_max == 10
    assert settings.database.health_timeout_seconds == 5.0


def test_an_absent_database_is_not_enabled() -> None:
    """Absent and broken must stay distinguishable."""
    settings = load(SERVICE, {})

    assert settings.database.enabled is False
    assert settings.database.error is None


def test_a_malformed_database_url_never_repeats_its_own_password() -> None:
    """The assertion is on the error string because the error string IS the
    behaviour: this is about what gets written down.

    The value is a connection string with a password in it, the error is logged
    at startup while the service keeps serving, and the credential is shared. One
    typo would otherwise write it into the log system and to everyone who can
    read it.
    """
    password = "sup3r-secret-value"  # noqa: S105 - the point of the test
    user = "pgadmin"
    host = "db.internal.example.com"
    url = f"mysql://{user}:{password}@{host}:5432/app"

    settings = load(SERVICE, {"KIT_DATABASE_URL": url})
    message = str(settings.database.error)

    for label, secret in {
        "the password": password,
        "a password fragment": "sup3r",
        "the user": user,
        "the host": host,
        "the whole URL": url,
    }.items():
        assert secret not in message, f"{label} appears in {message!r}"

    # And it still has to be usable. An error naming nothing sends an operator to
    # read the source.
    assert "DATABASE_URL" in message


def test_value_reads_service_specific_settings_through_the_same_namespace() -> None:
    settings = load(
        SERVICE,
        {"KIT_ADMIN_TOKEN": "  the-token  ", "ADMIN_TOKEN": "another-service-token"},
    )

    assert settings.value("ADMIN_TOKEN") == "the-token"
    assert settings.value("NOT_SET", "fallback") == "fallback"
