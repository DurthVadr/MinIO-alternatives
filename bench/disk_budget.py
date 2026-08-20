#!/usr/bin/env python3
"""Single source of truth for the pre-run host disk requirement.

    disk_budget.py <workloads.yaml> <profile-id> <config> <floor-gib>

prints the whole number of GiB that must be free on the host before that
(profile, config) may start.

WHY THIS IS A SEPARATE FILE
---------------------------
Two callers need the same number and must never disagree about it:
bench/run-workload.sh refuses to start a run below it, and bench/run.sh waits
for free space to come back above it. When the driver waited on a flat floor
while the measurement refused on a scaled one, a transient dip became a
permanent "no result" instead of a pause -- and which cells were lost depended
on the order they happened to run in, so the same matrix produced different
holes on different nights.

THE FORMULA
-----------
    required = ceil(write_gib_estimate) + floor

Read it as what it means: enough room for everything this run writes, plus the
margin you want still free when it finishes. Nothing else.

It replaces `max(floor, ceil(write * 2**is_c2 * 2 + 4))`, which was written
before any of these profiles had run and was 2.3x over-conservative against
the first 24 measured runs -- it demanded 24 GiB for a run whose worst
observed consumption was 10.23 GiB, and 36 GiB for `bigdata-put` on config-2,
which no machine in this study has. The doubling and the +4 GiB were slop, not
measurement.

THE CONFIG-2 MULTIPLIER IS GONE, AND THAT IS A MEASUREMENT
----------------------------------------------------------
The old formula doubled the estimate for config-2 on the reasoning that a
second replica or parity doubles the stored bytes. Every result file records
host free space before and after its run; across the first 24 runs the worst
config-2 consumption was 1.09-1.14x its config-1 twin, not 2x:

    profile      c1 worst   c2 worst   ratio
    medium          9.00      10.23     1.14
    bigdata-get     6.33       6.88     1.09
    small           0.58       0.66     1.14

So config-2 does cost more, and the direction the old term encoded was right;
the magnitude was wrong by roughly a factor of two. Rather than keep a
multiplier that measurement does not support, `write_gib_estimate` is now the
worst consumption observed across BOTH configs for that profile, and the
config term is gone. Note that these deltas are contaminated downward by
Docker Desktop reclaiming earlier runs' space mid-measurement -- two `small`
runs even show negative consumption -- so the worst observed figure is a lower
bound on the truth, which is the safe direction for a maximum.

`disk_headroom_after_run` in bench/assemble_result.py re-checks the same
number after the run and is the backstop if an estimate is still too low.
"""
import math
import os
import sys

import yaml


def required_gib(workloads, profile_id, config, floor_gib):
    """Whole GiB of free host disk needed before (profile_id, config) starts."""
    if config not in ("c1", "c2"):
        raise ValueError("unknown config %r" % (config,))
    profiles = {p["id"]: p for p in workloads["profiles"]}
    if profile_id not in profiles:
        raise KeyError("no profile %r in workloads.yaml (have: %s)"
                       % (profile_id, ", ".join(sorted(profiles))))
    write = float(profiles[profile_id].get(
        "write_gib_estimate", workloads["defaults"]["write_gib_estimate"]))
    if write < 0:
        raise ValueError("write_gib_estimate for %r is negative" % (profile_id,))
    return int(math.ceil(write)) + int(floor_gib)


def main(argv):
    if len(argv) != 5:
        sys.exit("usage: disk_budget.py <workloads.yaml> <profile-id> <config> <floor-gib>")
    path, profile_id, config, floor = argv[1:]
    try:
        floor_gib = int(floor)
    except ValueError:
        sys.exit("disk_budget: floor must be a whole number of GiB, got %r" % (floor,))
    if floor_gib < 0:
        sys.exit("disk_budget: floor must not be negative")
    with open(path) as handle:
        workloads = yaml.safe_load(handle)
    try:
        print(required_gib(workloads, profile_id, config, floor_gib))
    except (KeyError, ValueError) as exc:
        sys.exit("disk_budget: %s" % exc)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
