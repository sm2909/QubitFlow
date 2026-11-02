# QubitFlow

QubitFlow is a small, dependency-light discrete-event simulator for exploring scheduling in load/store fault-tolerant quantum-computing architectures.

The current prototype models:
- A limited-capacity computation region (CR) and banked storage area (SAM).
- Logical-qubit load/store transfers with per-bank ports and shared bus lanes.
- Dependency-constrained gate execution and bounded parallelism.
- A finite magic-state buffer and periodic factory production.
- Fixed-order memory-management baselines and a DAG-aware joint scheduling heuristic.
- An independent trace validator and CSV experiment harness.

**Research boundary:** this is an architectural scheduling abstraction, not a physical surface-code simulator and not a faithful reproduction of LSQCA. Timing and area defaults are placeholders. In particular, the current Line-SAM model is a configurable scan-row abstraction whose parameters still require calibration against the paper.

## Quick start

Python 3.10+.

```bash
python smoke.py
python -m unittest discover -s tests -v
python run.py
```

No third-party packages are needed for the core simulator or tests. Qiskit/QASM and MQT Bench loaders require their optional dependencies and are not exercised by the core test suite.

## Policies

| Policy | Behavior |
|---|---|
| `lsqca_lazy` | Program-order issue, demand loads, LRU eviction |
| `fixed_prefetch(W)` | Program-order issue, bounded lookahead prefetch, LRU |
| `fixed_belady(W)` | Program-order issue, bounded lookahead prefetch, farthest-next-use eviction |
| `dag_no_prefetch` | DAG-ready operation selection, demand loads, no speculative prefetch |
| `joint` | DAG-ready operation selection, residency management, conservative prefetch |

The Belady policy is an offline oracle-like baseline under its fixed-order assumptions, not a practical online policy. The joint policy is a deterministic heuristic, not an optimizer.

## Experiments

Edit `EXPERIMENT` in `run.py` or pass a JSON experiment file:

```bash
python run.py --json experiment.json
```

The harness records cycles, transfers, transfer time, execution busy time, memory wait, MSF stall estimate, architecture parameters, policy parameters, and workload metadata in CSV.

For policy comparisons, hold the architecture constant. The default sweep varies CR capacity and bank count; because this also changes hardware resources, report it as an architecture sensitivity study rather than a pure policy comparison.

## Project layout

- `qf_core.py`: architecture config, circuit DAG, simulator, trace validator.
- `qf_policies.py`: baseline and joint scheduling policies.
- `run.py`: reproducible experiment harness.
- `smoke.py`: small end-to-end validation run.
- `tests/`: correctness and regression tests.
- `docs/DESIGN.md`: model assumptions, limitations, and calibration checklist.

## Citation / architectural background

The architecture is motivated by LSQCA (Kobori et al., HPCA 2025) and virtualized logical qubits (Duckering et al., MICRO 2020). Consult the original papers before attributing exact physical timings, layout behavior, or implementation details to this prototype.
