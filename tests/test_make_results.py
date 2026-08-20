"""RESULTS.md generation, on the committed results.

These are the four ways this generator can publish something wrong without
raising: eat a file that is not a result, drop a cell that has no result,
render a status it does not have a glyph for, or disagree with
bench/workloads.yaml about how large the matrix is meant to be.
"""
import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bench"))

import make_results  # noqa: E402

PROFILE_ID = "apple-m1-8gb-darwin"
RESULTS_DIR = ROOT / "results" / PROFILE_ID


@pytest.fixture(scope="module")
def workloads():
    return yaml.safe_load((ROOT / "bench" / "workloads.yaml").read_text())


@pytest.fixture(scope="module")
def conformance():
    return json.loads((RESULTS_DIR / "conformance.json").read_text())


def test_warp_analysis_files_are_not_read_as_results(tmp_path):
    """warp writes <run>.warp-analysis.json next to the result it came from.

    It is warp's raw output, not a result: no `run` key, no `status`. Globbing
    *.json without excluding it crashes the generator at best and invents a
    row at worst.
    """
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "sys__c1__small__c32__r1.json").write_text(json.dumps({
        "status": "ok",
        "run": {"system": "sys", "config": "c1", "profile": "small", "round": 1},
    }))
    (raw / "sys__c1__small__c32__r1.warp-analysis.json").write_text('{"warp": "raw"}')

    runs = make_results.load_runs(str(raw))

    assert list(runs) == [("sys", "c1", "small", 1)]


def test_every_recorded_status_has_a_glyph(conformance):
    """A status with no glyph renders as `?` in the matrix and says nothing."""
    recorded = {cell["status"]
                for cells in conformance["matrix"].values()
                for cell in cells.values()}
    assert recorded <= set(make_results.STATUS_GLYPH), (
        "unmapped conformance status: %s" % (recorded - set(make_results.STATUS_GLYPH))
    )


def test_not_exercisable_cells_carry_a_reason(conformance):
    """`not_exercisable` without a reason is indistinguishable from a gap."""
    for system, cells in conformance["matrix"].items():
        for name, cell in cells.items():
            if cell["status"] == "not_exercisable":
                assert cell.get("reason"), f"{system}/{name} has no reason"


def test_missing_cells_are_rendered_not_dropped(workloads):
    """An absent row reads as "not applicable". These absences are findings."""
    profile = next(p for p in workloads["profiles"] if p["id"] == "bigdata-put")
    systems = ["minio", "silo", "rustfs", "seaweedfs"]
    runs = make_results.load_runs(str(RESULTS_DIR / "raw"))

    rows = make_results.profile_rows(workloads, profile, runs, systems, 1)

    assert len(rows) == len(systems) * len(make_results.CONFIG_LABEL)
    assert any("*no result*" in row for row in rows)


def test_planned_run_count_matches_workloads(workloads):
    """The coverage headline is only honest if it counts the real matrix."""
    expected = (len(make_results.SYSTEM_ORDER)
                * len(make_results.CONFIG_LABEL)
                * (len(workloads["profiles"]) + len(workloads["sweep"]["concurrency"]))
                * workloads["rounds"])
    assert make_results.planned_run_count(workloads) == expected


def test_render_is_deterministic():
    """Two renders of the same inputs must be byte-identical.

    Otherwise every regeneration produces a diff and nobody reads them.
    """
    once = make_results.render(str(ROOT), PROFILE_ID)
    twice = make_results.render(str(ROOT), PROFILE_ID)
    assert once == twice
    assert once.startswith("# Results")


def test_committed_results_md_is_up_to_date():
    """RESULTS.md is generated; a hand-edit or a stale copy is a defect."""
    committed = (ROOT / "RESULTS.md").read_text()
    assert committed == make_results.render(str(ROOT), PROFILE_ID), (
        "RESULTS.md is stale -- run bench/make_results.py"
    )
