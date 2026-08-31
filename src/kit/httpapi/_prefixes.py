"""Which paths count as being under a trusted prefix.

ONE MODULE BECAUSE TWO MIDDLEWARES MUST AGREE. The CORS wrapper and the rate
limiter both ask this question, and a prefix exempt from one and not the other is
a service whose behaviour nobody can state in a sentence. Duplicating a
`startswith` in both is how they drift.
"""

from __future__ import annotations

from starlette.types import Scope


def normalize_trusted_prefixes(prefixes: tuple[str, ...]) -> tuple[str, ...]:
    """The prefixes, checked at construction, or a refusal.

    RAISES RATHER THAN DROPPING, matching `CORS.__post_init__` one module over
    and for the same reason: this runs once, before the process serves anything,
    so a service configured into nonsense fails to start instead of serving
    whichever reading happened to win.

    THE EMPTY STRING IS THE ONE THAT MATTERS. Every path starts with it, so a
    single empty entry exempts the entire application from both CORS and rate
    limiting, silently, with a green deployment. That is not a hypothetical
    typo: splitting an unset comma-separated environment variable produces
    exactly `("",)`, which is the ordinary shape of a setting somebody forgot to
    fill in. Dropping it quietly would leave the deployment believing it had
    exempted something.

    A prefix must also be absolute. A relative one can never match a path and is
    therefore a statement that does nothing, which is worth failing on for the
    same reason: somebody wrote it expecting an effect.
    """
    for prefix in prefixes:
        if not prefix or not prefix.startswith("/"):
            raise ValueError(
                f"trusted_prefixes must be absolute paths, got {prefix!r}. "
                "An empty prefix matches every path and would exempt the whole "
                "application from CORS and rate limiting; a relative one matches "
                "nothing and would exempt something the author expected it to."
            )
    return prefixes


def under(scope: Scope, prefixes: tuple[str, ...]) -> bool:
    """Whether this request is under one of the prefixes.

    ON A SEGMENT BOUNDARY, never a bare `startswith`. Trusting `/api/admin` must
    not exempt `/api/administrator` or `/api/admin-fake`: those are different
    routes that merely share an opening, and exempting them is the same mistake
    `EXEMPT_PATHS` avoids by staying an exact match for the probes. A bare
    `startswith` reads correct and hands an unrelated route the exemption.

    THE APP-RELATIVE PATH, not the raw one. `scope["path"]` carries the mount
    prefix when conventions are installed on an application mounted under one, so
    a service mounted at `/service` would compare `/service/admin/...` against
    `/admin` and never match. That failure is silent and in the safe direction,
    which is exactly why it would survive unnoticed: the exemption simply never
    applies and nothing reports it.

    Stripping `root_path` by hand rather than borrowing starlette's helper. The
    helper is not exported from `starlette.routing` and lives in a private module,
    so importing it would tie this to an internal that can move between releases.
    The behaviour is the ASGI specification itself, `root_path` is the mount point
    and `path` includes it, so spelling it out is both stabler and readable.
    """
    if not prefixes:
        return False
    path: str = scope.get("path", "")
    root: str = scope.get("root_path", "")
    if root and path.startswith(root):
        # `or "/"` because stripping the mount from a request AT the mount leaves
        # an empty string, and every prefix here is absolute.
        path = path[len(root) :] or "/"
    return any(path == prefix or path.startswith(f"{prefix}/") for prefix in prefixes)
