# QubitFlow

QubitFlow is a discrete-event simulator for studying operation scheduling and logical-qubit residency in a load/store fault-tolerant quantum-computing architecture inspired by LSQCA.

## What is modeled

- A limited-capacity computation region (CR) and banked storage area (SAM).
- Per-bank transfer ports and a configurable number of shared transfer lanes.
- A stateful scan-row Line-SAM abstraction whose transfer cost is row distance.
- Dependency-constrained Clifford+T circuit execution.
- Bounded gate parallelism and finite magic-state buffers/factory throughput.
- Program-order LRU and farthest-next-use, DAG-LRU, DAG-aware replacement, transfer arbitration, and bounded speculative prefetch.
- Independent schedule validation and reproducible CSV experiments.

**Research boundary:** QubitFlow is not a physical surface-code simulator and is not a complete reproduction of LSQCA. It does not simulate stabilizer extraction, lattice-surgery patches, routing geometry, decoding, or factory distillation internals. The Line-SAM implementation is a row-distance abstraction; do not equate it with a full physical implementation.

## Quick start

Python 3.10+; the simulator, OpenQASM subset importer, tests, and experiment harness use only the standard library.

```bash
python -m unittest discover -s tests -v
python smoke.py
python run.py
python analyze_results.py
```

The experiment suite writes `results.csv`, `experiment_manifest.json`, and representative compressed JSONL event traces in `traces/`. `analyze_results.py` checks matrix completeness and produces matched-configuration, equal-workload, per-workload and workload-class summaries. GitHub Actions uploads the complete evidence package.

## Circuit import

The OpenQASM 2 importer supports a deliberately restricted subset: quantum/classical register declarations, barriers, terminal measurements, CX, CCX, Clifford/Pauli gates, SX/SXdg, T/Tdg, and exact pi/4-grid RZ rotations. CCX is expanded to a standard Clifford+T decomposition. Off-grid rotations, custom gate definitions, resets, and classically conditioned operations are rejected.

Four QASMBench arithmetic workloads are included under `benchmarks/`: 15- and 45-qubit multipliers and 28- and 118-qubit adders. Source provenance, upstream gate counts/depths, and redistribution notices are documented in `benchmarks/README.md`. The upstream license and notice are included alongside the files.

The synthetic `qft_topology` generator is only an interaction-topology workload: it does not encode controlled-phase angles or a gate-equivalent QFT decomposition. It must not be described as a functionally exact QFT benchmark.

## Scheduling policies

| Policy | Meaning |
|---|---|
| `lsqca_lazy` | QubitFlow's program-order demand/LRU reference (not a full LSQCA policy reproduction) |
| `fixed_prefetch(W)` | Program-order issue, bounded lookahead prefetch, LRU |
| `fixed_farthest_next_use` | Program-order issue with offline farthest-next-use replacement |
| `dag_lru` | DAG-ready scheduling with LRU replacement |
| `dag_next_use` | DAG-ready scheduling with farthest-next-use replacement |
| `dag_next_use_nearest` | Same DAG scheduler with nearest-row demand-transfer arbitration |
| `joint-prefetch(W)` | DAG-ready issue, DAG-aware replacement, demand arbitration and bounded in-flight/resident prefetch with per-next-use retry suppression (W=1/2/4) |

Farthest-next-use is an offline Belady-inspired baseline for fixed-order issue, not a deployable online policy or an optimal oracle under this nonuniform transfer-cost model. `lsqca_lazy` is QubitFlow's simple demand-paging reference policy; it is not a full reproduction of every LSQCA compiler/runtime optimization. The joint policy is a heuristic, not an optimizer or proof of optimality.

## Experiments

The research questions, controlled ablations, workload classes, aggregation rules, and interpretation limits are specified in [docs/EXPERIMENTS.md](docs/EXPERIMENTS.md).

`run.py` separates broad ablations from the architecture matrix:
- Four core policies (program-order LRU, DAG-LRU, DAG-farthest-next-use, and joint W=2) run across the full architecture sensitivity matrix.
- All 13 policy variants (lookahead, replacement, transfer arbitration, prefetch windows) run at one fixed reference configuration: CR=4, two banks, local access overhead=1.
- The four core policies run in the separate CNOT-duration sensitivity matrix.
- CR slots: 2, 4, 8.
- SAM banks: 1, 2, 4.
- Total SAM data cells: 144 across every bank-count point (fixed aggregate budget).
- Near-square bank geometries: one 12×12 bank, two 8×9 banks, or four 6×6 banks.
- Local Line-SAM access overhead: 0, 1, 2 beats (0 is movement-only lower bound).
- Separate CNOT-duration sensitivity: 1, 2, 4 beats at CR=4, banks=2, access overhead=1.
- One transfer lane per bank by default.

Within each matched workload/configuration group, policy comparisons hold hardware parameters fixed. The full 13-policy ablation is paired separately at its fixed reference configuration; do not compare its rows directly to architecture-sensitivity rows. Changing bank count redistributes the same 144-cell SAM data budget across a different number of near-square banks; total data capacity is held constant. Results include workload provenance, full architecture parameters, policy parameters, Python version, makespan, transfer counts, non-compute cycles, MSF-blocked cycles, logical-cell count, and logical memory-cell fraction.

For a policy-only comparison, select one fixed configuration (for example CR=4, banks=2) from the CSV. Do not compare rows with different architecture settings as if only the policy changed.

## Metrics and limitations

- `cycles`: simulated makespan in abstract code beats.
- `exec_busy_cycles`: union of gate-execution intervals.
- `non_exec_cycles`: makespan minus execution-busy union; it includes memory, dependency, and resource waiting, not only memory stalls.
- `total_transfer_cycles`: sum of individual transfer durations; overlapping transfers are counted separately.
- `msf_stall_cycles`: union-of-time estimate when at least one ready T gate is otherwise executable but no magic state is available.
- `logical_cells` and `memory_density`: abstract architectural cell accounting, not physical-qubit counts or a fabrication-area estimate.

## References

- Takumi Kobori, Yasunari Suzuki, Yosuke Ueno, Teruo Tanimoto, Synge Todo, and Yuuki Tokunaga. “LSQCA: Resource-Efficient Load/Store Architecture for Limited-Scale Fault-Tolerant Quantum Computing.” HPCA 2025, pp. 304–320. DOI: 10.1109/HPCA61900.2025.00033. Preprint: https://arxiv.org/abs/2412.20486.
- Casey Duckering, Jonathan M. Baker, David I. Schuster, and Frederic T. Chong. “Virtualized Logical Qubits: A 2.5D Architecture for Error-Corrected Quantum Computing.” MICRO 2020, pp. 173–185. DOI: 10.1109/MICRO50266.2020.00026. Preprint: https://arxiv.org/abs/2009.01982.
- Ang Li, Samuel Stein, Sriram Krishnamoorthy, and James Ang. “QASMBench: A Low-Level Quantum Benchmark Suite for NISQ Evaluation and Simulation.” ACM Transactions on Quantum Computing 4(2), 2023. DOI: 10.1145/3550488. Source: https://github.com/pnnl/QASMBench.
