# Model definition, assumptions, and validation boundary

## Research question

How do operation ordering, CR residency decisions, and transfer arbitration
interact in a load/store architecture with a small computation region and
banked scan-line memory?

The implementation is a discrete-event scheduling model. It does not model
physical syndrome extraction, lattice-surgery stabilizers, decoding, patch
routing, or a full surface-code circuit.

## Time units and gate costs

Time is measured in abstract code beats. The default primitive costs follow the
logical-operation table in LSQCA:
- H: 3 beats.
- S/Sdg: 2 beats.
- Pauli X/Y/Z and single-qubit measurement: 0 beats in the paper's abstraction.
- Two-qubit Pauli measurement: 1 beat.
- T/Tdg macro: 3 beats, representing a 1-beat MZZ measurement plus 2-beat
  conditional S correction.

The CNOT operation in the optimized instruction set has compilation-dependent
latency. QubitFlow exposes `t_2q` as an experiment parameter; its default is
not claimed to be a universal LSQCA timing.

## Line-SAM transfer abstraction

Each bank has one scan line. A cell index maps to row
`row = cell // bank_width`. The scan line starts at the configured row
(default: middle row). A transfer to a target row costs

`T_transfer = line_move_per_row * abs(target_row - current_row)`.

The default is one beat per row, so the scan-line movement term equals the
vertical distance H. Same-row access has zero scan-line movement cost and is
represented as a zero-duration transfer event. The scan-line position updates
when the transfer completes.

This captures only the Line-SAM row-travel term. It does not claim to model
port patch movement, horizontal geometry, lattice-surgery operations, or
multi-scan-line hardware. Point-SAM is not implemented. The legacy
`sam_model="depth"` option is retained only for comparison with the original
placeholder and is not paper-calibrated.

## Banks and transfer resources

- Every SAM bank has one serialized access port.
- Different banks may transfer concurrently.
- By default there is one transfer lane per bank, so the shared interconnect
  does not artificially serialize independent banks.
- Setting `bus_lanes` below the bank count is an explicit shared-interconnect
  sensitivity experiment.
- Load reserves a CR slot at start and releases its SAM source cell at
  completion. Store reserves a SAM destination at start and releases the CR
  slot at completion.

The shared-lane model is an abstraction; physical route topology and
cross-bank data movement are not modeled.

## Magic-state factories

The aggregate factory model produces up to `magic_factories` states every
`magic_interval` beats. The default interval is 15 beats per factory and the
default buffer is `2 * magic_factories`. Production is capped at free buffer
capacity; excess output is discarded. T/Tdg consumes one state at issue.

This is a throughput/buffer abstraction. It does not model distillation
circuits, probabilistic failures, injection ports, factory placement, or
routing from factory to CR.

## Circuit import and workload provenance

The bundled QASMBench inputs are transpiled OpenQASM 2 files. The standard
library parser supports only the subset used by those files: register
declarations, barriers, terminal measurements, CX, Clifford/Pauli gates,
SX/SXdg, T/Tdg, and exact pi/4-grid RZ rotations. SX is expanded to H-S-H;
RZ is mapped exactly to Clifford+T up to global phase. Off-grid rotations,
classical control, resets, and unknown gates are rejected.

The adder10, bigadder18, and multiplier15 files are from PNNL QASMBench.
Their source paths and redistribution license/notice are retained under
`benchmarks/`. They are useful real circuit inputs, but the model does not
simulate quantum-state semantics.

## Scheduling policies

- Fixed-order policies issue the lowest-ID dependency-ready operation only.
- LRU evicts the least recently used idle CR qubit.
- Belady is an offline farthest-next-use baseline over each qubit's remaining
  ordered use chain.
- DAG policies select among dependency-ready operations using resident
  operand count, static unit-weight critical-path length, and deterministic
  operation-ID tie-breaking.
- Joint policy combines DAG issue, demand loads, farthest-next-use CR eviction,
  and optional prefetch when free CR slots exist.

The joint policy is a deterministic heuristic. It is not optimal. Its critical
path uses unit gate weights; replace it with duration-weighted costs only after
the architecture timing model is fixed. Prefetch is deliberately conservative
and does not evict qubits to make speculative room.

## Validation

The trace validator independently checks:
- every circuit operation appears exactly once with matching operands/duration;
- dependency order and per-qubit non-overlap;
- valid initial placement and legal CR/SAM state transitions;
- CR-slot and SAM-cell occupancy;
- per-bank ports, bus lanes, and maximum parallel operations;
- Line-SAM row transitions and transfer durations;
- magic-state consumption, production cadence, and buffer capacity.

Zero-duration logical operations and same-row transfers are legal. The event
engine completes zero-duration events at the current simulation timestamp
before progressing time.

## Metrics

- `cycles`: simulated makespan.
- `exec_busy_cycles`: union of gate-execution intervals.
- `non_exec_cycles`: makespan minus execution-busy union; not synonymous with
  memory wait.
- `msf_stall_cycles`: estimated interval where a T operation is ready,
  operands are resident and idle, a parallel-op slot is available, and stock
  is empty.
- `logical_cells`: SAM data cells + scan-line auxiliary cells + CR cells.
- `memory_density`: SAM data cells divided by the above logical-cell total.
  These are architectural cell-accounting metrics, not physical-qubit counts.
- `total_transfer_cycles`: sum of transfer durations, not transfer makespan.

## Fair experiment protocol

- Compare policies only at identical CR capacity, bank count/capacity,
  bank width, lane count, operation parallelism, gate timings, and factory
  settings.
- Report the full configuration with every result.
- The default sweep varies CR slots and bank count, and therefore is an
  architecture sensitivity experiment.
- Use multiple structurally distinct circuits; do not treat synthetic random
  workloads or the topology-only QFT generator as application results.
- Makespan is the primary metric. Transfer count alone is not a performance
  metric.
- Report the baseline and relative speedup per workload/configuration; do not
  pool raw makespans across different circuits.

## Explicitly out of scope / future work

1. A physically detailed Point-SAM model and comparison with Line-SAM.
2. Patch-routing and lattice-surgery port geometry.
3. Duration-weighted scheduling and DAG-aware earliest-use/liveness estimates.
4. Exact operation-specific optimized CNOT compilation latency.
5. A distillation/injection-network model beyond aggregate factory throughput.
6. Physical-qubit/area estimation calibrated to the complete LSQCA floorplan.
7. Functional quantum-state equivalence checking of imported circuits.
