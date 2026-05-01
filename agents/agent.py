"""
Minimal LangGraph agent for systems performance analysis.
Graph: extract_metrics -> summarize_bottlenecks -> visualize_metrics -> generate_flamegraph
"""

import os
import re
from typing import TypedDict

import matplotlib

matplotlib.use("Agg")  # non-interactive backend — must be set before importing pyplot
import matplotlib.pyplot as plt

import anthropic
from langgraph.graph import StateGraph, END

# ---------------------------------------------------------------------------
# State schema shared across all nodes
# ---------------------------------------------------------------------------


class PerfState(TypedDict):
    raw_perf_output: str       # input: raw `perf stat` text
    perf_script_output: str    # input: simulated `perf script` stack traces
    metrics: dict              # populated by node 1
    summary: str               # populated by node 2
    chart_path: str            # populated by node 3
    flamegraph_path: str       # populated by node 4


# ---------------------------------------------------------------------------
# Node 1 — extract key metrics from raw perf stat output
# ---------------------------------------------------------------------------


def extract_metrics(state: PerfState) -> PerfState:
    """Parse numeric values out of `perf stat` output into a structured dict."""
    raw = state["raw_perf_output"]

    # Patterns that cover the most common perf stat counters
    patterns = {
        "task_clock_ms": r"([\d,\.]+)\s+task-clock",
        "context_switches": r"([\d,\.]+)\s+context-switches",
        "cpu_migrations": r"([\d,\.]+)\s+cpu-migrations",
        "page_faults": r"([\d,\.]+)\s+page-faults",
        "cycles": r"([\d,\.]+)\s+cycles",
        "instructions": r"([\d,\.]+)\s+instructions",
        "cache_references": r"([\d,\.]+)\s+cache-references",
        "cache_misses": r"([\d,\.]+)\s+cache-misses",
        "branch_instructions": r"([\d,\.]+)\s+branch-instructions",
        "branch_misses": r"([\d,\.]+)\s+branch-misses",
        "elapsed_seconds": r"([\d,\.]+)\s+seconds time elapsed",
    }

    metrics: dict = {}
    for key, pattern in patterns.items():
        match = re.search(pattern, raw, re.IGNORECASE)
        if match:
            # Strip locale-specific commas before converting to float
            metrics[key] = float(match.group(1).replace(",", ""))

    # Derived ratios that are useful for bottleneck detection
    if metrics.get("cache_references") and metrics.get("cache_misses"):
        metrics["cache_miss_rate"] = round(
            metrics["cache_misses"] / metrics["cache_references"], 4
        )
    if metrics.get("instructions") and metrics.get("cycles"):
        metrics["ipc"] = round(metrics["instructions"] / metrics["cycles"], 4)
    if metrics.get("branch_instructions") and metrics.get("branch_misses"):
        metrics["branch_miss_rate"] = round(
            metrics["branch_misses"] / metrics["branch_instructions"], 4
        )

    return {**state, "metrics": metrics}


# ---------------------------------------------------------------------------
# Node 2 — use Claude to summarize bottlenecks from the extracted metrics
# ---------------------------------------------------------------------------


def summarize_bottlenecks(state: PerfState) -> PerfState:
    """Apply threshold rules to metrics and report the worst bottleneck."""
    metrics = state["metrics"]

    if not metrics:
        return {
            **state,
            "summary": "No metrics could be extracted from the provided output.",
        }

    # Each rule is (severity_score, label, explanation).
    # Higher score = worse; we surface only the top culprit.
    candidates = []

    cache_miss_rate = metrics.get("cache_miss_rate")
    if cache_miss_rate is not None and cache_miss_rate > 0.10:
        pct = round(cache_miss_rate * 100, 1)
        candidates.append(
            (
                cache_miss_rate,
                "High cache miss rate",
                f"Cache miss rate is {pct}% (threshold: 10%). This means roughly 1 in "
                f"{int(1/cache_miss_rate)} cache lookups must go to main memory, stalling "
                "the CPU while it waits for data. The program likely accesses memory in a "
                "non-sequential pattern (pointer chasing, large working sets, or random "
                "access), preventing the hardware prefetcher from hiding latency.",
            )
        )

    ipc = metrics.get("ipc")
    if ipc is not None and ipc < 1.0:
        candidates.append(
            (
                1.0 - ipc,  # distance from the threshold becomes the score
                "Low IPC (instructions per cycle)",
                f"IPC is {ipc} (threshold: 1.0). The CPU is completing less than one "
                "instruction per clock cycle, which typically points to pipeline stalls "
                "caused by memory latency, data-dependency chains, or frequent branch "
                "mispredictions forcing the front-end to refill. A low IPC is often the "
                "downstream symptom of a cache or branch bottleneck.",
            )
        )

    branch_miss_rate = metrics.get("branch_miss_rate")
    if branch_miss_rate is not None and branch_miss_rate > 0.02:
        pct = round(branch_miss_rate * 100, 1)
        candidates.append(
            (
                branch_miss_rate,
                "High branch miss rate",
                f"Branch miss rate is {pct}% (threshold: 2%). Each mispredicted branch "
                "flushes the CPU pipeline and wastes ~15-20 cycles of speculative work. "
                "This often stems from data-dependent conditionals (e.g. tree traversals, "
                "unpredictable input dispatch) where the branch predictor cannot build a "
                "reliable pattern.",
            )
        )

    context_switches = metrics.get("context_switches")
    if context_switches is not None and context_switches > 1000:
        candidates.append(
            (
                context_switches / 10_000,  # normalise for comparison
                "Excessive context switches",
                f"Context switch count is {int(context_switches):,} (threshold: 1,000). "
                "Frequent context switches indicate heavy thread contention or I/O blocking, "
                "causing the OS scheduler to replace the process before it finishes a useful "
                "quantum. Each switch also pollutes CPU caches and TLB, compounding any "
                "existing memory bottleneck.",
            )
        )

    if not candidates:
        rule_based_summary = (
            "No significant bottlenecks detected. All measured metrics are within "
            "acceptable thresholds. Consider profiling at a finer granularity (e.g. "
            "perf record + perf report) if performance still feels off."
        )
    else:
        # Report all flagged bottlenecks, worst score first
        ranked = sorted(candidates, key=lambda c: c[0], reverse=True)
        lines = []
        for i, (_, label, explanation) in enumerate(ranked, start=1):
            lines.append(f"Bottleneck #{i} — {label}:\n{explanation}")
        rule_based_summary = "\n\n".join(lines)

    # Try Claude API; fall back to rule-based summary if no key is set
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        metrics_text = "\n".join(f"  {k}: {v}" for k, v in metrics.items())
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "You are a systems performance engineer. Given these perf stat metrics, "
                        "identify the primary bottleneck and explain it in one concise paragraph. "
                        "Be specific about the numbers.\n\n"
                        f"Metrics:\n{metrics_text}"
                    ),
                }
            ],
        )
        summary = response.content[0].text.strip()
    else:
        summary = rule_based_summary

    return {**state, "summary": summary}


# ---------------------------------------------------------------------------
# Node 3 — visualize extracted metrics as a line plot
# ---------------------------------------------------------------------------

# Healthy thresholds for the three derived ratios.
# IPC: higher is better (threshold is the minimum acceptable value).
# Miss rates: lower is better (threshold is the maximum acceptable value).
_THRESHOLDS = {
    "IPC": ("ipc", 1.0, "higher is better"),
    "Cache Miss Rate": ("cache_miss_rate", 0.10, "lower is better"),
    "Branch Miss Rate": ("branch_miss_rate", 0.02, "lower is better"),
}


def visualize_metrics(state: PerfState) -> PerfState:
    """Save a line plot of the three key derived ratios to outputs/metrics_line.png."""
    metrics = state["metrics"]

    labels, values, thresholds = [], [], []
    for display_name, (key, threshold, _) in _THRESHOLDS.items():
        if key in metrics:
            labels.append(display_name)
            values.append(metrics[key])
            thresholds.append(threshold)

    if not labels:
        return {**state, "chart_path": ""}

    outputs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outputs")
    os.makedirs(outputs_dir, exist_ok=True)

    fig, ax = plt.subplots(figsize=(8, 4))

    ax.plot(
        labels, values, marker="o", linewidth=2, color="steelblue", label="Measured"
    )
    ax.plot(
        labels,
        thresholds,
        marker="x",
        linewidth=1.5,
        linestyle="--",
        color="crimson",
        label="Threshold",
    )

    # Annotate each measured point with its numeric value
    for x, y in zip(labels, values):
        ax.annotate(
            f"{y:.3f}",
            xy=(x, y),
            xytext=(0, 8),
            textcoords="offset points",
            ha="center",
            fontsize=9,
        )

    ax.set_title("Perf Stat Key Metrics vs Thresholds")
    ax.set_ylabel("Value")
    ax.set_xlabel("Metric")
    ax.legend()
    ax.grid(True, alpha=0.3)
    fig.tight_layout()

    chart_path = os.path.join(outputs_dir, "metrics_line.png")
    fig.savefig(chart_path)
    plt.close(fig)

    return {**state, "chart_path": chart_path}


# ---------------------------------------------------------------------------
# Node 4 — parse perf script stack traces and render a flame graph
# ---------------------------------------------------------------------------


def _parse_perf_script(text: str) -> list:
    """Return list of stacks in root→leaf order from perf script output."""
    stacks, current = [], []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            if current:
                stacks.append(list(reversed(current)))
                current = []
        elif line.startswith(("\t", " ")):
            parts = stripped.split(None, 1)
            current.append(parts[1] if len(parts) > 1 else parts[0])
    if current:
        stacks.append(list(reversed(current)))
    return stacks


def _build_tree(stacks: list) -> dict:
    root = {"name": "all", "count": 0, "children": {}}
    for stack in stacks:
        root["count"] += 1
        node = root
        for frame in stack:
            if frame not in node["children"]:
                node["children"][frame] = {"name": frame, "count": 0, "children": {}}
            node = node["children"][frame]
            node["count"] += 1
    return root


def _layout(node: dict, x: float = 0.0, depth: int = 0) -> list:
    """DFS traversal; returns [(name, x, count, depth), ...]."""
    result = [(node["name"], x, node["count"], depth)]
    child_x = x
    for child in node["children"].values():
        result.extend(_layout(child, child_x, depth + 1))
        child_x += child["count"]
    return result


def generate_flamegraph(state: PerfState) -> PerfState:
    """Render a flame graph from perf script stack traces to outputs/flamegraph.png."""
    text = state["perf_script_output"]
    if not text.strip():
        return {**state, "flamegraph_path": ""}

    stacks = _parse_perf_script(text)
    root = _build_tree(stacks)
    entries = _layout(root)
    total = root["count"]
    max_depth = max(d for _, _, _, d in entries)

    fig, ax = plt.subplots(figsize=(12, max(4, (max_depth + 1) * 0.7)))
    cmap = plt.cm.YlOrRd

    for name, x, count, depth in entries:
        w = count / total
        color = cmap(0.2 + 0.6 * depth / max(max_depth, 1))
        ax.broken_barh(
            [(x / total, w)], (depth, 0.85),
            facecolors=[color], edgecolors="white", linewidth=0.5,
        )
        if w > 0.04:
            ax.text(
                x / total + w / 2, depth + 0.425, name,
                ha="center", va="center", fontsize=7, clip_on=True,
            )

    ax.set_xlim(0, 1)
    ax.set_ylim(-0.1, max_depth + 1)
    ax.set_xlabel("Fraction of Samples")
    ax.set_title("Flame Graph — perf script (simulated)")
    ax.set_yticks([])
    fig.tight_layout()

    outputs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outputs")
    os.makedirs(outputs_dir, exist_ok=True)
    flamegraph_path = os.path.join(outputs_dir, "flamegraph.png")
    fig.savefig(flamegraph_path, dpi=150)
    plt.close(fig)

    return {**state, "flamegraph_path": flamegraph_path}


# ---------------------------------------------------------------------------
# Build and compile the graph
# ---------------------------------------------------------------------------


def build_graph() -> StateGraph:
    graph = StateGraph(PerfState)

    graph.add_node("extract_metrics", extract_metrics)
    graph.add_node("summarize_bottlenecks", summarize_bottlenecks)
    graph.add_node("visualize_metrics", visualize_metrics)
    graph.add_node("generate_flamegraph", generate_flamegraph)

    # Linear flow: extract -> summarize -> visualize -> flamegraph -> end
    graph.set_entry_point("extract_metrics")
    graph.add_edge("extract_metrics", "summarize_bottlenecks")
    graph.add_edge("summarize_bottlenecks", "visualize_metrics")
    graph.add_edge("visualize_metrics", "generate_flamegraph")
    graph.add_edge("generate_flamegraph", END)

    return graph.compile()


# ---------------------------------------------------------------------------
# Entry point — run with a sample perf stat snippet
# ---------------------------------------------------------------------------

SAMPLE_PERF_OUTPUT = """
 Performance counter stats for 'ls -la':

          2.345678      task-clock (msec)         #    0.823 CPUs utilized
                 5      context-switches          #    0.002 M/sec
                 0      cpu-migrations            #    0.000 K/sec
               312      page-faults               #    0.133 M/sec
         8,432,101      cycles                    #    3.594 GHz
         6,125,678      instructions              #    0.73  insn per cycle
           512,034      cache-references          #  218.307 M/sec
            89,456      cache-misses              #   17.471 % of all cache refs
         1,234,567      branch-instructions       #  526.167 M/sec
            45,678      branch-misses             #    3.700 % of all branches

       0.002851754 seconds time elapsed
"""

# Compact representation of realistic `ls -la` call stacks (root → leaf, count).
# Mirrors what `perf record -g | perf script` would produce for listing a large directory.
_FLAME_STACKS = [
    (["_start", "__libc_start_main", "main", "print_long_format", "lstat64"], 15),
    (["_start", "__libc_start_main", "main", "print_dir", "getdents64"], 10),
    (["_start", "__libc_start_main", "main", "print_long_format", "vfprintf", "write"], 8),
    (["_start", "__libc_start_main", "main", "xmalloc", "malloc"], 5),
    (["_start", "__libc_start_main", "main", "opendir"], 2),
]


def _make_perf_script(stacks: list) -> str:
    """Serialize compact stack definitions into perf script text format."""
    lines = []
    ts = 0.001
    for stack, count in stacks:
        for _ in range(count):
            lines.append(f"ls 1234 [000] {ts:.3f}: cycles:")
            for frame in reversed(stack):  # perf script lists leaf first
                lines.append(f"\t55b000 {frame}")
            lines.append("")
            ts += 0.001
    return "\n".join(lines)


SAMPLE_PERF_SCRIPT = _make_perf_script(_FLAME_STACKS)

if __name__ == "__main__":
    app = build_graph()
    result = app.invoke(
        {
            "raw_perf_output": SAMPLE_PERF_OUTPUT,
            "perf_script_output": SAMPLE_PERF_SCRIPT,
            "metrics": {},
            "summary": "",
            "chart_path": "",
            "flamegraph_path": "",
        }
    )

    print("=== Extracted Metrics ===")
    for k, v in result["metrics"].items():
        print(f"  {k}: {v}")

    print("\n=== Bottleneck Summary ===")
    print(result["summary"])

    if result["chart_path"]:
        print(f"\n=== Chart saved to {result['chart_path']} ===")

    if result["flamegraph_path"]:
        print(f"\n=== Flame graph saved to {result['flamegraph_path']} ===")
