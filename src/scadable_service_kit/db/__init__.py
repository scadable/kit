"""Async engine construction and the tenant-scoped transaction helper.

Pool settings are applied to the engine explicitly rather than being left to the
URL, because a driver default silently replacing a configured pool size is a bug
this fleet has already shipped once.

The tenant helper opens the transaction, sets the tenant local to it, and only
then lets repositories run. See infra/db/tenancy.py in the service for why the
ordering is load-bearing.
"""
