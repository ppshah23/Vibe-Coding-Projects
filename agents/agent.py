"""
Minimal LangGraph agent for systems performance analysis.
Graph: extract_metrics -> summarize_bottlenecks
"""

import os
import re
from typing import TypedDict

import anthropic
from langgraph.graph import StateGraph, END

# ---------------------------------------------------------------------------
# State schema shared across all nodes
# ---------------------------------------------------------------------------


class PerfState(TypedDict):
    raw_perf_output: str  # input: raw `perf stat` text
    metrics: dict  # populated by node 1
    summary: str  # populated by node 2


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
            messages=[{
                "role": "user",
                "content": (
                    "You are a systems performance engineer. Given these perf stat metrics, "
                    "identify the primary bottleneck and explain it in one concise paragraph. "
                    "Be specific about the numbers.\n\n"
                    f"Metrics:\n{metrics_text}"
                ),
            }],
        )
        summary = response.content[0].text.strip()
    else:
        summary = rule_based_summary

    return {**state, "summary": summary}


# ---------------------------------------------------------------------------
# Build and compile the graph
# ---------------------------------------------------------------------------


def build_graph() -> StateGraph:
    graph = StateGraph(PerfState)

    graph.add_node("extract_metrics", extract_metrics)
    graph.add_node("summarize_bottlenecks", summarize_bottlenecks)

    # Linear flow: extract -> summarize -> end
    graph.set_entry_point("extract_metrics")
    graph.add_edge("extract_metrics", "summarize_bottlenecks")
    graph.add_edge("summarize_bottlenecks", END)

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

if __name__ == "__main__":
    app = build_graph()
    result = app.invoke(
        {"raw_perf_output": SAMPLE_PERF_OUTPUT, "metrics": {}, "summary": ""}
    )

    print("=== Extracted Metrics ===")
    for k, v in result["metrics"].items():
        print(f"  {k}: {v}")

    print("\n=== Bottleneck Summary ===")
    print(result["summary"])
