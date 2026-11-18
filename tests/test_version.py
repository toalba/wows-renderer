"""The package version must match pyproject.

`wows-replay-parser.__version__` sat at "0.1.0" through five releases
because nothing checked it, and installs self-reported the wrong version
the whole time. One assertion is cheaper than finding that out again.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

import renderer


def test_exported_version_matches_pyproject() -> None:
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    expected = tomllib.loads(pyproject.read_text())["project"]["version"]
    assert renderer.__version__ == expected
