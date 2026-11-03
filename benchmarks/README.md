# Bundled QASMBench workloads

These transpiled OpenQASM 2 inputs are from the PNNL QASMBench repository
(Battelle Memorial Institute, 2020), redistributed under the license reproduced
in `QASMBENCH_LICENSE`; see `QASMBENCH_NOTICE`.

| Bundled file | Upstream workload | Qubits | Upstream gate count | Upstream depth |
|---|---|---:|---:|---:|
| `qasmb_adder_n28.qasm` | `large/adder_n28` | 28 | 856 | 42 |
| `qasmb_multiplier_n15.qasm` | `medium/multiplier_n15` | 15 | 1,222 | 49 |
| `qasmb_adder_n118.qasm` | `large/adder_n118` | 118 | 3,706 | 132 |
| `qasmb_multiplier_n45.qasm` | `large/multiplier_n45` | 45 | 12,785 | 462 |

Upstream source revision: `357b942396d5c2b7cbc1c229c585a6ef5ccaebac` (immutable Git commit).

Pinned upstream source paths:
- [adder_n28](https://github.com/pnnl/QASMBench/blob/357b942396d5c2b7cbc1c229c585a6ef5ccaebac/large/adder_n28/adder_n28_transpiled.qasm)
- [multiplier_n15](https://github.com/pnnl/QASMBench/blob/357b942396d5c2b7cbc1c229c585a6ef5ccaebac/medium/multiplier_n15/multiplier_n15_transpiled.qasm)
- [adder_n118](https://github.com/pnnl/QASMBench/blob/357b942396d5c2b7cbc1c229c585a6ef5ccaebac/large/adder_n118/adder_n118_transpiled.qasm)
- [multiplier_n45](https://github.com/pnnl/QASMBench/blob/357b942396d5c2b7cbc1c229c585a6ef5ccaebac/large/multiplier_n45/multiplier_n45_transpiled.qasm)

The upstream gate counts and depths are source metadata, not the instruction
counts and makespans produced by QubitFlow. The exact bundled source files are
hash-recorded in `experiment_manifest.json`.

The restricted importer expands CCX into a standard Clifford+T decomposition,
SX into H-S-H, and exact pi/4-grid RZ rotations into Clifford+T (up to global
phase). The bundled transpiled arithmetic circuits use this supported subset.
QFT circuits with arbitrary off-grid phase rotations are intentionally not
included as QASMBench application results; `synthetic_qft_topology_n12` is
only a synthetic controlled-interaction topology stress test.

The simulator does not evolve quantum states. Gate counts, depth and circuit
semantics should be validated independently of its scheduling makespan.
