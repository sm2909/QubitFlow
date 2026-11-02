# QubitFlow

QubitFlow is a discrete-event simulator for studying operation scheduling and logical-qubit residency in a load/store fault-tolerant quantum-computing architecture inspired by LSQCA.

## What is modeled

- A limited-capacity computation region (CR) and banked storage area (SAM).
- Per-bank transfer ports and a configurable number of shared transfer lanes.
- A stateful scan-row Line-SAM abstraction whose transfer cost is row distance.
- Dependency-constrained Clifford+T circuit execution.
- Bounded gate parallelism and finite magic-state buffers/factory throughput.
- Program-order LRU, fixed-order Belady, DAG-aware scheduling, and conservative prefetch.
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

The experiment suite writes `results.csv`. `analyze_results.py` produces per-workload and aggregate comparisons against program-order LRU. GitHub Actions runs tests, smoke, experiments, and analysis, then uploads all three CSV artifacts.

## Circuit import

The OpenQASM 2 importer supports a deliberately restricted subset: quantum/classical register declarations, barriers, terminal measurements, CX, CCX, Clifford/Pauli gates, SX/SXdg, T/Tdg, and exact pi/4-grid RZ rotations. CCX is expanded to a standard Clifford+T decomposition. Off-grid rotations, custom gate definitions, resets, and classically conditioned operations are rejected.

Two QASMBench arithmetic circuits are included under `benchmarks/`:
- 28-qubit ripple-carry adder.
- 15-qubit multiplier.

Their upstream license and notice are included alongside the files. See `benchmarks/QASMBENCH_LICENSE` and `benchmarks/QASMBENCH_NOTICE`.

The synthetic `qft_topology` generator is only an interaction-topology workload: it does not encode controlled-phase angles or a gate-equivalent QFT decomposition. It must not be described as a functionally exact QFT benchmark.

## Scheduling policies

| Policy | Meaning |
|---|---|
| `lsqca_lazy` | Program-order issue, demand loads, LRU eviction |
| `fixed_prefetch(W)` | Program-order issue, bounded lookahead prefetch, LRU |
| `fixed_belady(W)` | Program-order issue, offline farthest-next-use eviction |
| `dag_no_prefetch` | DAG-ready operation selection, demand loads, no prefetch |
| `joint` | DAG-ready issue, weighted critical-path priority, DAG-aware eviction and conservative prefetch |

Belady is an offline oracle for fixed-order issue, not a deployable online policy. The joint policy is a heuristic, not an optimizer or proof of optimality.

## Experiments

`run.py` runs the same policy set across a fixed-capacity architecture sensitivity matrix:
- CR slots: 2, 4, 8.
- SAM banks: 1, 2, 4.
- SAM capacity per bank: 32 cells.
- Local Line-SAM access overhead: 0, 1, 2 beats (0 is movement-only lower bound).
- One transfer lane per bank by default.

Within each workload/configuration group, policy comparisons hold hardware parameters fixed. Changing bank count is an architecture sensitivity experiment and changes total SAM capacity. Results include workload provenance, full architecture parameters, policy parameters, Python version, makespan, transfer counts, non-compute cycles, MSF-blocked cycles, logical-cell count, and logical memory-cell fraction.

For a policy-only comparison, select one fixed configuration (for example CR=4, banks=2) from the CSV. Do not compare rows with different architecture settings as if only the policy changed.

## Metrics and limitations

- `cycles`: simulated makespan in abstract code beats.
- `exec_busy_cycles`: union of gate-execution intervals.
- `non_exec_cycles`: makespan minus execution-busy union; it includes memory, dependency, and resource waiting, not only memory stalls.
- `total_transfer_cycles`: sum of individual transfer durations; overlapping transfers are counted separately.
- `msf_stall_cycles`: union-of-time estimate when at least one ready T gate is otherwise executable but no magic state is available.
- `logical_cells` and `memory_density`: abstract architectural cell accounting, not physical-qubit counts or a fabrication-area estimate.

## References

- Kobori et al., “LSQCA: Resource-Efficient Load/Store Architecture for Limited-Scale Fault-Tolerant Quantum Computing,” HPCA 2025.
- Duckering et al., “Virtualized Logical Qubits: A 2.5D Architecture for Error-Corrected Quantum Computing,” MICRO 2020.
- QASMBench, PNNL: https://github.com/pnnl/QASMBench
