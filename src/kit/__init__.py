"""Cross-cutting behaviour shared by every SCADABLE service.

CONTRACT.md states this behaviour in language-independent terms; this package is
the Python implementation of it. A service rewritten in another language for
throughput has to reproduce the contract, and kit.testing is how it proves it.

kit never owns your application. create_app() lives in the service and calls in
here, so opting out of any piece is simply not calling it. There is no base
class, no plugin registry and no import-time side effects.

Two kinds of thing live here, with different rules:

  Contract, not overridable. The envelope shape, the readiness states, the
  header and log field names. Changing one of these in a single service
  desynchronises it from every dashboard and client in the fleet.

  Policy, override freely. Which exception maps to which code, rate limits,
  sampling, log level, which dependencies are declared. These are parameters.

The version below is logged at service boot, which is how a stale copy is found
across the fleet without opening every repository.
"""

__version__ = "0.1.1"
