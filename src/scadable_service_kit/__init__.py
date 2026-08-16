"""Cross-cutting behaviour shared by every SCADABLE Python service.

This package is vendored today and extracted tomorrow. It is already named
what it will be called on PyPI, so extraction is a directory move plus one
line in pyproject.toml, and zero import rewrites.

The version below is logged at service boot, which is the only cheap way to
answer 'who is running a stale kit' across a fleet of copies.
"""

__version__ = "0.1.0"
