# Timing assumptions, sources, and implementation status

Reference: T. Kobori et al., *LSQCA: Resource-Efficient Load/Store Architecture for Limited-Scale
Fault-Tolerant Quantum Computing*, HPCA 2025, arXiv:2412.20486 (cited below as **[LSQCA]**).
Time unit = one code beat (d code cycles); space unit = one surface-code tile.

## 1. Provenance of every constant

| Quantity | Value | Source |
|---|---|---|
| Lattice-surgery step | 1 beat | [LSQCA] Sec. II-C |
| H gate / S gate | 3 / 2 beats | [LSQCA] Sec. II-C, Table I (`Timing.t_h`, `t_s` in `lsqca_paper`) |
| Line-SAM load latency | = row distance H of the target to the scan line | [LSQCA] Sec. IV-C.3 (text) |
| Line-SAM worst case | 0.5·sqrt(n) (scan starts mid-bank) | [LSQCA] Sec. IV-C.3; `initial_scan_row = rows//2` |
| Same-row access | no scan-line movement | [LSQCA] Sec. IV-C.3 |
| Locality-aware store (line) | store to the row facing the scan line | [LSQCA] Sec. V-B (modelled as "nearest free row to the scan line") |
| Magic-state factory | 1 state / 15 beats | [LSQCA] Sec. III-B/VI-A (Litinski MSF) -> `magic_interval=15` |
| Magic buffer | 2n for n factories | [LSQCA] Sec. VI-A -> `magic_buffer=2` |
| Square-ish banks | L×L or L×(L+1) | [LSQCA] Sec. VI-A -> `experiments.make_arch` |
| T gate | 3 beats (ZZ measurement 1 + conditional S 2) | derived from [LSQCA] Sec. VI-A ("take the longer path") |
| **Final port move into/out of CR** | 1 beat (`Timing.port_move`) | **OUR ASSUMPTION** — the move primitives are in [LSQCA] Fig. 4/11, which could not be machine-read; verify against the figure |
| **Scan-line shift per row** | 1 beat (`Timing.line_shift`) | reading of "latency equals the y-axis distance"; **verify against Fig. 11d** |
| CX gate | 2 beats | **OUR ASSUMPTION** (two lattice-surgery measurements); paper uses an optimised single CNOT instruction |
| Pauli / other 1q gates | 1 beat | **OUR ASSUMPTION** (paper treats them as ~0; the simulator requires >= 1) |
| Area model (`cr_ancilla_ratio`, `bus_tiles_per_lane`, `factory_tiles`) | 1.0, 8, 40 | **PLACEHOLDERS** (paper cites 176 cells for a 15-beat MSF). Physical-qubit numbers are therefore only comparable *across* configurations |

`Timing()` defaults (t_2q=2, t_T=2, magic_interval=4, ...) are the original placeholders, kept for
backward compatibility. **All experiments use `Timing.lsqca_paper()`.**

## 2. What the validator checks about Line-SAM costs

`validate_schedule` replays the scan line of each bank from the trace and checks that every transfer
takes exactly `|row - scan_row| * line_shift + port_move`. This verifies *internal consistency with the
model*, and `tests/test_line_sam.py` checks the model against the paper's textual statements
(P1–P4 in that file). It cannot verify the two assumed constants above.

## 3. Feature status

| Feature | Status |
|---|---|
| Line-SAM, multi-bank, one scan line + one port per bank | **Implemented** |
| Locality-aware store (line) | **Approximated** (nearest free row to the scan line; the paper physically realigns empty cells) |
| Generic depth-based SAM (`sam_model="generic"`) | Implemented, **legacy placeholder**, not a faithful SAM |
| Point-SAM | **Not supported** (raises `NotImplementedError`) |
| In-memory operations (HD.M, PH.M, in-memory CX/MZZ) | **Not supported** — every op executes in CR after a load |
| Hybrid floorplan (conventional region + SAM) | Not supported |
| "Store immediately" CR policy of the paper (CR = 2 cells) | Not the default; CR holds qubits lazily. CR size is a swept parameter |
| Two-ported / double-empty-cell load speedups | Not modelled |
| Qubit-to-bank assignment | Fixed round-robin at start (as in the paper's baseline); stores may relocate qubits |
| Magic-state factory | Rate + buffer model; no distillation failures, no factory placement/routing latency |
| Lattice-surgery routing conflicts inside CR | Abstracted by `max_parallel_ops` |
| Conventional-floorplan baseline of the paper | Not implemented here (comparison is between schedulers, not vs. conventional floorplans) |

## 4. Current-model limitations to keep in mind when reading results

* `lsqca_lazy` / `demand_lru` is **our reading** of an on-demand policy, not the paper's exact instruction stream.
* The importer's `rz` handling is a *cost model*, not a decomposition (see §5).
* Transfers hold one bus lane + one bank port for their whole duration; no pipelining of the scan-line
  motion with the final port move.
* In-flight write-backs at the end of a run are drained but do not count toward `cycles`
  (cycles = finish time of the last operation).

## 5. Qiskit importer (`qf_core.from_qiskit`, `from_qasm`, `from_mqt_bench`)

1. Circuit is transpiled to `{cx,h,s,sdg,t,tdg,x,z,rz,measure}` with `optimization_level=0`; `ccx` and other
   multi-qubit gates are unrolled by Qiskit's own decompositions (no re-optimisation of T-count).
2. **Rotation gates are approximated** (`rz_to_gates`): multiples of π/2 -> one `S`-kind op (this also covers
   `Z`-type angles); odd multiples of π/4 -> one `T`; any other angle -> `rz_t_count` (default 4) `T` ops. The
   result is a resource *cost model* with the right order of magnitude of T-count, not a unitary-equivalent
   Clifford+T sequence. Real synthesis needs ~3·log2(1/ε) T gates per rotation; set `rz_t_count` accordingly
   and report it.
3. Barriers/delays and global phase are dropped. Classically-conditioned gates are rejected. `reset`
   and any gate outside the supported set is rejected with a clear error.
4. `swap` is charged as one 2-qubit op (really 3 CX); `cp` should be transpiled first (it is, via the basis).
5. Measurements cost `t_meas` and carry no classical-control dependency edges (use `extra_deps` to add them).
6. Qubit ordering follows Qiskit bit indices.
