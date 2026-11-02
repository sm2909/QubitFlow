# Bundled QASMBench workloads

These transpiled OpenQASM 2 benchmark inputs are from the PNNL QASMBench
repository (Battelle Memorial Institute, 2020), redistributed under the
license reproduced in `QASMBench-LICENSE`; see `QASMBench-NOTICE`.

Sources:
- `adder_n10.qasm`: `small/adder_n10/adder_n10_transpiled.qasm`
- `bigadder_n18.qasm`: `medium/bigadder_n18/bigadder_n18_transpiled.qasm`
- `multiplier_n15.qasm`: `medium/multiplier_n15/multiplier_n15_transpiled.qasm`

The bundled files are already transpiled into the supported gate subset. The
importer expands SX into H-S-H and maps exact pi/4-grid RZ rotations to
Clifford+T. Circuit metrics produced by QubitFlow are scheduling-model metrics,
not functional quantum simulation results.
