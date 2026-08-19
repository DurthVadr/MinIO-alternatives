"""Session fixtures shared by the measurement tests.

The durability results are collected in memory while the tests run and written
once, at the end of the session, into results/<profile_id>/durability.json --
the file Task 7's report reads.
"""
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def profile_id():
    """The hardware profile these results belong to.

    hwprofile.sh is the single source of that id (and, as a side effect,
    stamps results/<profile_id>/hardware-profile.json), so no measurement
    file can end up in a directory whose provenance is unrecorded.
    """
    out = subprocess.run(
        [str(ROOT / "bench" / "hwprofile.sh")], capture_output=True, text=True, check=True
    ).stdout
    return json.loads(out)["profile_id"]


@pytest.fixture(scope="session")
def durability_results(profile_id):
    collected = {}
    yield collected

    if not collected:
        # Nothing was observed this session (the whole module was deselected,
        # or every stack failed to come up). Writing an empty object here
        # would silently destroy a previous run's real measurements.
        return

    out_dir = ROOT / "results" / profile_id
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "durability.json"

    # Merge rather than overwrite: `pytest -k minio` is a normal way to re-run
    # one system after changing its config, and it must not drop the other
    # three systems' entries. Systems measured in this session win.
    merged = {}
    if path.exists():
        try:
            previous = json.loads(path.read_text())
            if isinstance(previous, dict):
                merged.update(previous)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            # A corrupt file protects nothing; this session's observations
            # replace it wholesale rather than aborting the run at teardown.
            merged = {}
    merged.update(collected)
    path.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n")
