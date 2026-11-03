# Bundled QASMBench workloads

These four vendor-transpiled OpenQASM 2 inputs are from the PNNL QASMBench repository
(Battelle Memorial Institute, 2020), redistributed under the license reproduced
in `QASMBENCH_LICENSE`; see `QASMBENCH_NOTICE`. All workloads use the upstream
`*_transpiled.qasm` representation, rather than mixing original CCX circuits
with vendor-transpiled gate sets.

| Bundled file | Upstream workload | Qubits | Original source gates | Original depth | Original two-qubit gates | Imported QubitFlow ops | Representation |
|---|---|---:|---:|---:|
| `qasmb_adder_n28.qasm` | `large/adder_n28` | 28 | 856 | 42 | 195 | 644 | Vendor transpiled |
| `qasmb_multiplier_n15.qasm` | `medium/multiplier_n15` | 15 | 1,222 | 49 | 246 | 651 | Vendor transpiled |
| `qasmb_adder_n118.qasm` | `large/adder_n118` | 118 | 3,706 | 132 | 845 | 2,784 | Vendor transpiled |
| `qasmb_multiplier_n45.qasm` | `large/multiplier_n45` | 45 | 12,785 | 462 | 2,574 | 6,286 | Vendor transpiled |

Upstream source revision: `357b942396d5c2b7cbc1c229c585a6ef5ccaebac` (immutable Git commit).

Pinned upstream source paths:
- [adder_n28](https://github.com/pnnl/QASMBench/blob/357b942396d5c2b7cbc1c229c585a6ef5ccaebac/large/adder_n28/adder_n28_transpiled.qasm)
- [multiplier_n15](https://github.com/pnnl/QASMBench/blob/357b942396d5c2b7cbc1c229c585a6ef5ccaebac/medium/multiplier_n15/multiplier_n15_transpiled.qasm)
- [adder_n118](https://github.com/pnnl/QASMBench/blob/357b942396d5c2b7cbc1c229c585a6ef5ccaebac/large/adder_n118/adder_n118_transpiled.qasm)
- [multiplier_n45](https://github.com/pnnl/QASMBench/blob/357b942396d5c2b7cbc1c229c585a6ef5ccaebac/large/multiplier_n45/multiplier_n45_transpiled.qasm)

The upstream gate counts, two-qubit counts, and depths describe the original
benchmark circuits; they are not counts of the vendor-transpiled input or the
normalized operations imported by QubitFlow. The manifest records, separately,
the exact input-file SHA-256, imported operation count, and imported gate-kind
counts. The imported operation counts above are regression-tested. They include
the deterministic expansion of SX to H-S-H and exact pi/4-grid RZ rotations to
Clifford+T; they are not the original source gate counts. This separation is necessary because transpilation and Clifford+T
normalization change the instruction representation.

The restricted importer expands CCX into a standard Clifford+T decomposition,
SX into H-S-H, and exact pi/4-grid RZ rotations into Clifford+T (up to global
phase). The bundled transpiled arithmetic circuits use this supported subset.
QFT circuits with arbitrary off-grid phase rotations are intentionally not
included as QASMBench application results; `synthetic_qft_topology_n12` is
only a synthetic controlled-interaction topology stress test.

The simulator does not evolve quantum states. Gate counts, depth and circuit
semantics should be validated independently of its scheduling makespan.
