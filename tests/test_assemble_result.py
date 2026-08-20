"""Tests for bench/assemble_result.py.

The assembler is where a correct measurement turns into a published number, so
these cover the two things review found wrong there: how latency windows are
merged, and how a degraded run is labelled.
"""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "bench" / "assemble_result.py"

spec = importlib.util.spec_from_file_location("assemble_result", MODULE_PATH)
ar = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ar)


# ---------------------------------------------------------------------------
# percentile
# ---------------------------------------------------------------------------
def test_percentile_endpoints_and_interpolation():
    data = [10.0, 20.0, 30.0, 40.0]
    assert ar.percentile(data, 0) == 10.0
    assert ar.percentile(data, 100) == 40.0
    # rank = 3 * 0.5 = 1.5 -> halfway between 20 and 30
    assert ar.percentile(data, 50) == 25.0


def test_percentile_handles_degenerate_inputs():
    assert ar.percentile([], 50) is None
    assert ar.percentile([7.0], 99) == 7.0


def test_summarise_gives_true_quantiles():
    # 1..100: p50 interpolates between the 50th and 51st values.
    summary = ar.summarise([float(i) for i in range(1, 101)])
    assert summary["samples"] == 100
    assert summary["min"] == 1.0
    assert summary["max"] == 100.0
    assert summary["p50"] == pytest.approx(50.5)
    assert summary["p90"] == pytest.approx(90.1)
    assert summary["p99"] == pytest.approx(99.01)
    assert summary["mean"] == pytest.approx(50.5)


# ---------------------------------------------------------------------------
# Window merging -- Critical 1
# ---------------------------------------------------------------------------
def _window(start, end, requests, ttfb_median, ttfb_avg=None, ttfb_p99=None):
    return {
        "start_time": start,
        "end_time": end,
        "single_sized_requests": {
            "requests": requests,
            "dur_avg_millis": ttfb_median,
            "dur_median_millis": ttfb_median,
            "dur_90_millis": ttfb_median,
            "dur_99_millis": ttfb_median,
            "fastest_millis": 1.0,
            "slowest_millis": 999.0,
            "std_dev_millis": 1.0,
            "first_byte": {
                "average_millis": ttfb_avg if ttfb_avg is not None else ttfb_median,
                "p25_millis": ttfb_median,
                "median_millis": ttfb_median,
                "p75_millis": ttfb_median,
                "p90_millis": ttfb_median,
                "p99_millis": ttfb_p99 if ttfb_p99 is not None else ttfb_median,
                "fastest_millis": 0.5,
                "slowest_millis": 1500.0,
                "std_dev_millis": 2.0,
            },
        },
    }


def test_window_merge_is_request_weighted_not_a_plain_mean():
    """A short window holding almost no requests must not get equal weight.

    Reproduces the shape of the defect found in review: warp's own console
    averages its aggregation windows unweighted, so a 1.5s ramp-down window
    holding 24 of 6127 requests got 14.3% of the weight.
    """
    windows = ar.collect_windows({
        "requests_by_client": {"c1": [
            _window("2026-08-20T00:00:00Z", "2026-08-20T00:00:10Z", 6000, 100.0),
            _window("2026-08-20T00:00:10Z", "2026-08-20T00:00:12Z", 24, 2000.0),
        ]}
    })
    summary = ar.weighted_window_summary(windows)
    expected = (6000 * 100.0 + 24 * 2000.0) / 6024
    assert summary["ttfb"]["mean_millis"] == pytest.approx(expected)
    # The unweighted answer would have been 1050.0 -- an order of magnitude out.
    assert summary["ttfb"]["mean_millis"] < 110.0
    assert summary["requests"] == 6024
    assert summary["windows"] == 2


def test_window_merge_percentiles_are_named_as_window_means():
    """A weighted mean of p99s is not a p99 and must not be published as one."""
    windows = ar.collect_windows({
        "requests_by_client": {"c1": [
            _window("2026-08-20T00:00:00Z", "2026-08-20T00:00:10Z", 100, 10.0),
        ]}
    })
    ttfb = ar.weighted_window_summary(windows)["ttfb"]
    assert "p99_millis_window_mean" in ttfb
    assert "p50_millis_window_mean" in ttfb
    # No bare quantile names anywhere in the fallback summary.
    for key in ttfb:
        assert key not in ("p50", "p99", "p50_millis", "p99_millis", "median_millis")


def test_window_merge_keeps_per_window_values_for_distributions():
    windows = ar.collect_windows({
        "requests_by_client": {"c1": [
            _window("2026-08-20T00:00:00Z", "2026-08-20T00:00:10Z", 500, 10.0),
            _window("2026-08-20T00:00:10Z", "2026-08-20T00:00:20Z", 400, 20.0),
        ]}
    })
    summary = ar.weighted_window_summary(windows)
    assert [w["requests"] for w in summary["per_window"]] == [500, 400]
    assert [w["ttfb_p50_millis"] for w in summary["per_window"]] == [10.0, 20.0]


def test_windows_outside_the_measured_span_are_dropped():
    """warp's latency windows overrun total.end_time; those are not measurement."""
    block = {"requests_by_client": {"c1": [
        _window("2026-08-20T00:00:00Z", "2026-08-20T00:00:10Z", 500, 10.0),
        _window("2026-08-20T00:00:10Z", "2026-08-20T00:00:20Z", 400, 20.0),
        _window("2026-08-20T00:00:20Z", "2026-08-20T00:00:30Z", 5, 3000.0),
    ]}}
    span_start = ar.parse_rfc3339("2026-08-20T00:00:00Z")
    span_end = ar.parse_rfc3339("2026-08-20T00:00:20Z")
    kept = ar.collect_windows(block, span_start, span_end)
    assert len(kept) == 2
    assert all(w["requests"] in (500, 400) for w in kept)
    # Without the span it would keep all three.
    assert len(ar.collect_windows(block)) == 3


def test_weighted_window_summary_is_none_without_requests():
    assert ar.weighted_window_summary([]) is None
    assert ar.weighted_window_summary([{"requests": 0}]) is None


# ---------------------------------------------------------------------------
# Per-request records (--full)
# ---------------------------------------------------------------------------
FULL_HEADER = ("idx\tthread\top\tclient_id\tn_objects\tbytes\tendpoint\tfile\terror\t"
               "start\tfirst_byte\tlast_byte\tend\tduration_ns\tcat")


def _record(op, start, first_byte, duration_ns, error=""):
    return "\t".join(["0", "1", op, "cid", "1", "1024", "http://h", "f", error,
                      start, first_byte, "", "", str(duration_ns), "0"])


def test_read_full_records_computes_ttfb_from_timestamps():
    text = "\n".join([
        FULL_HEADER,
        _record("GET", "2026-08-20T00:00:01.000000000Z", "2026-08-20T00:00:01.050000000Z", 80_000_000),
        _record("GET", "2026-08-20T00:00:02.000000000Z", "2026-08-20T00:00:02.010000000Z", 20_000_000),
    ])
    records = ar.read_full_records(text)
    assert records["GET"]["requests"] == 2
    assert records["GET"]["ttfb"] == pytest.approx([50.0, 10.0])
    assert records["GET"]["request"] == pytest.approx([80.0, 20.0])


def test_read_full_records_excludes_errors_from_latency_but_counts_them():
    text = "\n".join([
        FULL_HEADER,
        _record("PUT", "2026-08-20T00:00:01.000000000Z", "2026-08-20T00:00:01.010000000Z", 10_000_000),
        _record("PUT", "2026-08-20T00:00:02.000000000Z", "", 1_000_000, error="boom"),
    ])
    records = ar.read_full_records(text)
    assert records["PUT"]["requests"] == 2
    assert records["PUT"]["errors"] == 1
    assert len(records["PUT"]["request"]) == 1


def test_read_full_records_honours_the_window():
    text = "\n".join([
        FULL_HEADER,
        _record("GET", "2026-08-20T00:00:01.000000000Z", "2026-08-20T00:00:01.010000000Z", 10_000_000),
        _record("GET", "2026-08-20T00:00:99.000000000Z".replace("99", "50"),
                "2026-08-20T00:00:50.010000000Z", 10_000_000),
    ])
    start = ar.parse_rfc3339("2026-08-20T00:00:00Z")
    end = ar.parse_rfc3339("2026-08-20T00:00:10Z")
    assert ar.read_full_records(text, start, end)["GET"]["requests"] == 1


def test_read_full_records_rejects_non_full_benchdata():
    assert ar.read_full_records('{"v":2,"total":{}}') is None
    assert ar.read_full_records("") is None


# ---------------------------------------------------------------------------
# Status -- Critical 2
# ---------------------------------------------------------------------------
def test_warn_level_failure_does_not_leave_the_run_labelled_ok():
    checks = [
        {"name": "warp_exit_zero", "ok": True, "severity": "fatal", "detail": ""},
        {"name": "client_not_saturated", "ok": False, "severity": "warn", "detail": ""},
    ]
    assert ar.decide_status(checks, True, True) == "ok_with_warnings"


def test_all_checks_passing_is_ok():
    checks = [{"name": "a", "ok": True, "severity": "fatal", "detail": ""},
              {"name": "b", "ok": True, "severity": "warn", "detail": ""}]
    assert ar.decide_status(checks, True, True) == "ok"


def test_fatal_failure_is_suspect_and_outranks_a_warning():
    checks = [{"name": "a", "ok": False, "severity": "fatal", "detail": ""},
              {"name": "b", "ok": False, "severity": "warn", "detail": ""}]
    assert ar.decide_status(checks, True, True) == "suspect"


def test_warp_failure_is_failed_regardless_of_checks():
    assert ar.decide_status([], False, True) == "failed"
    assert ar.decide_status([], True, False) == "failed"


def test_saturated_client_produces_a_failing_check():
    checks, check = ar.make_checker()
    ar.evaluate_checks(
        check, warp_exit=0, analyze_exit=0, analysis={"total": {}}, metrics=None,
        telemetry={"server": {"samples": 3, "containers": ["a"]},
                   "client": {"samples": 3, "cpu_saturation_mean": 0.97}},
        duration_requested=60, expected_containers=["a"], client_cpu_limit_pct=200.0,
        disk_free_after=10 ** 12, disk_required_bytes=0,
    )
    by_name = {c["name"]: c for c in checks}
    assert by_name["client_not_saturated"]["ok"] is False
    assert ar.decide_status(checks, True, True) == "ok_with_warnings"


def test_missing_telemetry_container_is_fatal():
    checks, check = ar.make_checker()
    ar.evaluate_checks(
        check, warp_exit=0, analyze_exit=0, analysis={"total": {}}, metrics=None,
        telemetry={"server": {"samples": 3, "containers": ["bench-seaweedfs"]},
                   "client": {"samples": 3, "cpu_saturation_mean": 0.1}},
        duration_requested=60,
        expected_containers=["bench-seaweedfs", "bench-swfs-vol0"],
        client_cpu_limit_pct=200.0, disk_free_after=10 ** 12, disk_required_bytes=0,
    )
    by_name = {c["name"]: c for c in checks}
    assert by_name["telemetry_containers_complete"]["ok"] is False
    assert ar.decide_status(checks, True, True) == "suspect"


# ---------------------------------------------------------------------------
# Telemetry aggregation
# ---------------------------------------------------------------------------
def _row(cycle, epoch, role, container, cpu, mem, limit=2147483648):
    return {"cycle": str(cycle), "ts": "t", "epoch": str(epoch), "role": role,
            "container": container, "cpu_pct": str(cpu), "mem_bytes": str(mem),
            "mem_limit_bytes": str(limit), "net_rx_bytes": "0", "net_tx_bytes": "0",
            "blk_read_bytes": "0", "blk_write_bytes": "0"}


def test_instants_sum_across_a_systems_containers():
    rows = [_row(1, 100, "server", "bench-seaweedfs", 100.0, 10),
            _row(1, 100, "server", "bench-swfs-vol0", 200.0, 20)]
    telemetry = ar.aggregate_telemetry(rows)
    assert telemetry["server"]["samples"] == 1
    assert telemetry["server"]["cpu_pct_sum_mean"] == pytest.approx(300.0)
    assert telemetry["server"]["mem_bytes_sum_peak"] == 30


def test_a_cycle_straddling_a_second_boundary_is_not_split():
    """Same cycle, different wall-clock second: still one instant."""
    rows = [_row(1, 100, "server", "bench-seaweedfs", 100.0, 10),
            _row(1, 101, "server", "bench-swfs-vol0", 200.0, 20)]
    telemetry = ar.aggregate_telemetry(rows)
    assert telemetry["server"]["samples"] == 1
    assert telemetry["server"]["cpu_pct_sum_mean"] == pytest.approx(300.0)


def test_a_repeated_sample_of_one_container_is_not_double_counted():
    rows = [_row(1, 100, "server", "bench-minio", 550.0, 10),
            _row(1, 100, "server", "bench-minio", 560.0, 11)]
    telemetry = ar.aggregate_telemetry(rows)
    assert telemetry["server"]["cpu_pct_sum_max"] <= 600.0


def test_zero_memory_limit_from_an_exiting_container_is_ignored():
    rows = [_row(1, 100, "client", "bench-warp", 50.0, 10, limit=1073741824),
            _row(2, 101, "client", "bench-warp", 0.0, 0, limit=0)]
    telemetry = ar.aggregate_telemetry(rows)
    assert telemetry["client"]["per_container"]["bench-warp"]["mem_limit_bytes"] == 1073741824


def test_over_budget_cpu_samples_are_counted_not_hidden():
    rows = [_row(1, 100, "server", "bench-minio", 590.0, 10),
            _row(2, 101, "server", "bench-minio", 1200.0, 10)]
    server = ar.aggregate_telemetry(rows, server_cpu_budget_pct=600.0)["server"]
    assert server["cpu_pct_samples_over_budget"] == 1
    assert server["cpu_pct_max_over_budget_ratio"] == pytest.approx(2.0)


def test_telemetry_window_excludes_prepare_phase_samples():
    rows = [_row(1, 100, "server", "bench-minio", 10.0, 10),
            _row(2, 150, "server", "bench-minio", 550.0, 10),
            _row(3, 900, "server", "bench-minio", 5.0, 10)]
    telemetry = ar.aggregate_telemetry(rows, window_start=120, window_end=200)
    assert telemetry["rows_total"] == 3
    assert telemetry["rows_in_window"] == 1
    assert telemetry["server"]["cpu_pct_sum_mean"] == pytest.approx(550.0)


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------
def _run_assembler(tmp_path, analysis, telemetry_rows, benchdata_text=None, warp_exit=0):
    analysis_path = tmp_path / "a.json"
    analysis_path.write_text(json.dumps(analysis) if analysis is not None else "{")
    telemetry_path = tmp_path / "t.csv"
    header = ("cycle,ts,epoch,role,container,cpu_pct,mem_bytes,mem_limit_bytes,"
              "net_rx_bytes,net_tx_bytes,blk_read_bytes,blk_write_bytes")
    telemetry_path.write_text("\n".join([header] + telemetry_rows) + "\n")
    if benchdata_text is None:
        benchdata_arg = "-"
    else:
        import compression.zstd as zstd
        benchdata_path = tmp_path / "b.benchdata.csv.zst"
        benchdata_path.write_bytes(zstd.compress(benchdata_text.encode()))
        benchdata_arg = str(benchdata_path)
    out_path = tmp_path / "out.json"
    env = dict(os.environ)
    env.update({
        "BENCH_HW": json.dumps({"profile_id": "test-profile"}),
        "BENCH_SYSTEM": "minio", "BENCH_CONFIG": "c1", "BENCH_PROFILE": "medium",
        "BENCH_OP": "mixed", "BENCH_CONCURRENCY": "32", "BENCH_ROUND": "1",
        "BENCH_DURATION": "60", "BENCH_BUCKET": "warp-bench",
        "BENCH_ENDPOINT": "bench-minio:9000", "BENCH_IMAGE": "bench-warp:1.6.1",
        "BENCH_IMAGE_ID": "sha256:deadbeef",
        "BENCH_ARGS": "--no-color\n--full", "BENCH_CLIENT_CPUS": "2.0",
        "BENCH_CLIENT_MEMORY": "1024m", "BENCH_CLIENT_CONTAINER": "bench-warp",
        "BENCH_EXPECTED_CONTAINERS": "bench-minio", "BENCH_CPU_BUDGET_PCT": "600",
        "BENCH_WARP_EXIT": str(warp_exit), "BENCH_ANALYZE_EXIT": "0",
        "BENCH_STARTED_AT": "2026-08-20T00:00:00Z", "BENCH_ENDED_AT": "2026-08-20T00:02:00Z",
        "BENCH_DISK_BEFORE": "100000000000", "BENCH_DISK_AFTER": "90000000000",
        "BENCH_DISK_REQUIRED": "10000000000",
    })
    proc = subprocess.run(
        [sys.executable, str(MODULE_PATH), str(analysis_path), str(telemetry_path),
         benchdata_arg, str(out_path)],
        env=env, capture_output=True, text=True,
    )
    payload = json.loads(out_path.read_text()) if out_path.exists() else None
    return proc, payload


ANALYSIS = {
    "v": 2,
    "commandline": "warp mixed --secret-key=*REDACTED*",
    "total": {
        "total_requests": 100, "total_objects": 100, "total_bytes": 1000,
        "total_errors": 0,
        "start_time": "2026-08-20T00:00:00Z", "end_time": "2026-08-20T00:01:00Z",
        "throughput": {"measure_duration_millis": 58000, "bytes": 1000, "objects": 100,
                       "segmented": {"median_bps": 17, "median_ops": 1.7, "segments": []}},
    },
    "by_op_type": {"GET": {
        "total_requests": 100, "total_objects": 100, "total_bytes": 1000, "total_errors": 0,
        "throughput": {"measure_duration_millis": 58000, "bytes": 1000, "objects": 100,
                       "segmented": {"median_bps": 17, "median_ops": 1.7, "segments": []}},
        "requests_by_client": {"c1": [
            _window("2026-08-20T00:00:00Z", "2026-08-20T00:00:10Z", 90, 10.0),
            _window("2026-08-20T00:00:58Z", "2026-08-20T00:01:08Z", 2, 900.0),
        ]},
    }},
}


def test_end_to_end_ok_run_has_metrics_and_no_embedded_warp_block(tmp_path):
    proc, payload = _run_assembler(
        tmp_path, ANALYSIS,
        [f"{i},t,{ar.parse_rfc3339('2026-08-20T00:00:30Z') + i:.0f},server,bench-minio,550,1000,2147483648,0,0,0,0"
         for i in range(5)]
        + [f"{i},t,{ar.parse_rfc3339('2026-08-20T00:00:30Z') + i:.0f},client,bench-warp,40,100,1073741824,0,0,0,0"
           for i in range(5)],
    )
    assert proc.returncode == 0, proc.stderr
    assert payload["status"] in ("ok", "ok_with_warnings")
    assert "metrics" in payload
    # Finding 4: the raw analysis must not be embedded -- it is 88% of the file
    # and re-derivable from the kept benchdata.
    assert "warp" not in payload
    assert payload["run"]["warp_image_id"] == "sha256:deadbeef"


def test_end_to_end_warp_failure_writes_no_metrics_and_exits_nonzero(tmp_path):
    proc, payload = _run_assembler(tmp_path, ANALYSIS, [], warp_exit=137)
    assert proc.returncode != 0
    assert payload["status"] == "failed"
    assert "metrics" not in payload
    assert "warp" not in payload
    assert "warp_exit_zero" in payload["failed_checks"]


def test_end_to_end_full_benchdata_yields_true_quantiles(tmp_path):
    records = [FULL_HEADER]
    for i in range(100):
        # 1..100 ms TTFB, all inside the measured span
        start = "2026-08-20T00:00:30.000000000Z"
        first = "2026-08-20T00:00:30.%09dZ" % (i * 1_000_000 + 1_000_000)
        records.append(_record("GET", start, first, (i + 1) * 1_000_000))
    proc, payload = _run_assembler(
        tmp_path, ANALYSIS,
        [f"1,t,{ar.parse_rfc3339('2026-08-20T00:00:30Z'):.0f},server,bench-minio,550,1000,2147483648,0,0,0,0",
         f"1,t,{ar.parse_rfc3339('2026-08-20T00:00:30Z'):.0f},client,bench-warp,40,100,1073741824,0,0,0,0"],
        benchdata_text="\n".join(records),
    )
    assert proc.returncode == 0, proc.stderr
    latency = payload["metrics"]["by_op"]["GET"]["latency"]
    assert latency["source"] == "per_request"
    assert payload["metrics"]["latency_source"] == "per_request"
    assert latency["ttfb_millis"]["samples"] == 100
    assert latency["ttfb_millis"]["p50"] == pytest.approx(50.5, rel=1e-3)
    assert latency["ttfb_millis"]["p99"] == pytest.approx(99.01, rel=1e-3)
    assert "window_summary" not in latency


def test_end_to_end_without_full_falls_back_and_warns(tmp_path):
    proc, payload = _run_assembler(
        tmp_path, ANALYSIS,
        [f"1,t,{ar.parse_rfc3339('2026-08-20T00:00:30Z'):.0f},server,bench-minio,550,1000,2147483648,0,0,0,0",
         f"1,t,{ar.parse_rfc3339('2026-08-20T00:00:30Z'):.0f},client,bench-warp,40,100,1073741824,0,0,0,0"],
    )
    assert payload["metrics"]["latency_source"] == "window_summary"
    assert "latency_from_per_request_records" in payload["failed_checks"]
    assert payload["status"] == "ok_with_warnings"
    latency = payload["metrics"]["by_op"]["GET"]["latency"]
    assert "ttfb_millis" not in latency
    ttfb = latency["window_summary"]["ttfb"]
    # The trailing window starts at 00:00:58, inside the span, so it is kept --
    # but request-weighted it carries 2/92 of the weight, not half.
    assert ttfb["mean_millis"] == pytest.approx((90 * 10.0 + 2 * 900.0) / 92)


# ---------------------------------------------------------------------------
# warp's own record-validation artefacts vs real errors
# ---------------------------------------------------------------------------
def test_classify_errors_recognises_warps_timing_artefact():
    analysis = {"total": {"first_errors": ["Negative duration"]},
                "by_op_type": {"GET": {"first_errors": ["Negative duration"]}}}
    artifacts_only, messages = ar.classify_errors(analysis)
    assert artifacts_only is True
    assert len(messages) == 2


def test_classify_errors_does_not_whitewash_a_real_failure():
    analysis = {"total": {"first_errors": ["Negative duration",
                                           "InternalError: We encountered an internal error"]},
                "by_op_type": {}}
    artifacts_only, _ = ar.classify_errors(analysis)
    assert artifacts_only is False


def _errors_check(total_errors, requests, first_errors):
    analysis = {"total": {"first_errors": first_errors}, "by_op_type": {}}
    metrics = {"total": {"errors": total_errors, "requests": requests,
                         "measure_duration_seconds": 58.0}}
    checks, check = ar.make_checker()
    ar.evaluate_checks(
        check, warp_exit=0, analyze_exit=0, analysis=analysis, metrics=metrics,
        telemetry={"server": {"samples": 1, "containers": ["a"]},
                   "client": {"samples": 1, "cpu_saturation_mean": 0.1}},
        duration_requested=60, expected_containers=["a"], client_cpu_limit_pct=200.0,
        disk_free_after=10 ** 12, disk_required_bytes=0,
    )
    return {c["name"]: c for c in checks}


def test_a_handful_of_timing_artefacts_is_a_warning_not_a_fatal():
    by_name = _errors_check(3, 43717, ["Negative duration"])
    assert by_name["no_operation_errors"]["ok"] is True
    assert by_name["no_analysis_artifact_errors"]["ok"] is False


def test_real_s3_errors_stay_fatal():
    by_name = _errors_check(3, 43717, ["InternalError"])
    assert by_name["no_operation_errors"]["ok"] is False


def test_too_many_artefacts_is_fatal_even_though_they_are_artefacts():
    # 1% of requests -- ten times the tolerated rate. Something is wrong even
    # if every sampled message looks benign.
    by_name = _errors_check(1000, 100000, ["Negative duration"])
    assert by_name["no_operation_errors"]["ok"] is False


# ---------------------------------------------------------------------------
# Per-op TTFB availability
# ---------------------------------------------------------------------------
def test_an_op_without_per_request_ttfb_falls_back_rather_than_losing_it():
    """PUT records carry last_byte, not first_byte -- see build_metrics."""
    analysis = {
        "v": 2,
        "total": {"throughput": {"measure_duration_millis": 1000},
                  "start_time": "2026-08-20T00:00:00Z", "end_time": "2026-08-20T00:01:00Z"},
        "by_op_type": {"PUT": {
            "total_requests": 90, "total_errors": 0,
            "throughput": {"measure_duration_millis": 1000, "segmented": {}},
            "requests_by_client": {"c1": [
                _window("2026-08-20T00:00:00Z", "2026-08-20T00:00:10Z", 90, 111.6),
            ]},
        }},
    }
    records = {"PUT": {"ttfb": [], "request": [10.0, 20.0, 30.0], "requests": 3, "errors": 0}}
    metrics = ar.build_metrics(analysis, records, None, None)
    latency = metrics["by_op"]["PUT"]["latency"]
    assert latency["source"] == "per_request"
    assert latency["request_millis"]["p50"] == pytest.approx(20.0)
    assert latency["ttfb_source"] == "window_summary"
    assert latency["ttfb_window_summary"]["mean_millis"] == pytest.approx(111.6)


def test_an_op_with_no_ttfb_anywhere_says_so():
    analysis = {
        "v": 2,
        "total": {"throughput": {"measure_duration_millis": 1000}},
        "by_op_type": {"STAT": {
            "total_requests": 5, "total_errors": 0,
            "throughput": {"measure_duration_millis": 1000, "segmented": {}},
            "requests_by_client": {"c1": [{
                "start_time": "2026-08-20T00:00:00Z", "end_time": "2026-08-20T00:00:10Z",
                "single_sized_requests": {"requests": 5, "dur_median_millis": 9.9,
                                          "fastest_millis": 1.0, "slowest_millis": 2.0},
            }]},
        }},
    }
    records = {"STAT": {"ttfb": [], "request": [9.9], "requests": 1, "errors": 0}}
    metrics = ar.build_metrics(analysis, records, None, None)
    assert metrics["by_op"]["STAT"]["latency"]["ttfb_source"] == "not_recorded"
