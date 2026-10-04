"""Shared policy for tests that run JavaScript under node. node is a test-only dependency
(the user accepted it). Locally a missing node skips these tests; on CI (the CI env var,
set by GitHub Actions) a missing node fails them, so a runner without node can never pass
silently."""

import os
import shutil

import pytest


def require_node() -> None:
    if shutil.which("node") is None:
        msg = "node is not installed (needed for the dashboard JS tests)"
        if os.environ.get("CI"):
            pytest.fail(msg)
        pytest.skip(msg)
