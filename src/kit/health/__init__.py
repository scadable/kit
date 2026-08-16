"""The readiness registry.

Dependencies are declared with require() BEFORE anything is wired, so a
dependency that is never wired reports "unconfigured" by construction instead of
vanishing from the report. Success-only registration is forbidden: an empty
checks object must never be able to look healthy.

Three states, never two: ready, error, unconfigured. Only "error" always blocks;
"unconfigured" blocks only when the dependency was required.
"""
