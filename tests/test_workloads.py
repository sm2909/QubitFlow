"""Workload correctness: arithmetic verified by classical simulation, Toffoli verified against
the exact CCX unitary, importer behaviour."""
import functools, itertools, math
import numpy as np
import pytest
import qf_core as core
import qf_workloads as wl


def test_cuccaro_adder_is_functionally_correct_and_restores_inputs():
    for n in (1, 2, 3, 4):
        nq, g, m = wl.adder_rev(n)
        for a, b in itertools.product(range(2 ** n), repeat=2):
            bits = [0] * nq
            for i in range(n):
                bits[m["A"][i]] = (a >> i) & 1; bits[m["B"][i]] = (b >> i) & 1
            o = wl.simulate_reversible(nq, g, bits)
            assert sum(o[m["B"][i]] << i for i in range(n)) + (o[m["z"]] << n) == a + b
            assert [o[x] for x in m["A"]] == [bits[x] for x in m["A"]] and o[m["cin"]] == 0


def test_multiplier_is_functionally_correct_and_uncomputes_ancillas():
    for n in (1, 2, 3):
        nq, g, m = wl.multiplier_rev(n)
        for a, b in itertools.product(range(2 ** n), repeat=2):
            bits = [0] * nq
            for i in range(n):
                bits[m["a"][i]] = (a >> i) & 1; bits[m["b"][i]] = (b >> i) & 1
            o = wl.simulate_reversible(nq, g, bits)
            assert sum(o[m["acc"][i]] << i for i in range(2 * n)) == a * b
            assert all(o[t] == 0 for t in m["tmp"]) and o[m["cin"]] == 0


def test_core_adder_matches_reversible_adder_after_lowering():
    for n in (1, 3, 6):
        c1, c2 = core.adder(n), wl.adder(n)
        assert [(o.name, o.qubits) for o in c1.ops] == [(o.name, o.qubits) for o in c2.ops]


def _unitary(n, gates):
    I2 = np.eye(2)
    H = np.array([[1, 1], [1, -1]]) / math.sqrt(2)
    T = np.diag([1, np.exp(1j * math.pi / 4)]); Td = T.conj().T
    X = np.array([[0, 1], [1, 0]]); P0, P1 = np.diag([1, 0]), np.diag([0, 1])
    def lift(ops):                       # ops: {qubit: 2x2}, qubit 0 = most significant
        return functools.reduce(np.kron, [ops.get(q, I2) for q in range(n)])
    U = np.eye(2 ** n, dtype=complex)
    for g, qs in gates:
        if g == "h": M = lift({qs[0]: H})
        elif g == "t": M = lift({qs[0]: T})
        elif g == "tdg": M = lift({qs[0]: Td})
        elif g == "cx": M = lift({qs[0]: P0}) + lift({qs[0]: P1, qs[1]: X})
        else: raise ValueError(g)
        U = M @ U
    return U


def test_toffoli_decomposition_equals_exact_ccx_unitary():
    U = _unitary(3, core._toffoli(0, 1, 2))
    ccx = np.eye(8); ccx[[6, 7]] = ccx[[7, 6]]
    assert np.allclose(U, ccx)


def test_toffoli_uses_seven_t_gates():
    g = core._toffoli(0, 1, 2)
    assert sum(n in ("t", "tdg") for n, _ in g) == 7


def test_workload_shapes_and_determinism():
    assert wl.adder(4).n_qubits == 10 and wl.multiplier(3).n_qubits == 16
    assert wl.parallel_adders(3, 2).n_qubits == 18
    for spec in [{"kind": "adder", "n": 3}, {"kind": "multiplier", "n": 2}, {"kind": "qft", "n": 5},
                 {"kind": "parallel_adders", "k": 2, "n": 2}, {"kind": "random", "n": 6, "ops": 30, "seed": 1}]:
        assert wl.build_workload(spec).fingerprint() == wl.build_workload(spec).fingerprint()


def test_random_workload_requires_seed():
    with pytest.raises(core.CircuitError, match="seed"):
        wl.build_workload({"kind": "random", "n": 5, "ops": 10})
    with pytest.raises(core.CircuitError, match="unknown workload"):
        wl.build_workload({"kind": "nope"})


def test_parallel_adders_interleaving_changes_order_not_dag():
    a, b = wl.parallel_adders(3, 2, True), wl.parallel_adders(3, 2, False)
    assert len(a.ops) == len(b.ops) and sorted(a.stats().items()) == sorted(b.stats().items())
    assert max(a.level) == max(b.level)


def test_qft_structure():
    c = wl.qft(6)
    assert sum(o.kind == "h" for o in c.ops) == 6
    assert sum(o.kind == "2q" for o in c.ops) == 2 * (6 * 5 // 2)
    assert wl.qft(8, max_k=2).stats()["2q"] < wl.qft(8).stats()["2q"]


# ---------------------------------------------------------------- importer
@pytest.fixture
def qk():
    return pytest.importorskip("qiskit")


def test_rz_mapping_rules():
    assert core.rz_to_gates(math.pi / 2, (0,)) == [("s", (0,))]
    assert core.rz_to_gates(math.pi, (0,)) == [("s", (0,))]
    assert core.rz_to_gates(math.pi / 4, (0,)) == [("t", (0,))]
    assert core.rz_to_gates(-3 * math.pi / 4, (0,)) == [("t", (0,))]
    assert core.rz_to_gates(0.1234, (0,), rz_t_count=6) == [("t", (0,))] * 6


def test_from_qiskit_basic(qk):
    from qiskit import QuantumCircuit
    qc = QuantumCircuit(3, 3)
    qc.h(0); qc.cx(0, 1); qc.ccx(0, 1, 2); qc.t(2); qc.rz(math.pi / 4, 1); qc.rz(0.3, 0)
    qc.barrier(); qc.measure(2, 2)
    c = core.from_qiskit(qc, rz_t_count=3)
    st = c.stats()
    assert st["qubits"] == 3 and st["meas"] == 1
    assert st["T"] >= 7 + 1 + 1 + 3                      # ccx (7 T) + t + rz(pi/4) + 3 for rz(0.3)
    assert all(o.name != "barrier" for o in c.ops)


def test_from_qiskit_rejects_conditional_gates(qk):
    from qiskit import QuantumCircuit
    qc = QuantumCircuit(1, 1)
    qc.measure(0, 0)
    with qc.if_test((qc.clbits[0], 1)):
        qc.x(0)
    with pytest.raises(Exception):
        core.from_qiskit(qc)


def test_qiskit_circuit_schedules_validly(qk):
    from qiskit import QuantumCircuit
    from helpers import cfg, run
    qc = QuantumCircuit(4)
    for i in range(4): qc.h(i)
    qc.ccx(0, 1, 2); qc.cx(2, 3)
    c = core.from_qiskit(qc)
    for pol in ("demand_lru", "dag"):
        run(c, cfg(n_banks=2, bank_capacity=2, bank_width=2, bus_lanes=2, cr_slots=3), pol)
