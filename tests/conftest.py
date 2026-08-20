"""Session fixtures shared by the measurement tests.

The durability results are collected in memory while the tests run and written
once, at the end of the session, into results/<profile_id>/durability.json --
the file Task 7's report reads.
"""
import json
import os
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


# Set to "1" to let a test session record its measurements into results/.
# Without it the suite is read-only against every published artifact.
WRITE_RESULTS_ENV = "BENCH_WRITE_RESULTS"


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
    path = out_dir / "durability.json"

    # COMPARE BY DEFAULT, WRITE ONLY WHEN ASKED.
    #
    # results/<profile>/durability.json is a published finding, committed and
    # cited in the report. Re-running the module is how you re-measure it, but a
    # plain `pytest` -- run to check that nothing is broken -- must not silently
    # replace a published artifact with fresh, timing-dependent values. Every
    # number under it shifts run to run (seconds_since_fault, byte counts, which
    # sampling instants land in which sub-second window), so the diff always
    # looks like a change and never like a result.
    #
    # This is not the same class as the earlier hwprofile case, where the write
    # was an unintended side effect. Here the write IS the module's product,
    # which is exactly why it needs a deliberate switch rather than a default.
    if os.environ.get(WRITE_RESULTS_ENV) != "1":
        print("\n[durability] %s left untouched; re-run with %s=1 to record this "
              "session's measurements." % (path, WRITE_RESULTS_ENV))
        previous = None
        if path.exists():
            try:
                previous = json.loads(path.read_text())
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                previous = None
        if isinstance(previous, dict):
            for system in sorted(collected):
                was = previous.get(system)
                if not isinstance(was, dict):
                    print("[durability]   %s: no committed entry to compare against"
                          % system)
                    continue
                # Only the load-bearing conclusions are compared; the timings
                # underneath them are expected to differ on every run.
                for field in ("fault_tolerated", "mechanism", "device_killed"):
                    if was.get(field) != collected[system].get(field):
                        print("[durability]   %s: %s committed=%r observed=%r"
                              % (system, field, was.get(field),
                                 collected[system].get(field)))
        return

    out_dir.mkdir(parents=True, exist_ok=True)
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
