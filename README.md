# scadable-service-kit

What must be identical in every SCADABLE Python service, and nothing else.

One error envelope. One request id. One meaning for "ready". One way a log line
looks. If two services disagree about any of those, the fleet stops being
legible from the outside, and that is the only thing this package exists to
prevent.

## What is in here

| Package | What it owns |
| --- | --- |
| `config` | Env loading, the service prefix, the `PORT` and `OTEL_*` exceptions, fail-soft backing systems |
| `health` | The readiness registry: `require()` before wiring, three states |
| `httpapi` | Request id, pattern logging, the error envelope, the shared `/readyz` handler, CORS, rate limiting |
| `observability` | JSON logs with `trace_id` and `span_id`, OTLP push |
| `db` | Async engine construction, the tenant-scoped transaction helper |
| `cache` | Valkey, optional, cache only |
| `objects` | Spaces |
| `clients` | httpx and Connect transports, with timeouts and trace propagation |
| `testing` | Pytest fixtures, so every service inherits the contract tests |

## What is deliberately not in here

The outbox. It has zero production instances in Python, and a shared abstraction
built before its second real use case is a guess that everyone then has to work
around.

**Code moves into the kit on its third copy, not its first.** A runtime library
that becomes a framework is an outage multiplier: every service inherits its
bugs at the same moment, and none of them can opt out.

Business models, ORM models, migrations, route trees and service-specific
settings never belong here.

## Why it is vendored right now

Publishing a package requires a registry, a release process and a versioning
policy, and there is not yet a single service to justify any of them. Vendoring
buys roughly two services before the copies start to hurt.

The honest reading: the second copy is already one too many. When a `/readyz`
fix has to be pasted between two repositories, extraction is overdue.

## Extracting it

1. `git subtree split --prefix=kit -b kit-extract`
2. Push that branch to `scadable/service-kit`, tag `v0.1.0`
3. In each service, change `scadable-service-kit = { workspace = true }` to a
   version range, and delete `kit/` from the workspace members
4. Nothing else changes. No imports move.
