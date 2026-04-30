## Graph Structure

The first node parses the raw perf stat string using regex patterns and extracts numeric counters into a structured dictionary. Also computes three derived ratios that are more useful for bottleneck detection than the raw counts: IPC (instructions per cycle), cache miss rate, and branch miss rate.

The second node applies threshold rules to the extracted metrics and produces an explanation identifying the bottlenecks and how they relate to each other. Thresholds used: cache miss rate > 10%, IPC < 1.0, branch miss rate > 2%, context switches > 1,000.



## State Schema

raw perf output: Raw perf stat output string passed in at invocation

metrics: Node 1: Extracted counters and derived ratios

summary: Node 2: Plain-English bottleneck analysis paragraph



## Setup

Be within the bash terminal.
python3 -m venv venv
source venv/bin/activate
pip install langgraph
pip install langgraph anthropic
export ANTHROPIC_API_KEY="<your-api-key-here>"
python3 <path>/agent.py (use your specific file path)






## Sample Output


=== Extracted Metrics ===
  task\\\_clock\\\_ms: 2.345678
  context\\\_switches: 5.0
  cpu\\\_migrations: 0.0
  page\\\_faults: 312.0
  cycles: 8432101.0
  instructions: 6125678.0
  cache\\\_references: 512034.0
  cache\\\_misses: 89456.0
  branch\\\_instructions: 1234567.0
  branch\\\_misses: 45678.0
  elapsed\\\_seconds: 0.002851754
  cache\\\_miss\\\_rate: 0.1747
  ipc: 0.7265
  branch\\\_miss\\\_rate: 0.037

=== Bottleneck Summary ===
## Performance Bottleneck Analysis

The biggest bottleneck is memory inefficiency driven by a high cache miss rate, combined with poor CPU efficiency.

The most telling indicator is the 17.47% cache miss rate (89,456 misses out of 512,034 cache references), which is severely elevated. A healthy rate typically sits below 1-5%. This is directly causing the IPC (Instructions Per Cycle) of only 0.7265, meaning the CPU is completing fewer than one instruction per cycle when modern pipelines should sustain 2-4+ IPC under efficient conditions. The CPU is fundamentally stalling, waiting on memory fetches. Supporting this, the workload executed only 6,125,678 instructions across 8,432,101 cycles, a ~27% instruction "deficit" relative to cycles consumed, with those extra cycles largely burned as stall cycles awaiting cache-line fills. The 312 page faults add further memory pressure.