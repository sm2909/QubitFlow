import unittest

import qf_core as core
from qf_policies import make_policy


def config(n_qubits, **kwargs):
    values = dict(cr_slots=2, n_banks=1, bank_capacity=max(2, n_qubits),
                  bank_width=1, bus_lanes=1, max_parallel_ops=2)
    values.update(kwargs)
    return core.ArchConfig(**values)


def run_checked(circuit, policy="lsqca_lazy", **cfg_kwargs):
    cfg = config(circuit.n_qubits, **cfg_kwargs)
    sim = core.Simulator(circuit, cfg, make_policy(policy))
    result = sim.run()
    errors = core.validate_schedule(sim.trace, circuit, cfg, sim.initial_loc)
    if errors:
        raise AssertionError(errors)
    return sim, result


class ConfigAndCircuitTests(unittest.TestCase):
    def test_reject_invalid_config(self):
        with self.assertRaises(ValueError):
            core.ArchConfig(cr_slots=0)
        with self.assertRaises(ValueError):
            core.ArchConfig(n_banks=0)

    def test_reject_invalid_qubit_and_arity(self):
        with self.assertRaises(ValueError):
            core.Circuit.from_gates(1, [("cx", (0, 1))])
        with self.assertRaises(ValueError):
            core.Circuit.from_gates(1, [("h", (1,))])

    def test_random_circuit_seed_is_deterministic(self):
        a = core.random_circuit(8, 40, seed=123)
        b = core.random_circuit(8, 40, seed=123)
        self.assertEqual(a.ops, b.ops)

    def test_qft_topology_generator(self):
        circ = core.qft(5)
        self.assertEqual(circ.n_qubits, 5)
        self.assertGreater(len(circ.ops), 5)
        self.assertTrue(all(len(op.qubits) in (1, 2) for op in circ.ops))

    def test_line_sam_latency_is_vertical_distance(self):
        cfg = core.ArchConfig(cr_slots=2, n_banks=1, bank_capacity=16,
                              bank_width=4, line_initial_row=1)
        self.assertEqual(cfg.xfer_time(4, 1), 0)
        self.assertEqual(cfg.xfer_time(12, 1), 2)
        self.assertEqual(cfg.xfer_time(0, 3), 3)

    def test_line_sam_defaults_to_one_lane_per_bank(self):
        cfg = core.ArchConfig(cr_slots=2, n_banks=3, bank_capacity=4)
        self.assertEqual(cfg.bus_lanes, 3)

    def test_zero_latency_gate_and_transfer_complete(self):
        circ = core.Circuit.from_gates(2, [("x", (0,)), ("cx", (0, 1))])
        sim, result = run_checked(circ, policy="joint", cr_slots=2,
                                  n_banks=2, bank_capacity=1, bus_lanes=2)
        self.assertGreaterEqual(result.cycles, 0)
        self.assertTrue(any(e["kind"] == "load" and e["start"] == e["end"]
                            for e in sim.trace))

    def test_factory_rate_and_buffer_scale_with_factory_count(self):
        timing = core.Timing(magic_factories=3, magic_interval=15)
        self.assertEqual(timing.magic_buffer, 6)
        circ = core.Circuit.from_gates(1, [("t", (0,)) for _ in range(10)])
        cfg = core.ArchConfig(cr_slots=2, n_banks=1, bank_capacity=1,
                              timing=timing)
        sim = core.Simulator(circ, cfg, make_policy("joint"))
        sim.run()
        produced = [e for e in sim.trace if e["kind"] == "magic_produce"]
        self.assertTrue(all(e["count"] <= 3 for e in produced))

    def test_initially_resident_register_configuration(self):
        circ = core.Circuit.from_gates(2, [("h", (0,)), ("cx", (0, 1))])
        cfg = config(2, cr_slots=2)
        sim = core.Simulator(circ, cfg, make_policy("joint"), initially_resident=True)
        result = sim.run()
        self.assertEqual(result.transfers, 0)
        self.assertEqual(core.validate_schedule(sim.trace, circ, cfg, sim.initial_loc), [])

    def test_qasm_import_real_qasmbench_circuit(self):
        import os
        path = os.path.join(os.path.dirname(__file__), "..", "benchmarks", "adder_n10.qasm")
        circ = core.from_qasm(path)
        self.assertEqual(circ.n_qubits, 10)
        self.assertGreater(len(circ.ops), 250)
        self.assertGreater(sum(op.kind == "T" for op in circ.ops), 0)



class SimulatorTests(unittest.TestCase):
    def test_empty_circuit(self):
        circ = core.Circuit(0, [])
        cfg = core.ArchConfig(cr_slots=2, n_banks=1, bank_capacity=2)
        sim = core.Simulator(circ, cfg, make_policy("joint"))
        result = sim.run()
        self.assertEqual(result.cycles, 0)
        self.assertEqual(core.validate_schedule(sim.trace, circ, cfg, sim.initial_loc), [])

    def test_single_qubit_gate(self):
        circ = core.Circuit.from_gates(1, [("h", (0,))])
        run_checked(circ)

    def test_two_qubit_gate_and_bank_port_contention(self):
        circ = core.Circuit.from_gates(2, [("cx", (0, 1))])
        sim, result = run_checked(circ, n_banks=1, bank_capacity=2)
        loads = [e for e in sim.trace if e["kind"] == "load"]
        self.assertEqual(len(loads), 2)
        self.assertGreaterEqual(loads[1]["start"], loads[0]["end"])
        self.assertGreater(result.cycles, 0)

    def test_independent_operations_and_dag_policy(self):
        circ = core.Circuit.from_gates(2, [("h", (0,)), ("x", (1,))])
        sim, _ = run_checked(circ, policy="joint", max_parallel_ops=2)
        ops = [e for e in sim.trace if e["kind"] == "op"]
        self.assertEqual({e["id"] for e in ops}, {0, 1})

    def test_cross_bank_loads_overlap_with_two_bus_lanes(self):
        circ = core.Circuit.from_gates(2, [("cx", (0, 1))])
        sim, _ = run_checked(circ, policy="joint", n_banks=2,
                             bank_capacity=2, bus_lanes=2)
        loads = [e for e in sim.trace if e["kind"] == "load"]
        self.assertEqual(len(loads), 2)
        self.assertEqual(loads[0]["start"], loads[1]["start"])

    def test_cr_capacity_forces_safe_eviction(self):
        circ = core.Circuit.from_gates(3, [
            ("h", (0,)), ("h", (1,)), ("h", (2,)), ("cx", (0, 2))
        ])
        sim, _ = run_checked(circ, policy="joint", cr_slots=2,
                             n_banks=1, bank_capacity=3)
        self.assertGreater(sum(e["kind"] == "store" for e in sim.trace), 0)

    def test_dependencies_are_respected_after_reordering(self):
        circ = core.Circuit.from_gates(2, [
            ("h", (0,)), ("x", (1,)), ("t", (0,)), ("cx", (0, 1))
        ])
        sim, _ = run_checked(circ, policy="joint", n_banks=2,
                             bank_capacity=2, bus_lanes=2)
        starts = {e["id"]: e["start"] for e in sim.trace if e["kind"] == "op"}
        ends = {e["id"]: e["end"] for e in sim.trace if e["kind"] == "op"}
        self.assertGreaterEqual(starts[2], ends[0])
        self.assertGreaterEqual(starts[3], ends[2])
        self.assertGreaterEqual(starts[3], ends[1])

    def test_t_gate_magic_supply_accounting(self):
        circ = core.Circuit.from_gates(1, [("t", (0,)) for _ in range(7)])
        sim, _ = run_checked(circ, policy="joint",
                             timing=core.Timing(magic_buffer=2, magic_interval=3))
        consumes = [e for e in sim.trace if e["kind"] == "magic_consume"]
        self.assertEqual(len(consumes), 7)

    def test_all_baselines_and_joint_validate(self):
        circ = core.adder(2)
        for policy in ("lsqca_lazy", "fixed_prefetch", "fixed_belady",
                       "dag_no_prefetch", "joint"):
            with self.subTest(policy=policy):
                run_checked(circ, policy=policy, cr_slots=4,
                            n_banks=2, bank_capacity=4, bank_width=2)

    def test_corrupted_trace_is_rejected(self):
        circ = core.Circuit.from_gates(1, [("h", (0,))])
        cfg = config(1)
        sim = core.Simulator(circ, cfg, make_policy("lsqca_lazy"))
        sim.run()
        trace = [dict(e) for e in sim.trace]
        op = next(e for e in trace if e["kind"] == "op")
        op["end"] += 1
        self.assertTrue(core.validate_schedule(trace, circ, cfg, sim.initial_loc))


if __name__ == "__main__":
    unittest.main()
