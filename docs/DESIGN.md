# Model assumptions and validation status

## Scope

QubitFlow evaluates the interaction between:
1. Operation selection among dependency-ready gates.
2. Logical-qubit residency in a small computation region (CR).
3. Load/store transfers competing for per-bank ports and shared transfer lanes.

It is a discrete-event scheduling abstraction. It does not model physical surface-code patches, syndrome extraction, lattice-surgery stabilizer rounds, decoding, routing geometry, or magic-state distillation circuits.

## Timing model

All times are abstract code beats.

The defaults for primitive timings follow the logical-operation categories reported in LSQCA Table I:
- H: 3 beats.
- S/Sdg: 2 beats.
- Pauli X/Y/Z and single-qubit measurement: 0 beats.
- Two-qubit Pauli parity measurement: 1 beat.
- T/Tdg macro: 3 beats (one-beat MZZ measurement plus two-beat conditional S).

The optimized CNOT/unitary implementation has workload-dependent behavior; `t_2q=1` is an explicit experiment parameter, **not a universal paper-derived CNOT latency**. Sensitivity experiments should vary it before drawing claims dependent on two-qubit timing.

A T gate consumes one buffered magic state. The aggregate factory model produces `magic_factories` states every 15 beats; the default buffer capacity is `2 * magic_factories`. The factory starts with a full buffer. The T macro is assigned 3 beats (one-beat MZZ plus a two-beat conditional S path), but this is an explicit simplified assumption: LSQCA's PM (magic-state move to CR) and SK (classical correction) instructions have variable latency. QubitFlow currently models PM availability through the finite stock only (no separate MSF-to-CR transfer latency) and assumes no additional SK delay. Thus T timing is not a complete reproduction of the paper's instruction sequence. The model also omits distillation stages, failure probabilities, injection routing, and spatial placement.

## SAM and transfer model

The default experiment fixes aggregate SAM data capacity at 144 cells and varies bank count while preserving that capacity: one 12×12 bank, two 8×9 banks, or four 6×6 banks. These near-square logical geometries are modelling choices, not physical floorplan reproductions. The default `sam_model="line"` models one scan-row position per bank. A transfer to a cell in row `r` takes:

`line_access_cost + line_move_per_row * abs(r - current_scan_row)`

The scan row is updated when a transfer completes. The default initial row is the boundary row 0. `line_access_cost=0` is a movement-only lower bound; the experiment suite sweeps local overheads of 0, 1, and 2 beats because the port-exchange cost is not calibrated from the paper. Transfers in different banks can overlap; the default transfer-lane count equals the bank count. Setting fewer lanes is an explicit shared-interconnect sensitivity.

This is a **row-distance abstraction**, not a full physical Line-SAM reproduction. It omits local patch transport, port geometry, code-deformation/lattice-surgery details, and any additional movement constraints not represented by the row model. The legacy `depth` model is a synthetic sensitivity option, not an LSQCA Point-SAM model. Point-SAM is not implemented.

Zero-row-distance transfers and Pauli operations may have zero duration. The event loop completes zero-time events immediately before requesting another policy action. Resource intervals are half-open; zero-duration events consume no time.

## Initial placement and CR/SAM state

By default, all logical qubits begin in SAM, distributed round-robin across banks and placed in ascending cell order within each bank. CR slots are reserved at load start and released when stores complete. SAM source cells are released when loads complete; store destinations are reserved at store start.

An optional all-qubits-in-CR initial state is provided as a lower-bound reference when the entire register fits in the CR. It is not a competing memory policy.

## Circuit representation

The circuit DAG preserves per-qubit program order. A two-qubit operation depends on the latest prior operation on each operand. The restricted OpenQASM importer supports a documented Clifford+T subset, exact pi/4-grid RZ rotations, and CCX expansion to a standard Clifford+T decomposition. It rejects off-grid rotations and unsupported control flow rather than silently changing circuit semantics.

The synthetic QFT workload captures a dense controlled-interaction topology only; its controlled-phase angles are abstracted to generic two-qubit operations. It is not a gate-equivalent QFT benchmark and should only be used for topology/scheduler stress testing.

## Scheduling policies

- Fixed-order LRU and fixed-order Belady issue in program order.
- Belady uses exact next operation ID for a fixed sequence and is an offline oracle baseline.
- The joint policy selects among DAG-ready operations using weighted critical-path priority, demand-loads their operands, and evicts based on a DAG-aware estimate of the longest remaining dependency path to the next use.
- Prefetch is conservative: it is disabled while ready operations still need demand operands and never evicts a resident qubit to prefetch.

The joint policy is evaluated at prefetch widths W=1/2/4. The window limit is persistent across scheduler iterations: an in-flight or resident prefetched qubit consumes a window slot until its first consumer starts or it is evicted. It remains a deterministic heuristic. Its DAG-aware next-use estimate is not an optimal register-allocation algorithm.

## Metrics

- `cycles`: makespan in abstract code beats.
- `exec_busy_cycles`: union of intervals in which at least one gate executes.
- `non_exec_cycles`: makespan minus execution-busy union. It includes memory, dependency, and resource waiting.
- `total_transfer_cycles`: sum of individual transfer durations; overlapping transfers are counted separately.
- `msf_stall_cycles`: union-of-time estimate when a ready T operation has its operands resident, a gate slot available, and no magic state. It is not the sum of per-operation waiting times.
- `logical_cells`: abstract SAM data cells, scan-line auxiliary cells, and CR cells.
- `memory_density`: SAM data cells divided by abstract logical-cell count. It is not a physical-qubit count, tile-area estimate, or fabrication-density claim.

## Fair comparisons

The experiment harness evaluates CR slots 2/4/8, bank counts 1/2/4 at a fixed aggregate capacity of 144 SAM data cells, and local Line-SAM access overhead 0/1/2 beats. Per-bank geometry is kept near-square while aggregate data-cell capacity remains constant. A separate CNOT-duration sensitivity sweep evaluates t_2q = 1/2/4 beats at CR=4, two banks, and one-beat local access overhead. Policy speedups are computed only within identical workload/configuration groups.

- Policy comparisons must hold CR size, bank count/capacity, bank width, bus lanes, code distance, gate timings, factory count/rate/buffer, and workload fixed.
- Architecture sensitivity varies one or more explicit hardware parameters and must be reported separately.
- Include multiple workload families and random seeds.
- Report makespan first; transfer count alone is not a performance metric.
- Do not infer physical resource savings from the abstract logical-cell fraction.

## Benchmark provenance

The included 15- and 45-qubit multipliers and 28- and 118-qubit adders are from PNNL QASMBench. Their original QASM files are retained under `benchmarks/`; the upstream license and notice are included in the same directory. The importer expands CCX using a standard Clifford+T decomposition. The decomposition and importer are covered by unit tests.
