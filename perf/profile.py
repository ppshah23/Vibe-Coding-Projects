#!/usr/bin/env python3
"""
profile.py — Matrix multiplication profiler using perf stat -p <PID>

Sweeps across multiple matrix sizes, running each for DURATION_PER_SIZE
seconds, then prints a single comparison table so you can see how cache
miss rate and IPC change as the matrix grows.

Requirements:
  pip install numpy
  sudo apt install linux-tools-common linux-tools-generic   # WSL / Linux
"""

import re
import signal
import time
import multiprocessing
import subprocess

import numpy as np

# Sizes to compare in one run. Total runtime = len(SIZES) * DURATION_PER_SIZE.
SIZES            = [256, 512, 1024, 2048]
DURATION_PER_SIZE = 60   # seconds per size

EVENTS = [
    "instructions",
    "cycles",
    "cache-misses",
    "cache-references",
    "context-switches",
]


# ── Part 1: Workload ──────────────────────────────────────────────────────────

def workload(size: int, duration_seconds: int):
    """
    Multiply two N×N matrices in a loop for exactly `duration_seconds` seconds.

    The loop checks the clock on every iteration rather than counting a fixed
    number of repetitions. This guarantees perf stat always has a full
    measurement window of activity regardless of how fast or slow the CPU
    completes each multiply.
    """
    # Record the exact moment we want to stop.
    # time.monotonic() never jumps backwards (unlike time.time()) so the
    # deadline is always exact even if the system clock is adjusted mid-run.
    deadline = time.monotonic() + duration_seconds
    count    = 0

    while time.monotonic() < deadline:
        A = np.random.rand(size, size)
        B = np.random.rand(size, size)
        _ = A @ B
        count += 1

    print(f"  [workload {size}×{size}] {count} multiplications in {duration_seconds}s",
          flush=True)


# ── Part 2: perf stat monitoring by PID ──────────────────────────────────────

def monitor_with_perf(size: int, duration_seconds: int) -> str:
    """
    Start the workload in a child process, then attach perf stat to it by PID.

    Uses Popen (non-blocking) instead of run (blocking) so we can send perf a
    signal ourselves once the workload finishes. When attaching by PID, perf
    stat does NOT exit on its own when the target process dies — it waits
    indefinitely until it receives a signal.
    """
    proc = multiprocessing.Process(target=workload, args=(size, duration_seconds))
    proc.start()

    # ── workload process and perf stat are both running from here ─────────────
    # Popen launches perf without blocking so we can wait on the workload
    # and then stop perf ourselves.
    perf = subprocess.Popen(
        ["perf", "stat", "-p", str(proc.pid), "-e", ",".join(EVENTS)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    proc.join()   # block until the workload's timed loop finishes

    # SIGINT is the correct signal for perf stat: it catches it, prints the
    # counter summary to stderr, then exits cleanly.
    # SIGTERM would cause perf to quit silently with no output.
    perf.send_signal(signal.SIGINT)
    # ── perf stat exits and flushes its report after receiving SIGINT ─────────

    # perf stat always writes its counter report to stderr, never stdout.
    _, stderr = perf.communicate()
    return stderr


# ── Part 3: Parsing perf stat's stderr ───────────────────────────────────────

# perf stat stderr looks like:
#
#  Performance counter stats for process id '219482':
#
#       901,234,567      instructions              #    1.87  insn per cycle
#       481,823,409      cycles
#            45,231      cache-misses              #    6.12% of all cache refs
#           738,942      cache-references
#                 3      context-switches
#
#         20.001234567 seconds time elapsed
#
# Parser must handle:
#   1. Comma separators in numbers  →  strip before int()
#   2. Counter name comes AFTER the number
#   3. Trailing '#' annotations    →  ignored by the regex
#   4. <not counted> / <not supported>  →  stored as "N/A"

def parse_perf_output(stderr: str) -> dict:
    metrics = {}
    for event in EVENTS:
        pattern = rf"([\d,]+|<not counted>|<not supported>)\s+{re.escape(event)}"
        match   = re.search(pattern, stderr)
        if match:
            raw = match.group(1)
            metrics[event] = int(raw.replace(",", "")) if raw[0].isdigit() else "N/A"
        else:
            metrics[event] = "N/A"
    return metrics


# ── Part 4: Derived metrics ───────────────────────────────────────────────────

def compute_derived(metrics: dict) -> dict:
    """
    IPC  = instructions / cycles
           Below 1.0 means the CPU is stalling — waiting on data from RAM.

    Cache miss rate = cache-misses / cache-references * 100
           Above ~10% means the working set no longer fits in L3 cache and
           the workload is memory-bandwidth bound.
    """
    instructions = metrics.get("instructions")
    cycles       = metrics.get("cycles")
    cache_misses = metrics.get("cache-misses")
    cache_refs   = metrics.get("cache-references")

    if isinstance(instructions, int) and isinstance(cycles, int) and cycles > 0:
        ipc = round(instructions / cycles, 3)
    else:
        ipc = "N/A"

    if isinstance(cache_misses, int) and isinstance(cache_refs, int) and cache_refs > 0:
        miss_rate = round(cache_misses / cache_refs * 100, 2)
    else:
        miss_rate = "N/A"

    return {"IPC": ipc, "cache miss rate": miss_rate}


# ── Part 5: Display ───────────────────────────────────────────────────────────

# All columns in the order they appear in the table
COLUMNS = EVENTS + ["IPC", "cache miss rate"]

def print_table(rows: list):
    """
    Print a comparison table — one row per matrix size, one column per metric.
    rows is a list of (size, metrics_dict, derived_dict).
    """
    size_w  = 10   # width of the Size column
    col_w   = 22   # width of every metric column
    total_w = size_w + col_w * len(COLUMNS)
    divider = "─" * total_w

    # Header
    print()
    print(divider)
    header = f"{'Size':>{size_w}}" + "".join(f"{c:>{col_w}}" for c in COLUMNS)
    print(header)
    print(divider)

    for size, metrics, derived in rows:
        all_values = {**metrics, **derived}
        label      = f"{size}×{size}"
        row        = f"{label:>{size_w}}"

        for col in COLUMNS:
            val = all_values.get(col, "N/A")
            if col == "cache miss rate":
                display = f"{val:.2f}%" if isinstance(val, float) else val
            elif col == "IPC":
                display = f"{val:.3f}"  if isinstance(val, float) else val
            else:
                display = f"{val:,}"    if isinstance(val, int)   else val
            row += f"{display:>{col_w}}"

        # Flag memory-bound rows inline
        miss_rate = derived.get("cache miss rate")
        ipc       = derived.get("IPC")
        flags = []
        if isinstance(miss_rate, float) and miss_rate > 10.0:
            flags.append("← high miss rate")
        if isinstance(ipc, float) and ipc < 1.0:
            flags.append("← stalling on RAM")
        print(row + ("  " + "  ".join(flags) if flags else ""))

    print(divider)
    print()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    total = len(SIZES) * DURATION_PER_SIZE
    print(f"Sweeping {len(SIZES)} matrix sizes × {DURATION_PER_SIZE}s each "
          f"(~{total}s total)...\n")

    rows = []
    for size in SIZES:
        print(f"  Running {size}×{size} for {DURATION_PER_SIZE}s...")
        stderr  = monitor_with_perf(size, DURATION_PER_SIZE)
        metrics = parse_perf_output(stderr)
        derived = compute_derived(metrics)
        rows.append((size, metrics, derived))

    print_table(rows)


if __name__ == "__main__":
    multiprocessing.set_start_method("fork", force=True)
    main()
