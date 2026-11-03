# Experiment protocol

This document specifies the questions, matched comparisons, workloads, metrics, and interpretation rules used by `run.py`. It is an experiment protocol, not a claim that any particular scheduler wins.

## Research questions

**RQ1 — Does operation reordering help independently of residency policy?**

Compare `dag_lru` against `lsqca_lazy`. Both use LRU replacement and demand loads; the former may select any dependency-ready operation, whereas the latter issues in program order.

**RQ2 — Does changing residency replacement help after gate reordering?**

Compare `dag_next_use` against `dag_lru`. Both use the same ready-operation priority and demand-transfer policy; only the replacement heuristic changes from LRU to DAG-aware farthest-next-use.

**RQ3 — Does transfer arbitration matter?**

Compare `dag_next_use_nearest` against `dag_next_use`. Both use the same DAG issue and residency policies; the transfer request key changes from critical-path priority to nearest current scan-row travel.

**RQ4 — Does dependency-frontier prefetch add value?**

Compare `joint-prefetch(W=1/2/4,critical)` against `dag_next_use`. Prefetch targets operands of successor operations whose predecessors have started and at least one predecessor remains in flight. W limits the number of speculative qubits outstanding (in flight or resident); prefetch never evicts a resident qubit.

**RQ5 — How sensitive are outcomes to architectural assumptions?**

The architecture matrix runs four core policies (program-order LRU, DAG-LRU, DAG-farthest-next-use, and joint prefetch W=2) while varying CR slots (2, 4, 8), SAM bank count (1, 2, 4), and Line-SAM local access overhead (0, 1, 2 beats). Total SAM data capacity is fixed at 144 cells across bank counts, using near-square per-bank geometries. A separate policy-ablation matrix runs all 13 variants at CR=4, two banks, and one-beat local overhead. A separate two-qubit gate-duration sweep runs the four core policies with `t_2q` (1, 2, 4 beats) at the same reference architecture.

## Workloads

| Workload family | Inputs | Role |
|---|---|---|
| QASMBench arithmetic | adders 28/118 qubits; multipliers 15/45 qubits | Primary vendor-supplied application circuits |
| Synthetic arithmetic | Cuccaro-style adders with 2 and 8 bits | Small correctness and size-scaling checks |
| Synthetic topology | 12-qubit controlled-interaction topology | Stress test only; not a functionally equivalent QFT |
| Seeded random | 24 qubits, 180 operations, seeds 11/17/23 | Stress tests only; seeds are not independent application benchmarks |

The QASMBench QASM files, upstream gate-count/depth metadata, license, and notice are bundled in `benchmarks/`. Exact source hashes and imported circuit statistics are emitted in `experiment_manifest.json`.

## Controlled factors and grouping

Within each `comparison_group`, every policy in that axis-specific policy matrix sees the same:
- circuit and imported operation sequence/DAG;
- CR capacity and SAM bank geometry;
- bank ports and shared transfer lanes;
- Line-SAM local overhead, scan-row initial state, and row movement parameter;
- gate timings, code-distance metadata, MSF factory count, cadence, and buffer.

The bank-count experiment preserves **total SAM data capacity**, rather than holding per-bank capacity fixed. This avoids confounding extra banks with extra aggregate storage. Per-bank geometry changes with bank count and is included in every CSV row.

The policy-ablation and two-qubit timing axes use different policy sets and reference configurations; their rows are never pooled with the architecture-sensitivity axis.

## Metrics

Primary outcome: `cycles`, the makespan in abstract code beats.

Secondary outcomes:
- `loads`, `stores`, and `transfers`;
- `total_transfer_cycles`, the sum of transfer durations (overlaps counted separately);
- `exec_busy_cycles`, the union of gate execution intervals;
- `non_exec_cycles`, makespan minus execution-busy union;
- `msf_stall_cycles`, estimated union time during which an otherwise executable ready T operation has no buffered magic state;
- abstract logical-cell count and memory-cell fraction.

Transfer count alone is not an execution-time metric. `non_exec_cycles` includes memory, dependency, and resource waiting; it must not be called pure memory stall time.

## Aggregation and statistical limits

For each configuration, speedup is paired against the program-order LRU baseline:

`speedup = cycles(LRU) / cycles(candidate)`.

Values greater than one indicate lower makespan. The analyzer reports:
1. per-workload geometric-mean speedup across that workload's configuration points;
2. configuration-weighted aggregate geometric means;
3. equal-workload aggregate geometric means, giving each workload equal weight after reducing its configurations;
4. configuration win/loss counts and workload-class summaries.

Configuration points within a single circuit are correlated sensitivity settings, not independent samples. The three random seeds are workload robustness checks, not a sufficient population for inferential statistics. No p-values or population-level claims are reported.

## Reproducibility artifacts

A successful CI run uploads:
- `results.csv`: one row per workload/configuration/policy run (1,330 paired rows plus applicable all-CR lower-bound rows);
- `experiment_manifest.json`: runtime, source revision, policy/configuration matrix, workload metadata, and SHA-256 hashes;
- `workload_summary.csv`, `workload_geomeans.csv`, `policy_summary.csv`, `workload_class_summary.csv`;
- `summary.md`: generated result tables and limitations;
- `traces/*.jsonl.gz`: raw event streams for selected arithmetic workloads and representative policy ablations.

The analysis script rejects missing/extra experiment groups, incomplete policy matrices, mismatched hardware parameters within a comparison group, and source/code hash mismatches.

## Interpretation limits

This study evaluates a discrete-event scheduling abstraction. It is **not** a physical surface-code simulator or a complete reproduction of LSQCA:
- Line-SAM access is row-distance plus a configurable local overhead; patch routing, port geometry, lattice-surgery operations, decoding, and routing conflicts are omitted.
- Point-SAM is not implemented.
- CNOT timing is a sensitivity assumption; the paper's optimized operation has variable latency.
- T is a macro with an explicit simplified cost. PM/SK variable latency and detailed magic-state factory internals are not modeled.
- The abstract logical-cell accounting is not a physical-qubit count or fabrication area.
- The QFT topology and random circuits are synthetic stress workloads, not validated quantum application circuits.
