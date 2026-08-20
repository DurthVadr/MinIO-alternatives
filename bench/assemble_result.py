#!/usr/bin/env python3
"""Assemble one benchmark result from warp's output and the telemetry CSV.

Usage:
    assemble_result.py <analysis.json> <telemetry.csv> <benchdata|-> <out.json>

Run metadata arrives through BENCH_* environment variables (see main()), which
keeps every value out of any shell-interpolated string. Everything above main()
is pure and is what tests/test_assemble_result.py exercises.

WHY THIS IS A MODULE AND NOT A HEREDOC
--------------------------------------
It used to be 444 lines of Python inside bench/run-workload.sh, which meant the
two places this project can most easily publish a wrong number -- how a
measurement is summarised, and how a degraded one is labelled -- were the two
places nothing could test. Both defects found in review lived here.
"""
import calendar
import csv
import json
import os
import statistics
import sys
import time

SCHEMA = 2

# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------
_MINUTE_EPOCH = {}


def parse_rfc3339(value):
    """Epoch seconds (float) from an RFC3339 stamp, or None.

    warp emits nanosecond fractions with a varying number of digits, which
    datetime.fromisoformat has historically been picky about, and this is
    called once per request record (hundreds of thousands per matrix), so the
    minute prefix is memoised and only the seconds are done per call.
    """
    if not isinstance(value, str) or len(value) < 19:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1]
    if len(text) < 19 or text[10] != "T":
        return None
    minute_key = text[:16]
    base = _MINUTE_EPOCH.get(minute_key)
    if base is None:
        try:
            base = float(calendar.timegm(time.strptime(minute_key, "%Y-%m-%dT%H:%M")))
        except ValueError:
            return None
        _MINUTE_EPOCH[minute_key] = base
    try:
        seconds = float(text[17:])
    except ValueError:
        return None
    return base + seconds


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------
def percentile(ordered, pct):
    """Linear interpolation between closest ranks, on an already-sorted list."""
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (pct / 100.0)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    frac = rank - low
    return ordered[low] + (ordered[high] - ordered[low]) * frac


def summarise(values):
    """True quantiles over a sample of per-request latencies, in milliseconds."""
    if not values:
        return None
    ordered = sorted(values)
    return {
        "samples": len(ordered),
        "mean": statistics.fmean(ordered),
        "min": ordered[0],
        "p50": percentile(ordered, 50),
        "p90": percentile(ordered, 90),
        "p95": percentile(ordered, 95),
        "p99": percentile(ordered, 99),
        "max": ordered[-1],
        "stddev": statistics.pstdev(ordered) if len(ordered) > 1 else 0.0,
        "method": "linear interpolation between closest ranks",
    }


def mean_or_none(values):
    return statistics.fmean(values) if values else None


# ---------------------------------------------------------------------------
# Per-request records (warp --full)
# ---------------------------------------------------------------------------
FULL_COLUMNS = ("op", "error", "start", "first_byte", "duration_ns")


def read_full_records(text, window_start=None, window_end=None):
    """Per-op latency samples from warp's --full benchdata (a TSV).

    Columns: idx thread op client_id n_objects bytes endpoint file error
             start first_byte last_byte end duration_ns cat

    Returns {op: {"ttfb": [ms], "request": [ms], "requests": n, "errors": n}}
    or None if the text is not warp's per-request format (which is what a
    non---full run's .json.zst decompresses to).
    """
    lines = text.splitlines()
    if not lines:
        return None
    header = lines[0].split("\t")
    if not all(column in header for column in FULL_COLUMNS):
        return None
    index = {name: header.index(name) for name in FULL_COLUMNS}
    by_op = {}
    for line in lines[1:]:
        if not line:
            continue
        fields = line.split("\t")
        if len(fields) <= index["duration_ns"]:
            continue
        op = fields[index["op"]]
        started = parse_rfc3339(fields[index["start"]])
        if started is None:
            continue
        if window_start is not None and not (window_start <= started <= window_end):
            continue
        slot = by_op.setdefault(op, {"ttfb": [], "request": [], "requests": 0, "errors": 0})
        slot["requests"] += 1
        if fields[index["error"]]:
            # An errored request has no meaningful latency; count it, and keep
            # it out of the percentiles rather than letting a fast 500 look
            # like a fast GET.
            slot["errors"] += 1
            continue
        try:
            slot["request"].append(int(fields[index["duration_ns"]]) / 1e6)
        except (TypeError, ValueError):
            pass
        first_byte = parse_rfc3339(fields[index["first_byte"]])
        if first_byte is not None:
            slot["ttfb"].append((first_byte - started) * 1000.0)
    return by_op


# ---------------------------------------------------------------------------
# warp's aggregation windows (the fallback when --full is off)
# ---------------------------------------------------------------------------
TTFB_KEYS = ("average_millis", "p25_millis", "median_millis", "p75_millis",
             "p90_millis", "p99_millis", "std_dev_millis")
DURATION_KEYS = ("dur_avg_millis", "dur_median_millis", "dur_90_millis",
                 "dur_99_millis", "std_dev_millis")


def collect_windows(op_block, window_start=None, window_end=None):
    """warp's 10-second aggregation windows for one operation.

    Windows that do not overlap the measured span are dropped: warp's latency
    windows run past `total.end_time` (70s of windows for a 58s measured span
    on the verification run), and the trailing ramp-down window holds a
    handful of very slow requests.
    """
    out = []
    for windows in (op_block.get("requests_by_client") or {}).values():
        for window in windows or []:
            started = parse_rfc3339(window.get("start_time"))
            ended = parse_rfc3339(window.get("end_time"))
            if window_start is not None and started is not None and ended is not None:
                if ended <= window_start or started >= window_end:
                    continue
            single = window.get("single_sized_requests")
            multi = window.get("multi_sized_requests")
            if single:
                out.append({
                    "start_time": window.get("start_time"),
                    "end_time": window.get("end_time"),
                    "requests": single.get("requests") or 0,
                    "ttfb": single.get("first_byte"),
                    "request": {k: single.get(k) for k in DURATION_KEYS
                                + ("fastest_millis", "slowest_millis")},
                    "sizing": "single",
                })
            elif multi:
                for by_size in multi.get("by_size") or []:
                    out.append({
                        "start_time": window.get("start_time"),
                        "end_time": window.get("end_time"),
                        "requests": by_size.get("requests") or 0,
                        "ttfb": by_size.get("first_byte"),
                        "request": None,
                        "sizing": "multi",
                    })
    return out


def weighted_window_summary(windows):
    """Request-weighted summary of warp's per-window figures.

    Two deliberate choices:

    * Weighting by request count, not the plain mean warp's console uses.
      Unweighted, the verification run's 7th GET window -- 1.5s of ramp-down
      holding 24 of 6127 requests, 0.4% of the work, with a median TTFB of
      1932ms against ~80ms through the body of the run -- got 14.3% of the
      weight and pushed the published median from 109ms to 411ms. For the
      average, the weighted figure is not merely the better estimate: it is
      the run mean by construction.

    * The percentile fields are named `*_window_mean`. A mean of p99s is not a
      p99, and weighting it makes it a weighted mean of p99s, which is still
      not a quantile. Naming them this way means a reader cannot mistake them
      for the real thing, and the per-window values are kept alongside so a
      distribution can be shown instead of a fabricated single number. True
      quantiles need `--full`; see read_full_records.
    """
    usable = [w for w in windows if w.get("requests")]
    if not usable:
        return None

    def weighted(key, block_name):
        pairs = [(w["requests"], (w.get(block_name) or {}).get(key)) for w in usable]
        pairs = [(n, v) for n, v in pairs if isinstance(v, (int, float))]
        total = sum(n for n, _ in pairs)
        if not total:
            return None
        return sum(n * v for n, v in pairs) / total

    def extreme(key, block_name, fn):
        values = [(w.get(block_name) or {}).get(key) for w in usable]
        values = [v for v in values if isinstance(v, (int, float))]
        return fn(values) if values else None

    summary = {
        "windows": len(usable),
        "requests": sum(w["requests"] for w in usable),
        "weighting": "request-weighted mean across warp's aggregation windows",
    }
    if any(w.get("ttfb") for w in usable):
        summary["ttfb"] = {
            "mean_millis": weighted("average_millis", "ttfb"),
            "p25_millis_window_mean": weighted("p25_millis", "ttfb"),
            "p50_millis_window_mean": weighted("median_millis", "ttfb"),
            "p75_millis_window_mean": weighted("p75_millis", "ttfb"),
            "p90_millis_window_mean": weighted("p90_millis", "ttfb"),
            "p99_millis_window_mean": weighted("p99_millis", "ttfb"),
            "min_millis": extreme("fastest_millis", "ttfb", min),
            "max_millis": extreme("slowest_millis", "ttfb", max),
        }
    if any(w.get("request") for w in usable):
        summary["request"] = {
            "mean_millis": weighted("dur_avg_millis", "request"),
            "p50_millis_window_mean": weighted("dur_median_millis", "request"),
            "p90_millis_window_mean": weighted("dur_90_millis", "request"),
            "p99_millis_window_mean": weighted("dur_99_millis", "request"),
            "min_millis": extreme("fastest_millis", "request", min),
            "max_millis": extreme("slowest_millis", "request", max),
        }
    summary["per_window"] = [
        {
            "start_time": w["start_time"],
            "end_time": w["end_time"],
            "requests": w["requests"],
            "ttfb_p50_millis": (w.get("ttfb") or {}).get("median_millis"),
            "ttfb_p99_millis": (w.get("ttfb") or {}).get("p99_millis"),
            "request_p50_millis": (w.get("request") or {}).get("dur_median_millis"),
        }
        for w in usable
    ]
    return summary


# ---------------------------------------------------------------------------
# Throughput
# ---------------------------------------------------------------------------
def rate(value, millis):
    if not isinstance(value, (int, float)) or not millis:
        return None
    return value * 1000.0 / millis


def throughput_metrics(block):
    throughput = block.get("throughput") or {}
    millis = throughput.get("measure_duration_millis") or 0
    segmented = throughput.get("segmented") or {}
    return {
        "measure_duration_seconds": millis / 1000.0 if millis else None,
        "requests": block.get("total_requests"),
        "objects": block.get("total_objects"),
        "bytes": block.get("total_bytes"),
        "errors": block.get("total_errors"),
        "bytes_per_sec_avg": rate(throughput.get("bytes"), millis),
        "obj_per_sec_avg": rate(throughput.get("objects"), millis),
        "bytes_per_sec_median": segmented.get("median_bps"),
        "bytes_per_sec_fastest": segmented.get("fastest_bps"),
        "bytes_per_sec_slowest": segmented.get("slowest_bps"),
        "obj_per_sec_median": segmented.get("median_ops"),
        "obj_per_sec_fastest": segmented.get("fastest_ops"),
        "obj_per_sec_slowest": segmented.get("slowest_ops"),
        "segments": len(segmented.get("segments") or []),
    }


def build_metrics(analysis, full_records, window_start, window_end):
    total = analysis.get("total") or {}
    by_op_type = analysis.get("by_op_type") or {}
    metrics = {
        "analysis_schema_version": analysis.get("v"),
        "latency_source": "per_request" if full_records else "window_summary",
        "total": throughput_metrics(total),
        "by_op": {},
    }
    for op_name, block in sorted(by_op_type.items()):
        entry = throughput_metrics(block)
        records = (full_records or {}).get(op_name)
        windows = collect_windows(block, window_start, window_end)
        window_summary = weighted_window_summary(windows)

        if records:
            latency = {
                "source": "per_request",
                "records": records["requests"],
                "record_errors": records["errors"],
                "request_millis": summarise(records["request"]),
            }
            if records["ttfb"]:
                latency["ttfb_millis"] = summarise(records["ttfb"])
                latency["ttfb_source"] = "per_request"
            else:
                # VERIFIED against a real run: warp's per-request CSV only fills
                # `first_byte` for downloads. PUT rows carry `last_byte` (when
                # the client finished writing the body) instead, and that is not
                # the same quantity -- 3.67ms mean against the 111.59ms warp
                # itself reports as PUT TTFB. DELETE and STAT have neither. So
                # for those operations true TTFB quantiles are simply not
                # recoverable from --full, and falling back to warp's own
                # aggregate beats silently publishing no PUT latency at all.
                if (window_summary or {}).get("ttfb"):
                    latency["ttfb_source"] = "window_summary"
                    # Keep the whole envelope, not just the scalar block. This
                    # is the ONLY operation that uses the fallback -- GET has
                    # true quantiles and DELETE/STAT have no TTFB at all -- so
                    # publishing only the `*_window_mean` scalars here would
                    # discard the distribution for precisely the case the
                    # rename exists to protect. Task 7 needs `per_window` to
                    # render PUT latency as a spread rather than one number.
                    #
                    # The `request` sub-block is deliberately left out: true
                    # request-duration quantiles are already in
                    # latency["request_millis"] above, and carrying warp's
                    # window means for the same quantity alongside them would
                    # give a reader two different answers to one question.
                    latency["ttfb_window_summary"] = {
                        "windows": window_summary["windows"],
                        "requests": window_summary["requests"],
                        "weighting": window_summary["weighting"],
                        "ttfb": window_summary["ttfb"],
                        "per_window": window_summary["per_window"],
                    }
                else:
                    latency["ttfb_source"] = "not_recorded"
        else:
            latency = {"source": "window_summary"}
            if full_records:
                latency["note"] = "no per-request records for this operation"
            latency["window_summary"] = window_summary
            latency["ttfb_source"] = (
                "window_summary" if (window_summary or {}).get("ttfb") else "not_recorded"
            )
        entry["latency"] = latency
        metrics["by_op"][op_name] = entry
    return metrics


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------
def aggregate_telemetry(rows, window_start=None, window_end=None,
                        client_cpu_limit_pct=None, server_cpu_budget_pct=None):
    """Per-role and per-container aggregates from the telemetry CSV rows.

    Instants are keyed on the collector's cycle counter, not on a wall-clock
    second: a cycle that straddles a second boundary would otherwise be split
    in two, which halves the apparent per-system CPU of a multi-container
    system exactly when it is busiest. Summing per instant (rather than
    averaging per container) is what makes SeaweedFS config-2's five
    containers comparable to MinIO's one against the same 6 CPU budget.
    """
    per_container = {}
    per_instant = {}
    total_rows = 0
    kept_rows = 0
    for row in rows:
        total_rows += 1
        role = row.get("role") or ""
        name = row.get("container") or ""
        if not role or not name:
            continue
        if window_start is not None:
            try:
                when = float(row.get("epoch") or "")
            except (TypeError, ValueError):
                continue
            if when < window_start or when > window_end:
                continue
        kept_rows += 1
        instant_key = row.get("cycle") or row.get("epoch") or ""
        slot = per_container.setdefault(
            (role, name), {"samples": 0, "cpu": [], "mem": [], "mem_limit": None}
        )
        slot["samples"] += 1
        try:
            cpu = float(row["cpu_pct"])
        except (TypeError, ValueError, KeyError):
            cpu = None
        else:
            slot["cpu"].append(cpu)
        try:
            mem = int(row["mem_bytes"])
        except (TypeError, ValueError, KeyError):
            mem = None
        else:
            slot["mem"].append(mem)
        # max, not last: the final sample of an exiting container reports a
        # limit of 0, which would otherwise overwrite the real limit.
        try:
            limit = int(row["mem_limit_bytes"])
        except (TypeError, ValueError, KeyError):
            limit = None
        if limit:
            slot["mem_limit"] = max(slot["mem_limit"] or 0, limit)
        per_instant.setdefault((role, instant_key), {})[name] = {"cpu": cpu, "mem": mem}

    telemetry = {"rows_total": total_rows, "rows_in_window": kept_rows}
    for role in ("server", "client"):
        instants = [v for (r, _), v in per_instant.items() if r == role]
        cpus = [
            sum(c["cpu"] for c in inst.values() if c["cpu"] is not None)
            for inst in instants if any(c["cpu"] is not None for c in inst.values())
        ]
        mems = [
            sum(c["mem"] for c in inst.values() if c["mem"] is not None)
            for inst in instants if any(c["mem"] is not None for c in inst.values())
        ]
        containers = {}
        for (r, name), slot in sorted(per_container.items()):
            if r != role:
                continue
            containers[name] = {
                "samples": slot["samples"],
                "cpu_pct_mean": mean_or_none(slot["cpu"]),
                "cpu_pct_max": max(slot["cpu"]) if slot["cpu"] else None,
                "mem_bytes_mean": mean_or_none(slot["mem"]),
                "mem_bytes_peak": max(slot["mem"]) if slot["mem"] else None,
                "mem_limit_bytes": slot["mem_limit"],
            }
        block = {
            "samples": len(instants),
            "containers": sorted(containers),
            "cpu_pct_sum_mean": mean_or_none(cpus),
            "cpu_pct_sum_max": max(cpus) if cpus else None,
            "mem_bytes_sum_mean": mean_or_none(mems),
            "mem_bytes_sum_peak": max(mems) if mems else None,
            "per_container": containers,
        }
        if role == "server" and server_cpu_budget_pct:
            # A cgroup with no burst budget cannot exceed its quota, so
            # anything above it is measurement error. Counted and disclosed
            # rather than quietly averaged in.
            block["cpu_pct_budget"] = server_cpu_budget_pct
            over = [c for c in cpus if c > server_cpu_budget_pct]
            block["cpu_pct_samples_over_budget"] = len(over)
            block["cpu_pct_fraction_over_budget"] = len(over) / len(cpus) if cpus else None
            block["cpu_pct_max_over_budget_ratio"] = (
                max(cpus) / server_cpu_budget_pct if cpus else None
            )
        if role == "client" and client_cpu_limit_pct:
            block["cpu_pct_limit"] = client_cpu_limit_pct
            block["cpu_saturation_mean"] = (
                block["cpu_pct_sum_mean"] / client_cpu_limit_pct
                if block["cpu_pct_sum_mean"] is not None else None
            )
        telemetry[role] = block
    return telemetry


# ---------------------------------------------------------------------------
# Checks and status
# ---------------------------------------------------------------------------
# warp's --full mode validates every per-request record and discards ones whose
# timestamps do not make sense, reporting them as operation errors. Observed on
# a clean run against MinIO: 3 "Negative duration" out of 43,717 requests
# (0.007%), with no error field set on any record in the benchdata and no
# negative duration_ns anywhere in it -- so these are warp's own clock
# bookkeeping, not the object store failing a request. Tolerating them keeps
# the matrix from labelling nearly every --full run "suspect"; naming them
# explicitly, and capping the rate, keeps a real 5xx from hiding behind that
# tolerance.
WARP_RECORD_ARTIFACTS = frozenset({"negative duration"})
ARTIFACT_ERROR_RATE_LIMIT = 0.001  # 0.1%


def classify_errors(analysis):
    """(artifacts_only, messages) for the errors warp reported.

    `first_errors` is warp's sample of error strings, not necessarily all of
    them, which is why the caller also caps the acceptable rate rather than
    trusting this alone.
    """
    messages = []
    for block in ((analysis or {}).get("total") or {}, ):
        messages.extend(block.get("first_errors") or [])
    for block in ((analysis or {}).get("by_op_type") or {}).values():
        messages.extend(block.get("first_errors") or [])
    if not messages:
        return False, []
    artifacts_only = all(m.strip().lower() in WARP_RECORD_ARTIFACTS for m in messages)
    return artifacts_only, messages

def make_checker():
    checks = []

    def check(name, ok, severity, detail):
        checks.append({"name": name, "ok": bool(ok), "severity": severity, "detail": detail})
        return bool(ok)

    return checks, check


def decide_status(checks, warp_ok, analysis_ok):
    """ok | ok_with_warnings | suspect | failed.

    A warn-level failure must not leave the run labelled "ok". The check that
    fires when the warp client was itself the bottleneck is warn-level, and a
    consumer filtering on status == "ok" would otherwise publish a number that
    measures the load generator -- most likely on the 4 KiB profile, which is
    exactly where the headline small-object claim gets tested.
    """
    if not warp_ok or not analysis_ok:
        return "failed"
    if any(c["severity"] == "fatal" and not c["ok"] for c in checks):
        return "suspect"
    if any(not c["ok"] for c in checks):
        return "ok_with_warnings"
    return "ok"


def evaluate_checks(check, *, warp_exit, analyze_exit, analysis, metrics, telemetry,
                    duration_requested, expected_containers, client_cpu_limit_pct,
                    disk_free_after, disk_required_bytes):
    check("warp_exit_zero", warp_exit == 0, "fatal", "warp exit code %d" % warp_exit)
    check("analysis_parsed", analysis is not None, "fatal",
          "warp analyze --json produced a readable document" if analysis is not None
          else "no readable analysis (analyze exit %d)" % analyze_exit)

    if metrics is not None:
        measured = metrics["total"]["measure_duration_seconds"] or 0.0
        errors = metrics["total"]["errors"] or 0
        requests = metrics["total"]["requests"] or 0
        artifacts_only, messages = classify_errors(analysis)
        rate_ok = requests and (errors / requests) <= ARTIFACT_ERROR_RATE_LIMIT
        tolerable = errors == 0 or (artifacts_only and rate_ok)
        check("no_operation_errors", tolerable, "fatal",
              "warp reported %s error(s) in %s requests%s"
              % (errors, requests,
                 "" if errors == 0 else " (%s)" % ", ".join(sorted(set(messages))[:3])))
        check("no_analysis_artifact_errors", errors == 0, "warn",
              "%s of %s requests were discarded by warp's own record validation"
              % (errors, requests))
        check("measured_window_plausible", measured >= 0.4 * duration_requested, "fatal",
              "measured %.1fs of a requested %ds window (warp trims its own ramp)"
              % (measured, duration_requested))
        check("work_was_done", (metrics["total"]["requests"] or 0) > 0, "fatal",
              "%s requests recorded" % metrics["total"]["requests"])
        check("latency_from_per_request_records",
              metrics.get("latency_source") == "per_request", "warn",
              "latency source is %r; only per-request records (warp --full) give true "
              "quantiles" % metrics.get("latency_source"))

    server = telemetry.get("server") or {}
    client = telemetry.get("client") or {}
    observed = server.get("containers") or []
    check("telemetry_has_samples", (server.get("samples") or 0) > 0, "fatal",
          "%s server telemetry sample instants" % server.get("samples"))
    check("telemetry_containers_complete", sorted(observed) == sorted(expected_containers),
          "fatal", "sampled %s; expected %s" % (observed or "nothing", expected_containers))
    check("telemetry_client_sampled", (client.get("samples") or 0) > 0, "warn",
          "%s client telemetry sample instants" % client.get("samples"))

    over_fraction = server.get("cpu_pct_fraction_over_budget")
    over_ratio = server.get("cpu_pct_max_over_budget_ratio")
    check("telemetry_cpu_plausible",
          (over_fraction is None or over_fraction <= 0.25)
          and (over_ratio is None or over_ratio <= 1.25), "warn",
          "%s of server CPU samples exceeded the %s%% cgroup budget; worst was %s of it"
          % ("unknown fraction" if over_fraction is None else "%.0f%%" % (over_fraction * 100),
             server.get("cpu_pct_budget"),
             "unknown" if over_ratio is None else "%.2fx" % over_ratio))

    saturation = client.get("cpu_saturation_mean")
    check("client_not_saturated", saturation is None or saturation < 0.90, "warn",
          "warp averaged %s of its %s%% CPU budget; above ~0.90 the number is the "
          "client's, not the server's"
          % ("unknown" if saturation is None else "%.2f" % saturation, client_cpu_limit_pct))

    if disk_required_bytes:
        check("disk_headroom_after_run", disk_free_after >= disk_required_bytes, "warn",
              "%.1f GiB free after the run against %.1f GiB needed for another one; "
              "Docker Desktop reclaims freed volume space on its own schedule"
              % (disk_free_after / 2 ** 30, disk_required_bytes / 2 ** 30))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def load_benchdata_text(path):
    """Decompress warp's benchdata, or return None if it cannot be read."""
    if not path or path == "-" or not os.path.exists(path):
        return None
    try:
        import compression.zstd as zstd
    except ImportError:  # pragma: no cover - Python < 3.14
        return None
    try:
        with open(path, "rb") as fh:
            return zstd.decompress(fh.read()).decode("utf-8", "replace")
    except (OSError, ValueError):
        return None


def main(argv):
    if len(argv) != 5:
        sys.exit("usage: assemble_result.py <analysis.json> <telemetry.csv> "
                 "<benchdata|-> <out.json>")
    analysis_path, telemetry_path, benchdata_path, out_path = argv[1:]
    env = os.environ

    warp_exit = int(env["BENCH_WARP_EXIT"])
    analyze_exit = int(env["BENCH_ANALYZE_EXIT"])
    duration_requested = int(env["BENCH_DURATION"])
    client_cpu_limit_pct = float(env["BENCH_CLIENT_CPUS"]) * 100.0
    expected_containers = sorted(env["BENCH_EXPECTED_CONTAINERS"].split())
    try:
        server_cpu_budget_pct = float(env.get("BENCH_CPU_BUDGET_PCT") or "")
    except ValueError:
        server_cpu_budget_pct = None
    disk_required_bytes = int(env.get("BENCH_DISK_REQUIRED") or 0)

    run = {
        "system": env["BENCH_SYSTEM"],
        "config": env["BENCH_CONFIG"],
        "profile": env["BENCH_PROFILE"],
        "concurrency": int(env["BENCH_CONCURRENCY"]),
        "round": int(env["BENCH_ROUND"]),
        "warp_op": env["BENCH_OP"],
        "warp_args": [a for a in env["BENCH_ARGS"].split("\n") if a],
        "warp_image": env["BENCH_IMAGE"],
        "warp_image_id": env.get("BENCH_IMAGE_ID") or None,
        "duration_requested_seconds": duration_requested,
        "bucket": env["BENCH_BUCKET"],
        "endpoint": env["BENCH_ENDPOINT"],
        "client": {
            "container": env["BENCH_CLIENT_CONTAINER"],
            "cpus": env["BENCH_CLIENT_CPUS"],
            "memory": env["BENCH_CLIENT_MEMORY"],
            "on_network": "s3bench",
        },
        "expected_containers": expected_containers,
        "server_cpu_budget_pct": server_cpu_budget_pct,
        "started_at": env["BENCH_STARTED_AT"],
        "ended_at": env["BENCH_ENDED_AT"],
        # BENCHDATA IS DELIBERATELY NOT COMMITTED -- see the matching rule in
        # .gitignore. This is a decision, not an oversight, and "fixing" it
        # would break something real: the project's answer to being measured on
        # one laptop is CONTRIBUTING.md inviting people to run this harness on
        # their own hardware and send the results as a pull request. Tracked
        # result JSONs, telemetry and logs come to roughly 6 MB for a full
        # matrix; adding warp's per-request records would make it ~300 MB per
        # contributor, growing with every hardware profile, which kills that
        # workflow outright.
        #
        # The chain of evidence does not depend on those bytes being in git.
        # True quantiles are computed from the records HERE, at assemble time,
        # and land in the result. The size and SHA256 below identify the exact
        # file they came from, so a contributor keeps their benchdata locally
        # and can offer it out of band if one of their figures is ever
        # disputed, and anyone re-running the harness can compare both the
        # derived figures and the hashes.
        "benchdata_file": os.path.basename(benchdata_path) if benchdata_path not in ("", "-") else None,
        "benchdata_bytes": int(env.get("BENCH_BENCHDATA_BYTES") or 0) or None,
        "benchdata_sha256": env.get("BENCH_BENCHDATA_SHA256") or None,
        "host_disk_free_bytes_before": int(env["BENCH_DISK_BEFORE"]),
        "host_disk_free_bytes_after": int(env["BENCH_DISK_AFTER"]),
        "host_disk_required_bytes": disk_required_bytes or None,
    }

    analysis = None
    if warp_exit == 0 and analyze_exit == 0:
        try:
            with open(analysis_path) as fh:
                analysis = json.load(fh)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            run["analysis_error"] = str(exc)

    # Restrict everything to the span warp says it measured. The collector runs
    # across warp's prepare and cleanup phases too -- 17 of 86 seconds on one
    # verification run -- and warp's own latency windows overrun total.end_time.
    window_start = window_end = None
    window_source = "whole run (no warp analysis to bound it)"
    if analysis is not None:
        total = analysis.get("total") or {}
        window_start = parse_rfc3339(total.get("start_time"))
        window_end = parse_rfc3339(total.get("end_time"))
        if window_start is not None and window_end is not None and window_end > window_start:
            window_source = "warp total.start_time..total.end_time"
            run["warp_commandline"] = analysis.get("commandline")
        else:
            window_start = window_end = None

    full_records = None
    if analysis is not None:
        text = load_benchdata_text(benchdata_path)
        if text:
            full_records = read_full_records(text, window_start, window_end)

    metrics = build_metrics(analysis, full_records, window_start, window_end) if analysis else None

    try:
        with open(telemetry_path, newline="") as fh:
            rows = list(csv.DictReader(fh))
    except OSError:
        rows = []
    telemetry = aggregate_telemetry(
        rows, window_start, window_end, client_cpu_limit_pct, server_cpu_budget_pct
    )
    telemetry["csv"] = os.path.basename(telemetry_path)
    telemetry["window"] = {
        "source": window_source,
        "start_epoch": window_start,
        "end_epoch": window_end,
    }

    checks, check = make_checker()
    evaluate_checks(
        check,
        warp_exit=warp_exit, analyze_exit=analyze_exit, analysis=analysis,
        metrics=metrics, telemetry=telemetry, duration_requested=duration_requested,
        expected_containers=expected_containers,
        client_cpu_limit_pct=client_cpu_limit_pct,
        disk_free_after=run["host_disk_free_bytes_after"],
        disk_required_bytes=disk_required_bytes,
    )
    status = decide_status(checks, warp_exit == 0, analysis is not None)

    result = {
        "schema": SCHEMA,
        "status": status,
        "failed_checks": [c["name"] for c in checks if not c["ok"]],
        "hardware_profile": json.loads(env["BENCH_HW"]),
        "run": run,
        "checks": checks,
        "telemetry": telemetry,
    }
    # No "metrics" key at all on a failed run: an absent number cannot be
    # misread, whereas a zero or null inside a populated block can. The raw
    # warp analysis is NOT embedded -- it was 88% of the file and is fully
    # re-derivable from the kept .benchdata.*.zst by re-running warp analyze.
    if metrics is not None:
        result["metrics"] = metrics

    with open(out_path, "w") as fh:
        json.dump(result, fh, indent=2)
        fh.write("\n")

    print("%s  status=%s" % (out_path, status))
    for c in checks:
        if not c["ok"]:
            print("  %-5s %s: %s" % (c["severity"].upper(), c["name"], c["detail"]),
                  file=sys.stderr)
    # ok_with_warnings still exits 0: the run produced usable data and the
    # matrix should continue. Only a run whose numbers cannot be trusted at all
    # stops the caller.
    return 0 if status in ("ok", "ok_with_warnings") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
