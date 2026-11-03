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

- The `lsqca_lazy` label denotes QubitFlow's simple program-order demand-paging LRU reference. It is not a full reproduction of the LSQCA paper's complete compiler/runtime policy.
- Fixed-order LRU and fixed-order farthest-next-use use program-order issue.
- Farthest-next-use is an offline Belady-inspired replacement baseline for a fixed sequence. With nonuniform transfer costs and bank contention it is not an optimal replacement oracle.
- DAG-LRU changes gate ordering while keeping LRU replacement; DAG-farthest-next-use changes replacement while using the same DAG-ready issue heuristic. Its next-use urgency is estimated from cached static duration-weighted topological depth, with critical-path urgency as a tie-break; this is a heuristic proxy, not a dynamic schedule-time prediction.
- The nearest-transfer variant changes only demand-transfer ordering from critical-path priority to shortest current scan-row travel.
- Joint scheduling combines DAG-ready operation selection, farthest-next-use residency, critical-path demand-transfer ordering, and dependency-frontier prefetch.

The joint policy is evaluated at prefetch widths W=1/2/4. W is a persistent maximum number of speculative qubits outstanding (in transfer or resident): the slot is released after a consumer starts or the prefetched qubit is evicted. The nearest-transfer joint variant changes only arbitration. These are deterministic heuristics, not optimal schedulers.

## Metrics

- `cycles`: makespan in abstract code beats.
- `exec_busy_cycles`: union of intervals in which at least one gate executes.
- `non_exec_cycles`: makespan minus execution-busy union. It includes memory, dependency, and resource waiting.
- `total_transfer_cycles`: sum of individual transfer durations; overlapping transfers are counted separately.
- `msf_stall_cycles`: union-of-time estimate when a ready T operation has its operands resident, a gate slot available, and no magic state. It is not the sum of per-operation waiting times.
- `logical_cells`: abstract SAM data cells, scan-line auxiliary cells, and CR cells.
- `memory_density`: SAM data cells divided by abstract logical-cell count. It is not a physical-qubit count, tile-area estimate, or fabrication-density claim.

## Fair comparisons

The experiment protocol uses three matched axes over 10 workloads: (1) four core policies across CR slots 2/4/8, bank counts 1/2/4 at a fixed aggregate capacity of 144 SAM data cells, and local Line-SAM overhead 0/1/2 beats; (2) all 13 policy variants at one fixed reference architecture (CR=4, two banks, access overhead=1) to isolate lookahead/replacement/arbitration/prefetch effects; (3) the four core policies across CNOT durations t_2q=1/2/4 beats at the reference architecture. Per-bank geometry is kept near-square while aggregate data-cell capacity remains constant. The 10 workloads are four QASMBench arithmetic circuits, two synthetic adders, one topology-only QFT stress test, and three random stress seeds. Policy speedups are computed only within identical workload/configuration groups; the analyzer verifies the complete expected matrix and recorded file hashes.

- Policy comparisons must hold CR size, bank count/capacity, bank width, bus lanes, code distance, gate timings, factory count/rate/buffer, and workload fixed.
- Architecture sensitivity varies one or more explicit hardware parameters and must be reported separately.
- Include multiple workload families and random seeds.
- Report makespan first; transfer count alone is not a performance metric.
- Do not infer physical resource savings from the abstract logical-cell fraction.

## Benchmark provenance

The included 15- and 45-qubit multipliers and 28- and 118-qubit adders are from PNNL QASMBench. Their transpiled OpenQASM 2 files are retained under `benchmarks/`; the upstream license and notice are included in the same directory. The importer expands CCX using a standard Clifford+T decomposition, SX into H-S-H, and exact pi/4-grid RZ rotations to Clifford+T. It rejects off-grid rotations and unsupported dynamic control flow. The Toffoli decomposition and two-bit Cuccaro adder are checked on every computational-basis input by a small exact statevector test; this is a targeted semantic regression, not a complete verifier for the vendor benchmarks.
