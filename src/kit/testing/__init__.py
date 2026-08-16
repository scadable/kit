"""Contract tests a service inherits, and the fixtures they need.

A service imports :func:`contract_tests` into its own suite and gets the shared
behaviour verified in ITS repository: the readiness shape and status codes, the
error envelope, the request id header. Verified in every service rather than in
whichever one remembered to write the test.

They prove a service wired the kit correctly, which is the thing that actually
goes wrong: installing the kit and forgetting to call it looks identical to
having no kit at all until something fails in production.
"""

from kit.testing._contract import assert_contract, contract_tests

__all__ = ["assert_contract", "contract_tests"]
