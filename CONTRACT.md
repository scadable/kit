# The contract

This is what every SCADABLE service looks like from the outside, in any
language. The Python package in this repository is one implementation of it and
happens to be the first.

Why it is written down separately from the code: the plan is to build in Python
and rewrite individual services in another language when a real bottleneck
appears, most likely in repository parsing. On the day that happens, the
rewritten service still has to emit the same error envelope, the same readiness
document and the same log fields, or every dashboard, alert and client breaks at
the boundary between the rewritten service and the rest of the fleet. A
reimplementation needs something to conform to that is not "read the Python and
guess".

Everything on this page is **fixed**. Everything not on this page is a service's
own business.

## Errors

Every error response, from any service, at any status code:

```json
{
  "error": { "code": "not_found", "message": "No policy with that id" },
  "request_id": "01JC8Z2Q7X8V3K1M4N5P6R7S8T"
}
```

- `error.code` is snake_case and stable. Clients branch on it, so renaming one
  is a breaking change.
- `error.message` is for humans and may change freely.
- `request_id` is always present, and matches the `X-Request-ID` response header.
- No other top level keys. Not `detail`, which is what FastAPI emits by default
  and must be overridden.

Codes in use across the fleet: `invalid_request`, `unauthenticated`,
`forbidden`, `not_found`, `conflict`, `already_exists`, `rate_limited`,
`upstream_unavailable`, `not_configured`, `internal_error`. A service may add
its own; it may not redefine these.

## Health

`GET /healthz` answers whether the process is alive. It touches no dependency
and returns `200` with `{"status":"ok"}`. A platform health check restarts the
container on failure, so a database blip must not be able to reach this.

`GET /readyz` **always returns HTTP 200.** The verdict is in the body:

```json
{
  "status": "ready",
  "checks": { "postgres": "ready", "valkey": "unconfigured", "objects": "error" }
}
```

- `status` is `ready` or `not_ready`.
- `checks` is a flat map of name to state and is always present, even when empty.
- A state is exactly one of `ready`, `error`, `unconfigured`.
- Dependency names are declared **before** wiring is attempted, so a dependency
  that failed to construct reports `error` and one that was never configured
  reports `unconfigured`. Neither may be absent from the map.
- Driver errors and connection strings never appear in the body. `/readyz` is
  unauthenticated on public ingress.

The always-200 rule exists because the DigitalOcean App Platform edge replaces
an upstream 5xx with its own HTML page, which discards the report entirely.

## Request identity

Every response carries `X-Request-ID`. If the request arrived with one, it is
propagated; otherwise the service generates it. It appears in the error envelope
and on every log line for that request.

## Logs

JSON to stdout, one object per line. Required fields on a request log line:

| Field | Meaning |
| --- | --- |
| `level` | `debug` / `info` / `warn` / `error` |
| `msg` | Short, stable, not interpolated with ids |
| `request_id` | As above |
| `trace_id`, `span_id` | Present whenever a span is active |
| `method` | HTTP method |
| `route` | The route **pattern**, for example `/api/v1/policies/{id}` |
| `status` | HTTP status |
| `duration_ms` | Number |

`route` is the pattern and never the resolved URL. A URL accumulates ids and
tokens, and a log line is the wrong place to collect them. Unmatched requests
log `route: "unmatched"`.

## Telemetry

OpenTelemetry over OTLP, push only. There is no `/metrics` endpoint and no
Prometheus dependency anywhere in the fleet: replicas have separate
filesystems, so a load-balanced scrape reaches exactly one of them.

Standard `OTEL_*` environment variables, unprefixed. An empty exporter endpoint
means no exporter is installed, and the service still logs JSON to stdout.

## Configuration

Every variable is namespaced with the service name, uppercased:
`BILLING_DATABASE_URL`. Two exceptions, both because something else owns the
name: `PORT`, injected by the platform, and `OTEL_*`, read by the OTel SDK.

Process settings fail fast: a malformed port stops startup. Backing systems fail
soft into three states, absent, working and broken, so a broken block becomes a
failing readiness check rather than a service that cannot boot.

## Ports and shutdown

Listen on `$PORT`, default 8080. Handle `SIGTERM` by draining within the
configured shutdown timeout. Hold no durable state on the container filesystem.

## Conforming

A new implementation in another language is correct when it passes the
conformance suite in `src/kit/testing/`, which drives an implementation over
HTTP and asserts every statement on this page. Run it against the candidate
before routing traffic at it.
