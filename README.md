# kit

What must be identical in every SCADABLE service, and nothing else.

One error envelope. One request id. One meaning for "ready". One shape for a log
line. If two services disagree about any of those, the fleet stops being legible
from the outside, and preventing that is the only reason this package exists.

`CONTRACT.md` states that behaviour on one page. This package implements it, and
`kit.testing` proves a service wired it correctly.

```
pip install scadable-kit        # from git, see below
from kit.health import Registry
```

## What is in it

| Package | What it gives you |
| --- | --- |
| `kit.httpapi` | The error envelope, the request-id middleware, the request log line, CORS, rate limiting, the shared `/readyz` handler |
| `kit.health` | The readiness registry: `require()` before wiring, three states, checks run concurrently |
| `kit.observability` | JSON logs with `trace_id` and `span_id` on every line, OTLP traces and metrics, propagation in both directions |
| `kit.clients` | Outbound calls: deadlines, retries with jitter, a circuit breaker, trace and request-id propagation, pluggable auth |
| `kit.config` | Env loading, the service prefix, the `PORT` and `OTEL_*` exceptions, fail-fast process settings and fail-soft backing systems |
| `kit.testing` | The contract tests a service inherits, so the shared behaviour is verified in every repository |

About a thousand lines. The most important piece is also the smallest: roughly
sixty lines of error envelope is what makes every service fail the same way.

## Overriding it

**kit never owns your application.** `create_app()` lives in your service and
calls into kit. There is no base class to inherit, no plugin registry, no import
side effects. So the escape hatch for anything you disagree with is simply not
calling that function and writing your own.

That matters because a shared library nobody can opt out of stops being a
library and becomes a framework, and a framework is an outage multiplier: every
service inherits its bugs at the same moment.

There are two kinds of thing here and they have different rules.

**Contract. Not overridable.** The envelope shape, the readiness states, the
header names, the log field names. Overriding these does not customise your
service, it desynchronises it from every dashboard, alert and client in the
fleet. If one of them is genuinely wrong, change `CONTRACT.md` and the fleet
together.

**Policy. Override freely, that is what the parameters are for.** Which
exception maps to which code. Your rate limits. Sampling. Log level. Which
dependencies you declare. Whether you install CORS at all.

Everything is a function or an object you construct and pass. Nothing is a
module-level singleton, so nothing has to be monkeypatched to be replaced:

```python
# take the defaults
install_conventions(app, settings)

# or assemble it yourself, in your own order, minus the bits you do not want
app.add_middleware(RequestIDMiddleware)
app.add_middleware(RequestLogMiddleware, logger=my_logger)
install_error_handlers(app, mapper=my_exception_mapper)
# no rate limiter here: this service sits behind an authenticated gateway
```

When you do take an override, say why in the service's `CLAUDE.md`. An
undocumented deviation reads as a mistake to the next person and gets
"corrected" back.

## What is deliberately not in it

Database helpers, cache clients, object storage. They were here in the first
draft and were removed: no service has used them yet, and a shared abstraction
designed before its second real consumer is a guess everyone then works around.
They live in the service template's `infra/` until two services agree on what
they should look like.

**Code moves in on its third copy, not its first.**

`kit.clients` is the exception, and the reason is worth stating rather than
waving through: it is not an abstraction over a domain, it is the retry,
timeout and breaker policy, and those are wrong in the same way in every service
that writes them independently. Seven retry policies is seven different
behaviours the first time a dependency gets slow, discovered during the incident.
The typed gateway on top of it still lives in each service.

Business models, ORM models, migrations, route trees and service settings never
belong here at all.

## Installing it

Pinned by tag, not by branch, so a service upgrades deliberately:

```toml
[project]
dependencies = ["scadable-kit"]

[tool.uv.sources]
scadable-kit = { git = "https://github.com/scadable/kit", tag = "v0.4.0" }
```

The repository is public, so this needs no credentials: not in CI, not in a
Docker build, not on a new machine. Public source, proprietary license. Nothing
here is open source and publication grants no rights; see LICENSE.

`kit.__version__` is logged at service boot, which is how you answer "who is
running a stale kit" across the fleet without opening six repositories.

## Changing it

A change here reaches every service, so:

1. If it touches anything in `CONTRACT.md`, update the contract in the same pull
   request. The contract is the source of truth, the code follows it.
2. The contract tests must still pass.
3. Tag a release. Services pick it up as an ordinary dependency bump.
4. Never fix a service by patching its vendored copy. There are no vendored
   copies, and that is the point.
