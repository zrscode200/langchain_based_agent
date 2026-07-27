"""Shared test fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Keep agent construction off the developer's real ``$HOME``.

    The assembly creates agent/skills directories and reads subagent
    definitions under `~/.deepagents`, so an unisolated run both litters the
    home directory and makes branch coverage machine-dependent (which skill
    sources and subagents exist locally changes what gets composed).
    """
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    return home
