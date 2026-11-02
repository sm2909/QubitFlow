import os
import tempfile
from pathlib import Path
import unittest

import qf_core as core
from qf_policies import make_policy


def make_cfg(n_qubits, **kwargs):
    values = dict(
        cr_slots=2,
        n_banks=1,
        bank_capacity=max(2, n_qubits),
        bank_width=1,
        max_parallel_ops=2,
    )
    values.update(kwargs)
    return core.ArchConfig(**values)


def run_checked(circuit, policy="lsqca_lazy", cfg=None, initially_resident=False):
    cfg = cfg or make_cfg(circuit.n_qubits)
    sim = core.Simulator(
        circuit, cfg, make_policy(policy), initially_resident=initially_resident
    )
    result = sim.run()
    errors = core.validate_schedule(sim.trace, circuit, cfg, sim.initial_loc)
    if errors:
        raise AssertionError(errors)
    return sim, result


def parse_qasm(source):
    with tempfile.NamedTemporaryFile("w", suffix=".qasm", delete=False) as handle:
        handle.write(source)
        path = handle.name
    try:
        return core.from_qasm(path)
    finally:
        os.unlink(path)


class ConfigAndCircuitTests(unittest.TestCase):
    def test_reject_invalid_config(self):
        with self.assertRaises(ValueError):
            core.ArchConfig(cr_slots=0)
        with self.assertRaises(ValueError):
            core.ArchConfig(n_banks=0)
        with self.assertRaises(ValueError):
            core.ArchConfig(bank_width=0)

    def test_reject_invalid_or_unsupported_gate(self):
        with self.assertRaises(ValueError):
            core.Circuit.from_gates(1, [("cx", (0, 1))])
        with self.assertRaises(ValueError):
            core.Circuit.from_gates(1, [("h", (1,))])
        with self.assertRaises(ValueError):
            core.Circuit.from_gates(1, [("u3", (0,))])

    def test_random_seed_is_deterministic(self):
        self.assertEqual(
            core.random_circuit(8, 40, seed=123).ops,
            core.random_circuit(8, 40, seed=123).ops,
        )

    def test_line_sam_row_distance(self):
        cfg = core.ArchConfig(
            cr_slots=2, n_banks=1, bank_capacity=16, bank_width=4,
            line_initial_row=0, line_move_per_row=3,
        )
        self.assertEqual(cfg.xfer_time(1, 0), 0)
        self.assertEqual(cfg.xfer_time(13, 0), 9)
        self.assertEqual(cfg.xfer_time(13, 3), 0)

    def test_default_bus_lanes_scale_with_banks(self):
        cfg = core.ArchConfig(n_banks=4, bank_capacity=8)
        self.assertEqual(cfg.bus_lanes, 4)

    def test_qasm_rz_clifford_t_mapping(self):
        circ = parse_qasm("""OPENQASM 2.0;
            include "qelib1.inc";
            qreg q[1];
            rz(pi/2) q[0];
            rz(-pi/2) q[0];
            rz(pi/4) q[0];
            rz(3*pi/4) q[0];
        """)
        self.assertEqual([op.name for op in circ.ops],
                         ["s", "sdg", "t", "s", "t"])

    def test_qasm_rejects_non_clifford_t_exact_rotation(self):
        with self.assertRaises(ValueError):
            parse_qasm("""OPENQASM 2.0; qreg q[1]; rz(pi/8) q[0];""")

    def test_qasm_rejects_operations_after_measurement(self):
        with self.assertRaises(ValueError):
            parse_qasm("""OPENQASM 2.0; qreg q[1]; creg c[1];
                measure q[0] -> c[0]; x q[0];""")

    def test_qasm_expands_toffoli(self):
        circ = parse_qasm("""OPENQASM 2.0;
            include "qelib1.inc";
            qreg q[3]; creg c[1];
            ccx q[0],q[1],q[2];
            measure q[2] -> c[0];
        """)
        self.assertEqual(circ.n_qubits, 3)
        self.assertEqual(sum(op.kind == "T" for op in circ.ops), 7)
        self.assertEqual(len(circ.ops), 16)

    def test_vendor_qasmbench_circuits_import(self):
        root = Path(__file__).resolve().parents[1] / "benchmarks"
        adder = core.from_qasm(str(root / "qasmb_adder_n28.qasm"))
        multiplier = core.from_qasm(str(root / "qasmb_multiplier_n15.qasm"))
        self.assertEqual(adder.n_qubits, 28)
        self.assertEqual(multiplier.n_qubits, 15)
        self.assertGreater(sum(op.kind == "T" for op in adder.ops), 0)
        self.assertGreater(sum(op.kind == "T" for op in multiplier.ops), 0)


class SimulatorTests(unittest.TestCase):
    def test_empty_circuit(self):
        circ = core.Circuit(0, [])
        cfg = core.ArchConfig(cr_slots=2, n_banks=1, bank_capacity=2)
        sim, result = run_checked(circ, "joint", cfg=cfg)
        self.assertEqual(result.cycles, 0)

    def test_paper_timing_categories(self):
        circ = core.Circuit.from_gates(1, [
            ("h", (0,)), ("s", (0,)), ("x", (0,)), ("t", (0,)),
            ("measure", (0,)),
        ])
        cfg = make_cfg(1)
        sim, _ = run_checked(circ, "joint", cfg=cfg)
        durations = [e["end"] - e["start"] for e in sim.trace if e["kind"] == "op"]
        self.assertEqual(durations, [3, 2, 0, 3, 0])

    def test_same_bank_port_serializes_positive_latency_transfers(self):
        circ = core.Circuit.from_gates(2, [("cx", (0, 1))])
        cfg = core.ArchConfig(
            cr_slots=2, n_banks=1, bank_capacity=2, bank_width=1,
            line_initial_row=1, bus_lanes=1,
        )
        sim, _ = run_checked(circ, "joint", cfg=cfg)
        loads = [e for e in sim.trace if e["kind"] == "load"]
        self.assertEqual(len(loads), 2)
        self.assertGreaterEqual(loads[1]["start"], loads[0]["end"])

    def test_independent_bank_transfers_overlap_by_default(self):
        circ = core.Circuit.from_gates(2, [("cx", (0, 1))])
        cfg = core.ArchConfig(
            cr_slots=2, n_banks=2, bank_capacity=2, bank_width=1,
            line_initial_row=1,
        )
        sim, _ = run_checked(circ, "joint", cfg=cfg)
        loads = [e for e in sim.trace if e["kind"] == "load"]
        self.assertEqual(len(loads), 2)
        self.assertEqual(loads[0]["start"], loads[1]["start"])

    def test_zero_duration_transfer_and_gate_complete_without_deadlock(self):
        circ = core.Circuit.from_gates(2, [("x", (0,)), ("cx", (0, 1))])
        cfg = core.ArchConfig(
            cr_slots=2, n_banks=2, bank_capacity=2, bank_width=2,
            line_initial_row=0,
        )
        sim, result = run_checked(circ, "joint", cfg=cfg)
        self.assertGreaterEqual(result.cycles, 0)
        self.assertTrue(any(e["kind"] == "load" and e["start"] == e["end"]
                            for e in sim.trace))

    def test_initially_resident_baseline(self):
        circ = core.Circuit.from_gates(2, [("h", (0,)), ("cx", (0, 1))])
        cfg = core.ArchConfig(cr_slots=2, n_banks=1, bank_capacity=2)
        sim, result = run_checked(circ, "joint", cfg=cfg, initially_resident=True)
        self.assertEqual(result.loads + result.stores, 0)
        self.assertTrue(all(loc[0] == "CR" for loc in sim.initial_loc.values()))

    def test_cr_capacity_forces_safe_eviction(self):
        circ = core.Circuit.from_gates(3, [
            ("h", (0,)), ("h", (1,)), ("h", (2,)), ("cx", (0, 2))
        ])
        cfg = core.ArchConfig(cr_slots=2, n_banks=1, bank_capacity=3)
        sim, _ = run_checked(circ, "joint", cfg=cfg)
        self.assertGreater(sum(e["kind"] == "store" for e in sim.trace), 0)

    def test_joint_does_not_thrash_between_incompatible_ready_ops(self):
        circ = core.Circuit.from_gates(4, [
            ("h", (0,)), ("h", (1,)), ("h", (2,)), ("h", (3,)),
            ("cx", (0, 1)), ("cx", (2, 3)),
        ])
        cfg = core.ArchConfig(
            cr_slots=2, n_banks=2, bank_capacity=4, bank_width=1,
            line_initial_row=1,
        )
        sim, _ = run_checked(circ, "joint", cfg=cfg)
        self.assertEqual(
            {e["id"] for e in sim.trace if e["kind"] == "op"},
            set(range(len(circ.ops))),
        )

    def test_dag_dependencies_after_reordering(self):
        circ = core.Circuit.from_gates(2, [
            ("h", (0,)), ("x", (1,)), ("t", (0,)), ("cx", (0, 1))
        ])
        cfg = core.ArchConfig(cr_slots=2, n_banks=2, bank_capacity=2)
        sim, _ = run_checked(circ, "joint", cfg=cfg)
        starts = {e["id"]: e["start"] for e in sim.trace if e["kind"] == "op"}
        ends = {e["id"]: e["end"] for e in sim.trace if e["kind"] == "op"}
        self.assertGreaterEqual(starts[2], ends[0])
        self.assertGreaterEqual(starts[3], ends[2])
        self.assertGreaterEqual(starts[3], ends[1])

    def test_magic_factory_produces_at_paper_cadence(self):
        circ = core.Circuit.from_gates(1, [("t", (0,)) for _ in range(5)])
        timing = core.Timing(magic_buffer=1, magic_interval=3)
        cfg = make_cfg(1, timing=timing)
        sim, _ = run_checked(circ, "joint", cfg=cfg, initially_resident=True)
        production = [e for e in sim.trace if e["kind"] == "magic_produce"]
        self.assertTrue(production)
        self.assertTrue(all(e["time"] % 3 == 0 for e in production))
        self.assertEqual(sum(e["count"] for e in production), 4)

    def test_all_policies_produce_valid_trace(self):
        circ = core.adder(2)
        cfg = core.ArchConfig(
            cr_slots=4, n_banks=2, bank_capacity=8, bank_width=2,
        )
        for name in ("lsqca_lazy", "fixed_prefetch", "fixed_belady",
                     "dag_no_prefetch", "joint"):
            with self.subTest(policy=name):
                run_checked(circ, name, cfg=cfg)

    def test_corrupted_trace_is_rejected(self):
        circ = core.Circuit.from_gates(1, [("h", (0,))])
        cfg = make_cfg(1)
        sim = core.Simulator(circ, cfg, make_policy("lsqca_lazy"))
        sim.run()
        trace = [dict(event) for event in sim.trace]
        next(e for e in trace if e["kind"] == "op")["end"] += 1
        self.assertTrue(core.validate_schedule(trace, circ, cfg, sim.initial_loc))


if __name__ == "__main__":
    unittest.main()
