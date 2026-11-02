# Design, assumptions, and calibration status

## Scope

QubitFlow studies the interaction between:
1. Operation selection among dependency-ready gates.
2. Qubit residency in a small computation region (CR).
3. Load/store transfer starts competing for bank ports and shared bus lanes.

The simulator is discrete-event and operates at an abstract logical-patch/code-beat level. It does not simulate syndrome extraction, physical data-qubit movement, lattice-surgery stabilizers, routing geometry, or decoding.

## Current architectural state

- Each logical qubit is in CR, in a SAM cell, or in transfer.
- CR slots are reserved at load start and released at store completion.
- SAM source cells are released at load completion; store destinations are reserved at store start.
- Each bank has one serialized access port.
- Transfers also occupy one shared bus lane for their full modeled duration.
- Gate operations require all operands resident and idle in CR.
- Operations obey per-qubit dependency edges and a global maximum parallel-operation count.
- T gates consume one magic state. A finite buffer is replenished periodically by a simplified factory model.

## SAM timing models

`sam_model="depth"` preserves the original stateless placeholder:
`xfer_base + xfer_per_depth * row`.

`sam_model="line"` models a scan line with per-bank current-row state:
`line_access_cost + line_move_per_row * abs(target_row - current_row)`.

The scan row changes when a transfer completes. This is deliberately a simple, configurable abstraction. It is **not yet a calibrated implementation of Point-SAM or Line-SAM from LSQCA**. Do not quote its default timing values as paper results. Before making architecture claims:
- Verify the paper's exact Line-SAM geometry and movement sequence.
- Derive transfer latency from the chosen physical operation and resource assumptions.
- Check whether row position persists independently per bank or is shared by the physical layout.
- Model multiple scan lines / access ports only when supported by the selected architecture.
- Compare predicted access costs against examples or tables in the paper.

## Scheduling policies

The fixed-order policies only start operations in circuit order. Their Belady variant uses next operation ID as next-use distance and is an offline baseline.

The joint heuristic:
- Selects among dependency-ready operations.
- Prefers resident operands and longer static critical paths.
- Loads operands for high-priority ready operations.
- Evicts idle CR qubits with distant next use, protecting selected ready operands.
- Optionally prefetches a small number of operands for future operations when free slots exist.
- Relies on simulator methods for bank and bus arbitration.

This is a first heuristic for controlled experiments, not a claim of global optimality. The static critical-path calculation uses the circuit DAG and unit operation weights; replace with duration-weighted critical paths when the timing model is stable. The current future-use estimate is lowest unscheduled operation ID, which is a heuristic under reordering, not a full DAG-aware liveness metric.

## Magic-state model

The factory starts with a full finite buffer. Once consumption creates room, it produces one state every `magic_interval` beats until full. This is a simplified supply model. It does not model distillation stages, probabilistic failures, injection ports, or spatial factory routing. The validator checks explicit production/consumption events and buffer bounds.

## Metrics

- `cycles`: makespan in abstract beats.
- `exec_busy_cycles`: union of intervals during which at least one gate executes.
- `mem_wait_cycles`: makespan minus execution-busy union; this includes all non-compute time and is not exclusively memory wait.
- `total_transfer_cycles`: sum of individual transfer durations; overlapping transfers are counted separately.
- `msf_stall_cycles`: current simulator estimate; interpret cautiously until validated against a dedicated event-level stall attribution model.
- `physical_qubits`: coarse tile-area estimate using `2d^2-1` per tile, not a complete physical resource accounting.

## Fair comparisons

- For scheduler comparisons, use identical CR size, bank count/capacity, bus lanes, timing, code distance, and MSF configuration.
- For architecture comparisons, state whether physical-qubit budget or storage capacity is held constant.
- Report multiple workloads and seeds; don't infer a policy advantage from the smoke workload.
- Include the complete configuration and policy parameters with every result.
- Compare makespan first; transfer count alone is not a performance metric.

## Immediate calibration / research checklist

1. Re-read LSQCA's Point-SAM and Line-SAM sections and reproduce one hand-worked access-time example.
2. Confirm code-beat gate durations and magic-state production/buffer assumptions from the paper's tables.
3. Determine whether the bus-lane abstraction double-counts or omits movement constraints for the selected layout.
4. Add QASM import tests for exact Clifford+T rotations; off-grid rotations should remain rejected unless explicitly approximated.
5. Replace operation-ID next-use with DAG-aware liveness / earliest-use estimates before claiming the joint scheduler has an optimal replacement policy.
6. Attribute memory, dependency, resource, and MSF stalls separately before drawing bottleneck conclusions.
