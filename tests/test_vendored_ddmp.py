"""The vendored copy of ddmp must match what scripts/sync_ddmp.sh recorded."""

from __future__ import annotations

from pathlib import Path

import ddmp

from .conftest import SERVER_PLUGIN


def test_vendored_version_matches_record():
    record = Path(SERVER_PLUGIN) / "VENDORED_DDMP_VERSION"
    assert record.exists(), "run scripts/sync_ddmp.sh"
    fields = dict(
        line.split("=", 1) for line in record.read_text().splitlines() if "=" in line
    )
    assert fields["version"] == ddmp.__version__
    assert Path(ddmp.__file__).resolve().is_relative_to(Path(SERVER_PLUGIN).resolve())


def test_vendored_copy_has_no_pycache_committed():
    import subprocess

    tracked = subprocess.run(
        ["git", "ls-files", "Dometic CFX.indigoPlugin"],
        capture_output=True,
        text=True,
        cwd=Path(SERVER_PLUGIN).parents[2],
        check=True,
    ).stdout
    assert "__pycache__" not in tracked and ".pyc" not in tracked
