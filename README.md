# QubitFlow

A cycle-level simulator for **load/store fault-tolerant quantum architectures** in the style of LSQCA
(computational register **CR** + scan-access memory **SAM** + magic-state factory) and a testbed for
**memory-aware instruction scheduling** (fixed-order baselines vs a DAG-aware joint scheduler).

> Reference architecture: Kobori et al., *LSQCA*, HPCA 2025 (arXiv:2412.20486). This is an independent
> re-implementation; see `docs/ASSUMPTIONS.md` for what is faithful, approximated, or missing.

## Layout

| File | Purpose |
|---|---|
| `qf_core.py` | Config (`ArchConfig`, `Timing`; validated), circuit DAG + importers, `Simulator`, independent `validate_schedule` |
| `qf_policies.py` | `FixedOrderPolicy` (demand/prefetch × LRU/Belady) and `DagPolicy` (critical-path-driven joint scheduler) |
| `qf_workloads.py` | adder, multiplier, QFT, parallel adders, random (seeded); arithmetic verified classically |
| `experiments.py` | Reproducible study suites -> `results/*.csv` + manifests |
| `analyze.py` | CSV -> `results/SUMMARY.md` + figures |
| `run.py`, `smoke.py` | Ad-hoc runner / quick all-policy sanity check |
| `tests/` | 224 tests (simulator, validator mutation tests, Line-SAM vs paper statements, DAG, workloads, reproducibility) |
| `docs/ASSUMPTIONS.md`, `docs/EXPERIMENTS.md` | Timing sources & feature status; study design, results, threats to validity |

## Model in one paragraph

A logical qubit is in exactly one place: a CR slot, a SAM cell, or in transit. Operations execute only in CR on
resident, idle operands, subject to dependencies, a parallel-op limit and (for T) magic-state inventory. A transfer
holds one bus lane and one bank port; its destination is reserved at issue and its source released at completion.
With `sam_model="line"` each bank has a scan line whose row position is explicit state: a transfer costs
`|row − scan_row|·line_shift + port_move`, so same-row accesses are cheap and stores go to the row nearest the scan line.

## Policies

`demand_lru` (= `lsqca_lazy`), `demand_belady`, `prefetch_lru(W)` (= `fixed_prefetch`), `prefetch_belady(W)` (= `fixed_belady`),
`dag` (`prefetch_window=0` demand-only, `>0` with speculative prefetch). See the `DagPolicy` docstring for the algorithm.
Add a policy: implement `.name`, `.spec`, `.step(sim)` and register it in `POLICIES`.

## Commands

```bash
pip install -r requirements.txt
python -m pytest tests -q                       # all tests (≈15 s)
python smoke.py                                 # every policy on a tiny circuit, validated
python run.py                                   # ad-hoc sweep configured in run.py -> results.csv
python experiments.py --suite all --out results # full study (≈2 min); --quick for tiny workloads
python analyze.py results                       # tables + figures -> results/SUMMARY.md
```

Python ≥ 3.10. The core engine needs no third-party packages; `numpy`/`pandas`/`matplotlib` are needed for tests/analysis and
`qiskit` only for `from_qiskit`/`from_qasm` (tests skip it if absent).

## Reproducibility

Deterministic engine (no hidden randomness); seeded workloads; each experiment run is executed twice and hash-compared;
every schedule is validated; CSVs hold no timestamps; manifests record environment and git commit.
