import pytest

import node_required


def test_missing_node_fails_on_ci(monkeypatch):
    monkeypatch.setattr(node_required.shutil, "which", lambda name: None)
    monkeypatch.setenv("CI", "true")
    with pytest.raises(pytest.fail.Exception, match="node is not installed"):
        node_required.require_node()


def test_missing_node_skips_locally(monkeypatch):
    monkeypatch.setattr(node_required.shutil, "which", lambda name: None)
    monkeypatch.delenv("CI", raising=False)
    with pytest.raises(pytest.skip.Exception):
        node_required.require_node()


def test_present_node_is_fine_everywhere(monkeypatch):
    monkeypatch.setattr(node_required.shutil, "which", lambda name: "/usr/bin/node")
    monkeypatch.setenv("CI", "true")
    node_required.require_node()
