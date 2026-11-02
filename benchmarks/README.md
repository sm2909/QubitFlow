# Bundled QASMBench workloads

These transpiled OpenQASM 2 benchmark inputs are from the PNNL QASMBench
repository (Battelle Memorial Institute, 2020), redistributed under the
license reproduced in `QASMBENCH_LICENSE`; see `QASMBENCH_NOTICE`.

Sources:
- `qasmb_adder_n28.qasm`: `large/adder_n28/adder_n28_transpiled.qasm`
- `qasmb_multiplier_n15.qasm`: `medium/multiplier_n15/multiplier_n15_transpiled.qasm`

The importer expands CCX into the standard Clifford+T decomposition, SX into
H-S-H, and maps exact pi/4-grid RZ rotations to Clifford+T. Circuit metrics
are scheduling-model metrics, not quantum-state simulation results.
