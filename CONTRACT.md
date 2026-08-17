# The contract

This is what every SCADABLE service looks like from the outside. The kit
implements it, and a service inherits it by installing the kit.

Why it is written down separately from the code: this is the part an operator,
a dashboard and a client all depend on being identical across services, and code
alone does not tell you which details are load-bearing and which are incidental.
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

One deliberate exception, stated rather than hidden: a **rejected CORS preflight**
answers with the plain-text refusal the ASGI CORS middleware generates, not this
envelope. Reimplementing CORS to wrap it would be a large amount of security-
sensitive code for a body no browser ever surfaces to page script, and a preflight
rejection is reported to the developer as a CORS error rather than read as JSON.
Every other error, including one from an unrouted path or a rate limit, uses the
envelope.

Codes in use across the fleet: `invalid_request`, `unauthenticated`,
`forbidden`, `not_found`, `conflict`, `already_exists`, `rate_limited`,
`upstream_unavailable`, `not_configured`, `internal_error`. A service may add
its own; it may not redefine these.

## Health

`GET /healthz` answers whether the process is alive. It touches no dependency
and returns `200` with `{"status":"ok"}`. A platform health check restarts the
container on failure, so a database blip must not be able to reach this.

`GET /readyz` returns **200 when every required dependency answered, and 503
when one did not.** The detail is in the body either way:

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

The status code carries the verdict because a Kubernetes readiness probe reads
the status code and nothing else. A handler that always answered 200 would
never be taken out of the Service endpoints, which is the entire purpose of
this endpoint being separate from liveness.

The body carries the detail because a status code cannot say WHICH dependency
is down, and that is what an operator needs at 3am.

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

## Proving it

`kit.testing.assert_contract` drives a service over HTTP and asserts every
statement on this page. Call it from a test in your own suite, so the contract
is verified in each service rather than assumed because the kit is installed.
