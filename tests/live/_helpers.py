"""Shared helpers for the live tests.

Lives outside conftest.py so both conftest and the test modules can import it:
`tests/live` is not a package, so a relative `from .conftest import ...` fails.
"""

from __future__ import annotations

import shutil

#: clang only -- this project deliberately does not build with g++.
CXX_CANDIDATES = ("clang++-14", "clang++-15", "clang++-16", "clang++")


def find_cxx() -> str | None:
    """First available clang++ on PATH, or None."""
    for name in CXX_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    return None
