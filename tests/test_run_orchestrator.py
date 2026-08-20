"""bench/run.sh's scheduling, resume and selection logic, via --dry-run.

--dry-run starts no container, writes no result and does not touch the run log,
so every one of these is free. The behaviour they lock in -- round-robin
ordering, skipping combinations that already have a result, --force overriding
that, and the argument validation -- was previously verified only by running
the real thing for an hour.
"""
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "bench" / "run.sh"
JOB = re.compile(r"^\[(\d+)/(\d+)\] (\S+) (\S+) (\S+) c(\d+) r(\d+)\s+(.*)$")


def dry(*args):
    done = subprocess.run([str(RUN), "--dry-run", *args],
                          capture_output=True, text=True, cwd=str(ROOT))
    return done


def jobs(stdout):
    out = []
    for line in stdout.splitlines():
        m = JOB.match(line)
        if m:
            out.append({"system": m.group(3), "config": m.group(4),
                        "profile": m.group(5), "concurrency": int(m.group(6)),
                        "round": int(m.group(7)), "state": m.group(8).strip()})
    return out


@pytest.fixture(scope="module")
def full():
    done = dry()
    assert done.returncode == 0, done.stderr
    return jobs(done.stdout)


@pytest.fixture(scope="module")
def quick():
    done = dry("--quick")
    assert done.returncode == 0, done.stderr
    return jobs(done.stdout)


def test_full_matrix_is_the_size_the_plan_says(full):
    # 4 systems x 2 configs x (6 profiles + 2 sweep points) x 3 rounds
    assert len(full) == 192


def test_quick_is_one_round_three_profiles_no_sweep(quick):
    assert len(quick) == 24
    assert {j["round"] for j in quick} == {1}
    assert {j["profile"] for j in quick} == {"small", "medium", "bigdata-get"}
    assert {j["concurrency"] for j in quick} == {32}


def test_systems_cycle_within_a_round_rather_than_running_back_to_back(full):
    """Round-robin is the whole reason this driver exists.

    Thermal throttling accumulates against whoever runs last, so each system's
    cells must appear once per round, not all of one system then all of the
    next. Concretely: within round 1 the sequence of systems must visit all
    four before the round ends, and each round must repeat that visit.
    """
    for rnd in (1, 2, 3):
        order = [j["system"] for j in full if j["round"] == rnd]
        # collapse runs of the same system to the order they were first seen
        collapsed = [s for i, s in enumerate(order) if i == 0 or order[i - 1] != s]
        assert collapsed == ["minio", "silo", "rustfs", "seaweedfs"], rnd


def test_every_system_config_profile_combination_appears_once_per_round(full):
    seen = {}
    for j in full:
        key = (j["round"], j["system"], j["config"], j["profile"], j["concurrency"])
        seen[key] = seen.get(key, 0) + 1
    assert set(seen.values()) == {1}


def test_sweep_runs_only_on_its_own_profile(full):
    sweep = [j for j in full if j["concurrency"] != 32]
    assert sweep, "the sweep should contribute jobs to the full matrix"
    assert {j["profile"] for j in sweep} == {"medium"}
    assert {j["concurrency"] for j in sweep} == {8, 64}


def test_a_completed_combination_is_skipped_and_its_status_read_back(quick):
    done = [j for j in quick if j["state"].startswith("already done")]
    if not done:
        pytest.skip("no results recorded for this machine's hardware profile yet")
    for j in done:
        assert re.search(r"status=(ok|ok_with_warnings|suspect|failed)", j["state"]), j


def test_force_re_runs_what_resume_would_skip(quick):
    if not [j for j in quick if j["state"].startswith("already done")]:
        pytest.skip("no results recorded for this machine's hardware profile yet")
    forced = jobs(dry("--quick", "--force").stdout)
    assert forced, "forced dry run produced no jobs"
    assert all(j["state"] == "would run" for j in forced)


def test_profiles_selection_reaches_profiles_quick_leaves_out():
    """--quick shortens the default set; --profiles overrides it.

    Without this split there is no cheap way to smoke-test `list`,
    `bigdata-put` and `multipart` -- the three the quick set skips and the three
    most likely to fail -- short of three full rounds of them.
    """
    done = dry("--quick", "--systems", "minio",
               "--profiles", "bigdata-put,multipart,list")
    assert done.returncode == 0, done.stderr
    selected = jobs(done.stdout)
    assert len(selected) == 6
    assert {j["profile"] for j in selected} == {"bigdata-put", "multipart", "list"}
    assert {j["system"] for j in selected} == {"minio"}


def test_selecting_a_profile_that_is_not_the_sweep_profile_drops_the_sweep():
    selected = jobs(dry("--profiles", "small").stdout)
    assert selected
    assert {j["concurrency"] for j in selected} == {32}


def test_unknown_arguments_are_rejected_rather_than_ignored():
    for args in (["--quik"], ["--systems", "nope"], ["--profiles", "nope"]):
        done = dry(*args)
        assert done.returncode != 0, args


def test_a_dry_run_does_not_touch_the_run_log():
    logs = list((ROOT / "results").glob("*/run.log"))
    before = {p: p.stat().st_mtime_ns for p in logs}
    dry("--quick")
    for path, mtime in before.items():
        assert path.stat().st_mtime_ns == mtime, path
