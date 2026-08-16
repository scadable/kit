"""Pytest fixtures and the conformance suite.

Two jobs. The fixtures let a service inherit its contract tests, so the /readyz
shape, the error envelope and the request-id header are verified in every
repository rather than in whichever one remembered.

The conformance suite is the other half, and it matters more later than now: it
drives an implementation over HTTP and asserts every statement in CONTRACT.md.
When a service is rewritten in another language for throughput, this is what it
runs against before anything is routed at it.
"""
