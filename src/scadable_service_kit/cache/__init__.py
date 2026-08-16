"""Valkey, used as a cache and never as a system of record.

Optional everywhere: an absent cache is "unconfigured", not fatal. Keys are
namespaced by the service name so two services sharing one Valkey cannot
collide.
"""
