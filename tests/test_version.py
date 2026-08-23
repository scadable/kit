"""The exported version and the packaging metadata must agree.

They disagreed once, in the same commit that bumped pyproject and forgot this
one, and NOTHING CAUGHT IT. That matters more here than the mismatch itself:
kit.__version__ is logged at service boot precisely so a stale copy can be found
across the fleet without opening every repository, so a wrong value does not
degrade the diagnostic, it inverts it. A service running 0.6.1 reports 0.6.0 and
the one mechanism for finding stale copies confidently names the wrong version.

A version string is a description, and an unchecked description is the thing
people act on.
"""

from __future__ import annotations

from importlib.metadata import version

import kit


def test_the_exported_version_matches_the_installed_metadata() -> None:
    assert kit.__version__ == version("scadable-kit")
