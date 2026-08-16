"""Outbound transports: httpx for REST, the Connect runtime for protobuf.

Timeouts, retry policy and trace-context propagation are set here once. Every
client timeout must be strictly less than the server's write timeout, with
headroom. When those were equal the server killed the connection at the instant
the client gave up, and an honest 503 was never written.

This package supplies transports. Typed gateways live in the service.
"""
