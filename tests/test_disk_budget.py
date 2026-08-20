"""The pre-run disk requirement, and that it matches what runs actually consume.

bench/disk_budget.py is the single source of the number. bench/run-workload.sh
refuses to start below it and bench/run.sh waits for it; when those two used
different formulas a transient dip became a permanently lost cell.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bench"))

import disk_budget  # noqa: E402

GIB = 1024 ** 3
WORKLOADS = ROOT / "bench" / "workloads.yaml"


@pytest.fixture(scope="module")
def workloads():
    with open(WORKLOADS) as handle:
        return yaml.safe_load(handle)


def test_requirement_is_the_write_estimate_plus_the_floor(workloads):
    # medium is measured at 11 GiB; a 10 GiB floor means 21 GiB must be free.
    assert disk_budget.required_gib(workloads, "medium", "c1", 10) == 21


def test_config_does_not_multiply_the_requirement(workloads):
    """c2 costs 1.09-1.14x c1 in measurement, not 2x.

    The old formula doubled for c2 and demanded 36 GiB for bigdata-put on a
    machine with 31 -- a whole column of the matrix that could never run.
    """
    for profile in (p["id"] for p in workloads["profiles"]):
        assert (disk_budget.required_gib(workloads, profile, "c1", 10)
                == disk_budget.required_gib(workloads, profile, "c2", 10))


def test_floor_is_additive_not_a_minimum(workloads):
    small = disk_budget.required_gib(workloads, "small", "c1", 10)
    assert small == disk_budget.required_gib(workloads, "small", "c1", 0) + 10


def test_unknown_profile_and_config_are_rejected(workloads):
    with pytest.raises(KeyError):
        disk_budget.required_gib(workloads, "no-such-profile", "c1", 10)
    with pytest.raises(ValueError):
        disk_budget.required_gib(workloads, "medium", "c3", 10)


def test_cli_matches_the_library(workloads):
    out = subprocess.run(
        [sys.executable, str(ROOT / "bench" / "disk_budget.py"),
         str(WORKLOADS), "medium", "c2", "10"],
        capture_output=True, text=True, check=True).stdout.strip()
    assert int(out) == disk_budget.required_gib(workloads, "medium", "c2", 10)


def test_cli_rejects_a_bad_floor():
    done = subprocess.run(
        [sys.executable, str(ROOT / "bench" / "disk_budget.py"),
         str(WORKLOADS), "medium", "c2", "ten"],
        capture_output=True, text=True)
    assert done.returncode != 0


def test_estimates_cover_every_run_this_machine_has_recorded(workloads):
    """The estimate must be >= the worst consumption actually observed.

    Every result file records host free bytes before and after. This is the
    check that keeps write_gib_estimate a measurement instead of a guess: if a
    future run consumes more than its profile claims, this fails and the
    estimate gets raised rather than the guard quietly under-provisioning.

    Note the deltas are contaminated downward -- Docker Desktop reclaims an
    earlier run's space mid-measurement, and two `small` runs even show negative
    consumption -- so the worst observed figure is a lower bound on the truth.
    """
    results = sorted((ROOT / "results").glob("*/raw/*.json"))
    if not results:
        pytest.skip("no recorded runs on this machine yet")

    worst = {}
    for path in results:
        run = json.loads(path.read_text())["run"]
        before = run.get("host_disk_free_bytes_before")
        after = run.get("host_disk_free_bytes_after")
        if before is None or after is None:
            continue
        used = (before - after) / GIB
        key = run["profile"]
        if used > worst.get(key, (float("-inf"), None))[0]:
            worst[key] = (used, path.name)

    offenders = []
    for profile, (used, name) in sorted(worst.items()):
        try:
            claimed = disk_budget.required_gib(workloads, profile, "c1", 0)
        except KeyError:
            continue  # a profile that has since been removed from the matrix
        if used > claimed:
            offenders.append("%s: claims %d GiB, %s consumed %.2f GiB"
                             % (profile, claimed, name, used))
    assert not offenders, "write_gib_estimate is below observed consumption:\n" + \
        "\n".join(offenders)
