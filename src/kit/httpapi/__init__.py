"""The HTTP conventions every service shares.

Request id generation and the X-Request-ID response header, the request logger
that records the route PATTERN and never the URL, the error envelope
{"error": {"code", "message"}, "request_id"}, the shared /readyz handler, CORS,
and the in-process rate limiter.

This package owns how a response looks. It does not own routing, and it never
knows what any particular service exposes.
"""
