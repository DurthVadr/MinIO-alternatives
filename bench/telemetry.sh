#!/usr/bin/env bash
# Sample per-container CPU, memory, network and block IO once a second for
# every container of one system, plus the warp client container.
#
# Usage: telemetry.sh <system> <output.csv>   (run in background; kill to stop)
# Env:   TELEMETRY_CLIENT_CONTAINER  name of the load generator (default bench-warp)
#        TELEMETRY_INTERVAL          seconds between samples (default 1.0)
#        BENCH_PYTHON                interpreter to use (default python3; no
#                                    third-party packages needed)
#
# WHY NOT `docker stats`
# ----------------------
# The brief sampled `docker stats --no-stream` and used its CPUPerc column.
# That column is computed by the daemon as
#     cpu_delta / system_delta * online_cpus * 100
# where both deltas come from two reads taken back-to-back inside one
# --no-stream call. When the daemon is busy -- which is exactly what a
# benchmark makes it -- system_delta is undermeasured and the percentage
# explodes. Measured, not theorised: in a verification run against a container
# whose cgroup is hard-capped at 6 CPUs (cpu.max = "600000 100000", confirmed
# by reading it inside the container), 22 of 35 samples came back ABOVE 600%,
# peaking at 2016% -- higher than the 8-vCPU VM can produce even in principle.
# More than half the samples were physically impossible, so no amount of
# averaging or outlier-trimming afterwards would have rescued them.
#
# What this does instead is read the cumulative counter the daemon keeps --
# cpu_stats.cpu_usage.total_usage, a monotonic nanosecond total -- and divide
# its delta by our own wall-clock delta. That yields "CPU cores consumed x 100",
# directly comparable to the 600% budget, and it cannot exceed what the cgroup
# actually allows because both numerator and denominator are measured over the
# same, known, one-second interval.
#
# CONTAINER SELECTION
# -------------------
# The brief matched on container-name substrings plus a hardcoded
# `bench-swfs-*` special case. That is fragile: SeaweedFS config-2 runs
# bench-seaweedfs AND bench-swfs-vol0..3, so four of its five containers were
# only sampled because of that one exception, and undersampling a system's
# containers understates exactly the system that used the most resources.
# bench/stack.sh starts every system as compose project "bench-<system>", so
# every container of a system carries com.docker.compose.project=bench-<system>.
# Filtering on that label is exact and needs no special cases. The warp client
# is a plain `docker run` outside any compose project, so it is picked up by
# name and tagged role=client -- keeping it visible, and keeping it out of the
# server aggregate.
set -euo pipefail

system="${1:-}"
out="${2:-}"
if [ -z "$system" ] || [ -z "$out" ]; then
  echo "usage: telemetry.sh <system> <output.csv>" >&2
  exit 2
fi

PY="${BENCH_PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || { echo "telemetry: python interpreter '$PY' not found" >&2; exit 1; }

# Ask docker where its socket is rather than guessing /var/run/docker.sock:
# Docker Desktop on macOS puts the real socket under ~/.docker/run/.
docker_host="${DOCKER_HOST:-}"
if [ -z "$docker_host" ]; then
  docker_host="$(docker context inspect --format '{{.Endpoints.docker.Host}}' 2>/dev/null || true)"
fi

TELEMETRY_SYSTEM="$system" \
TELEMETRY_OUT="$out" \
TELEMETRY_DOCKER_HOST="$docker_host" \
TELEMETRY_CLIENT="${TELEMETRY_CLIENT_CONTAINER:-bench-warp}" \
TELEMETRY_INTERVAL="${TELEMETRY_INTERVAL:-1.0}" \
exec "$PY" - <<'PY'
"""Sample container resource usage from the Docker Engine API once a second."""
import calendar
import http.client
import json
import os
import socket
import sys
import time

env = os.environ
out_path = env["TELEMETRY_OUT"]
project = "bench-" + env["TELEMETRY_SYSTEM"]
client_name = env["TELEMETRY_CLIENT"]
interval = float(env["TELEMETRY_INTERVAL"])

docker_host = env.get("TELEMETRY_DOCKER_HOST") or "unix:///var/run/docker.sock"
if not docker_host.startswith("unix://"):
    sys.exit("telemetry: only unix:// docker endpoints are supported, got %r" % docker_host)
socket_path = docker_host[len("unix://"):]


class UnixHTTPConnection(http.client.HTTPConnection):
    """http.client over an AF_UNIX socket -- the Docker Engine API's transport."""

    def __init__(self, path, timeout=15):
        super().__init__("localhost", timeout=timeout)
        self.unix_path = path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.unix_path)
        self.sock = sock


class Docker:
    def __init__(self, path):
        self.path = path
        self.conn = None

    def get(self, url):
        for attempt in (0, 1):
            try:
                if self.conn is None:
                    self.conn = UnixHTTPConnection(self.path)
                self.conn.request("GET", url)
                resp = self.conn.getresponse()
                body = resp.read()
                if resp.status != 200:
                    return None
                return json.loads(body)
            except (OSError, http.client.HTTPException, ValueError):
                # One reconnect, then give up on this sample rather than
                # killing a collector that has a benchmark riding on it.
                try:
                    if self.conn is not None:
                        self.conn.close()
                except OSError:
                    pass
                self.conn = None
                if attempt == 1:
                    return None
        return None


def blkio(stats):
    entries = (stats.get("blkio_stats") or {}).get("io_service_bytes_recursive") or []
    read = write = 0
    seen = False
    for entry in entries:
        op = str(entry.get("op", "")).lower()
        value = entry.get("value")
        if not isinstance(value, int):
            continue
        if op == "read":
            read += value
            seen = True
        elif op == "write":
            write += value
            seen = True
    return (read, write) if seen else (None, None)


def netio(stats):
    nets = stats.get("networks")
    if not isinstance(nets, dict) or not nets:
        return None, None
    rx = sum(int(v.get("rx_bytes", 0) or 0) for v in nets.values())
    tx = sum(int(v.get("tx_bytes", 0) or 0) for v in nets.values())
    return rx, tx


def memory(stats):
    mem = stats.get("memory_stats") or {}
    usage = mem.get("usage")
    limit = mem.get("limit")
    if isinstance(usage, int):
        # Match what `docker stats` reports: page cache that the kernel can
        # drop under pressure is not the container's working set.
        inactive = (mem.get("stats") or {}).get("inactive_file")
        if isinstance(inactive, int) and inactive <= usage:
            usage -= inactive
    return usage, limit


def sampled_at(stats):
    """Epoch seconds (float) of the daemon's own reading of the counters.

    Using the local clock instead would fold this process's request latency
    into the interval: the counter is read by the daemon partway through each
    request, so timing the intervals from when the *responses* arrive makes a
    slow request followed by a fast one look like a short interval, which
    inflates the CPU percentage derived from it. The daemon stamps every stats
    payload with "read"; that is the clock the counters actually advance on.
    """
    text = stats.get("read")
    if not isinstance(text, str):
        return None
    text = text.strip()
    if text.endswith("Z"):
        text = text[:-1]
    whole, _, fraction = text.partition(".")
    try:
        parsed = time.strptime(whole, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    seconds = float(calendar.timegm(parsed))
    if fraction:
        digits = "".join(ch for ch in fraction if ch.isdigit())[:9]
        if digits:
            seconds += int(digits) / (10.0 ** len(digits))
    return seconds


def cell(value):
    return "" if value is None else str(value)


docker = Docker(socket_path)
previous = {}

with open(out_path, "w", buffering=1) as fh:
    fh.write("ts,epoch,role,container,cpu_pct,mem_bytes,mem_limit_bytes,"
             "net_rx_bytes,net_tx_bytes,blk_read_bytes,blk_write_bytes\n")
    while True:
        cycle_started = time.monotonic()
        containers = docker.get("/containers/json") or []
        wanted = []
        for entry in containers:
            names = [n.lstrip("/") for n in (entry.get("Names") or [])]
            labels = entry.get("Labels") or {}
            name = names[0] if names else entry.get("Id", "")[:12]
            if labels.get("com.docker.compose.project") == project:
                wanted.append((entry["Id"], name, "server"))
            elif name == client_name or client_name in names:
                wanted.append((entry["Id"], name, "client"))

        live = set()
        for cid, name, role in wanted:
            live.add(cid)
            # one-shot=true returns immediately with the cumulative counters
            # instead of blocking a second to compute the daemon's own
            # (unreliable, see the header) percentage for us.
            stats = docker.get("/containers/%s/stats?stream=false&one-shot=true" % cid)
            if not stats:
                continue
            now = sampled_at(stats)
            if now is None:
                now = time.time()
            total = ((stats.get("cpu_stats") or {}).get("cpu_usage") or {}).get("total_usage")
            cpu_pct = None
            if isinstance(total, int):
                prev = previous.get(cid)
                if prev is not None:
                    elapsed = now - prev[0]
                    used_ns = total - prev[1]
                    if elapsed > 0 and used_ns >= 0:
                        cpu_pct = used_ns / (elapsed * 1e9) * 100.0
                previous[cid] = (now, total)
            if cpu_pct is None:
                # First observation of this container: there is no interval to
                # divide by yet. Emitting a row with a blank or zero CPU would
                # put a sample into the average that measured nothing.
                continue
            mem_used, mem_limit = memory(stats)
            rx, tx = netio(stats)
            read, write = blkio(stats)
            fh.write(",".join([
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                str(int(time.time())),
                role,
                name,
                "%.2f" % cpu_pct,
                cell(mem_used), cell(mem_limit),
                cell(rx), cell(tx), cell(read), cell(write),
            ]) + "\n")

        for cid in list(previous):
            if cid not in live:
                del previous[cid]

        slack = interval - (time.monotonic() - cycle_started)
        time.sleep(slack if slack > 0 else 0.05)
PY
