"""Pytest fixtures every service inherits.

The point is that a service gets its /readyz contract test, its error-envelope
test and its ASGI client for free, so the conventions are verified in every
repo rather than in the one repo whose author remembered.
"""
