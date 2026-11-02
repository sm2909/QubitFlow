# QubitFlow

QubitFlow is a discrete-event simulator for studying operation scheduling,
logical-qubit residency, and load/store contention in a simplified LSQCA-style
fault-tolerant quantum architecture.

## What is modeled

- A small computation region (CR) and banked scan-line storage area (SAM).
- One SAM access port per bank; independent banks can transfer concurrently.
- Configurable shared transfer lanes; default is one lane per bank.
- Dependency-constrained gate issue and bounded operation parallelism.
- Finite magic-state buffering with configurable factory count and cadence.
- Program-order demand/prefetch baselines and a DAG-ready heuristic scheduler.
- Independent trace validation, bundled QASMBench circuits, and CSV experiments.

**Scope:** QubitFlow is an architectural scheduling model, not a surface-code
simulator and not a complete reproduction of LSQCA. The default Line-SAM
latency is the row-distance abstraction (H) (one beat per row by default).
It omits physical patch transport, routing congestion, syndrome extraction,
decoding, and factory layout. The optimized CNOT duration remains an explicit
model parameter because its implementation latency depends on the compiled
logical operation. Do not interpret model outputs as hardware predictions.

## Quick start

Python 3.10+; the core simulator, tests, and QASM importer use only the standard
library.

```bash
python -m unittest discover -s tests -v
python smoke.py
python run.py
```

The strict OpenQASM 2 importer supports the bundled QASMBench Clifford+T
inputs. It rejects off-grid rotations and unsupported statements rather than
silently assigning them an approximate cost.

## Scheduling policies

| Policy | Behavior |
|---|---|
| `lsqca_lazy` | Program-order issue; demand loads; LRU eviction |
| `fixed_prefetch(W)` | Program-order issue; bounded lookahead; LRU eviction |
| `fixed_belady(W)` | Program-order issue; bounded lookahead; offline farthest-next-use baseline |
| `dag_no_prefetch` | DAG-ready issue; demand loads; no speculative prefetch |
| `joint` | DAG-ready issue, CR residency management, demand loads, optional prefetch |

Belady is an offline comparison, not an implementable online policy. The joint
scheduler is a heuristic, not an optimizer or a claim of optimal scheduling.
Its next-use estimate is based on each qubit's remaining operation sequence;
critical-path priority currently uses unit operation weights.

## Experiments

`run.py` evaluates bundled QASMBench adder10, bigadder18, and multiplier15
circuits. It sweeps CR capacity and SAM bank count, and compares all policies
under identical hardware settings within each configuration.

```bash
python run.py
```

The generated `results.csv` contains raw makespan, loads/stores, transfer
cycles, operation busy cycles, non-execution cycles, MSF-blocked estimate,
logical-cell accounting, density ratio, circuit statistics, and full
architecture/policy parameters. The sweep changes architecture capacity, so
interpret it as an architecture-sensitivity study, not as a pure policy
comparison across rows.

## Metrics and limitations

- `cycles`: simulated makespan in abstract code beats.
- `exec_busy_cycles`: union of gate-execution intervals.
- `non_exec_cycles`: makespan minus execution-busy union; includes memory,
  dependency, resource, and magic-state waits.
- `msf_stall_cycles`: estimated interval where a ready T gate has resident,
  idle operands, a free operation slot, and no magic state.
- `logical_cells` and `memory_density`: explicit architectural cell
  accounting, not physical-qubit estimates.
- `total_transfer_cycles`: sum of transfer durations; overlapping transfers
  count separately.

All benchmark results are conditional on the abstract timing model. The
repository intentionally does not report physical-qubit counts or claim
paper-level physical calibration.

## Benchmark provenance

Bundled QASMBench inputs and their license/notice are in `benchmarks/`.
See `docs/DESIGN.md` for model assumptions, equations, and the remaining
validation boundary.
