import cmath
import math
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



def apply_basis_circuit(circuit, basis):
    """Tiny exact statevector checker for Clifford+T basis-state regressions."""
    state = [0j] * (1 << circuit.n_qubits)
    state[basis] = 1 + 0j
    scale = 1 / math.sqrt(2)
    for op in circuit.ops:
        if op.name == "h":
            q = op.qubits[0]
            mask = 1 << q
            for i in range(len(state)):
                if not (i & mask):
                    a, b = state[i], state[i | mask]
                    state[i] = (a + b) * scale
                    state[i | mask] = (a - b) * scale
        elif op.name in ("t", "tdg"):
            q = op.qubits[0]
            phase = cmath.exp((1j if op.name == "t" else -1j) * math.pi / 4)
            for i in range(len(state)):
                if i & (1 << q):
                    state[i] *= phase
        elif op.name == "cx":
            control, target = op.qubits
            cmask, tmask = 1 << control, 1 << target
            for i in range(len(state)):
                if (i & cmask) and not (i & tmask):
                    j = i | tmask
                    state[i], state[j] = state[j], state[i]
        else:
            raise AssertionError(f"statevector test does not support {op.name}")
    return state


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

    def test_reject_direct_operation_kind_gate_mismatch(self):
        with self.assertRaises(ValueError):
            core.Circuit(2, [core.Op(0, "1q", (0,), "cx")])
        with self.assertRaises(ValueError):
            core.Circuit(1, [core.Op(0, "T", (0,), "h")])

    def test_toffoli_decomposition_is_exact_on_all_basis_states(self):
        circ = core.Circuit.from_gates(3, core._toffoli(0, 1, 2), "toffoli")
        global_phase = None
        for basis in range(8):
            state = apply_basis_circuit(circ, basis)
            expected = basis ^ (1 << 2 if (basis & 0b11) == 0b11 else 0)
            self.assertAlmostEqual(abs(state[expected]), 1.0, places=8)
            self.assertTrue(all(abs(v) < 1e-8 for i, v in enumerate(state)
                                if i != expected))
            global_phase = state[expected] if global_phase is None else global_phase
            self.assertLess(abs(state[expected] - global_phase), 1e-8)

    def test_two_bit_cuccaro_adder_on_all_classical_inputs(self):
        circ = core.adder(2)
        global_phase = None
        for a in range(4):
            for b in range(4):
                basis = (((a >> 0) & 1) << 1) | (((b >> 0) & 1) << 2) \
                        | (((a >> 1) & 1) << 3) | (((b >> 1) & 1) << 4)
                total = a + b
                expected = (((a >> 0) & 1) << 1) | ((total & 1) << 2) \
                           | (((a >> 1) & 1) << 3) | (((total >> 1) & 1) << 4) \
                           | (((total >> 2) & 1) << 5)
                state = apply_basis_circuit(circ, basis)
                self.assertAlmostEqual(abs(state[expected]), 1.0, places=8)
                self.assertTrue(all(abs(v) < 1e-8 for i, v in enumerate(state)
                                    if i != expected))
                global_phase = state[expected] if global_phase is None else global_phase
                self.assertLess(abs(state[expected] - global_phase), 1e-8)

    def test_random_seed_is_deterministic(self):
        self.assertEqual(
            core.random_circuit(8, 40, seed=123).ops,
            core.random_circuit(8, 40, seed=123).ops,
        )

    def test_joint_prefetch_widths_have_distinct_policy_names(self):
        names = {
            make_policy("joint", prefetch_width=w).name
            for w in (1, 2, 4)
        }
        self.assertEqual(len(names), 3)

    def test_prefetch_window_ignores_preexisting_started_consumers(self):
        circ = core.Circuit.from_gates(1, [
            ("h", (0,)),
            ("t", (0,)),
        ])
        cfg = core.ArchConfig(
            cr_slots=2, n_banks=1, bank_capacity=1, bank_width=1,
        )
        sim = core.Simulator(
            circ, cfg, make_policy("joint", prefetch_width=1),
            initially_resident=True,
        )
        policy = sim.policy
        # Operation 0 started before q0 was speculatively loaded for op1.
        sim.started.add(0)
        policy.prefetched_qubits.add(0)
        policy._prefetched_started[0] = frozenset(sim.started)
        policy._refresh_prefetch_window(sim)
        self.assertIn(0, policy.prefetched_qubits)

        # Once a new consumer starts after prefetch, the slot is released.
        sim.started.add(1)
        policy._refresh_prefetch_window(sim)
        self.assertNotIn(0, policy.prefetched_qubits)
        self.assertNotIn(0, policy._prefetched_started)

    def test_joint_prefetch_window_is_persistent_and_overlaps_execution(self):
        circ = core.Circuit.from_gates(4, [
            ("h", (0,)), ("h", (1,)), ("cx", (0, 1)),
            ("cx", (0, 2)), ("cx", (1, 3)),
        ])
        cfg = core.ArchConfig(
            cr_slots=4, n_banks=2, bank_capacity=4, bank_width=1,
            line_initial_row=0, line_access_cost=1, line_move_per_row=1,
            timing=core.Timing(t_2q=6),
        )
        # Use explicit policies: each has the same scheduler and architecture,
        # differing only in the maximum number of speculative qubits.
        one_sim = core.Simulator(circ, cfg, make_policy("joint", prefetch_width=1))
        one_result = one_sim.run()
        two_sim = core.Simulator(circ, cfg, make_policy("joint", prefetch_width=2))
        two_result = two_sim.run()
        self.assertFalse(core.validate_schedule(one_sim.trace, circ, cfg, one_sim.initial_loc))
        self.assertFalse(core.validate_schedule(two_sim.trace, circ, cfg, two_sim.initial_loc))
        parent_end = next(e["end"] for e in two_sim.trace
                          if e["kind"] == "op" and e["id"] == 2)
        speculative = [e for e in two_sim.trace
                       if e["kind"] == "load" and e["q"] in (2, 3)]
        self.assertEqual(len(speculative), 2)
        self.assertTrue(all(e["start"] < parent_end for e in speculative))
        self.assertLess(two_result.cycles, one_result.cycles)

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

    def test_experiment_bank_sweep_preserves_total_sam_budget(self):
        import run as experiments
        circuit = core.adder(2)
        for banks in (1, 2, 4):
            cfg = experiments.build_cfg(circuit, {
                "cr_slots": 4, "n_banks": banks, "line_access_cost": 1
            })
            self.assertEqual(cfg.n_banks * cfg.bank_capacity, 144)

    def test_line_access_overhead_is_explicit(self):
        cfg = core.ArchConfig(
            cr_slots=2, n_banks=1, bank_capacity=8, bank_width=2,
            line_initial_row=0, line_access_cost=2,
        )
        self.assertEqual(cfg.xfer_time(0, 0), 2)
        self.assertEqual(cfg.xfer_time(6, 0), 5)

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

    def test_qasm_sx_expands_to_h_s_h(self):
        circ = parse_qasm("""OPENQASM 2.0;
            include "qelib1.inc";
            qreg q[1];
            sx q[0];
            sxdg q[0];
        """)
        self.assertEqual(
            [op.name for op in circ.ops],
            ["h", "s", "h", "h", "sdg", "h"],
        )

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
        expected = {
            "qasmb_adder_n28.qasm": 28,
            "qasmb_multiplier_n15.qasm": 15,
            "qasmb_adder_n118.qasm": 118,
            "qasmb_multiplier_n45.qasm": 45,
        }
        for filename, qubits in expected.items():
            with self.subTest(filename=filename):
                circuit = core.from_qasm(str(root / filename))
                self.assertEqual(circuit.n_qubits, qubits)
                self.assertGreater(len(circuit.ops), 0)
                self.assertGreater(sum(op.kind == "2q" for op in circuit.ops), 0)


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

    def test_dag_scheduler_reorders_ready_ops_by_critical_path(self):
        circ = core.Circuit.from_gates(3, [
            ("cx", (0, 2)), ("h", (1,)), ("t", (1,)), ("cx", (1, 0))
        ])
        cfg = core.ArchConfig(
            cr_slots=2, n_banks=2, bank_capacity=3, bank_width=1,
            line_initial_row=0, line_access_cost=1,
            timing=core.Timing(t_2q=1),
        )
        dag, _ = run_checked(circ, "dag_lru", cfg=cfg)
        fixed, _ = run_checked(circ, "lsqca_lazy", cfg=cfg)
        dag_starts = {
            e["id"]: e["start"] for e in dag.trace if e["kind"] == "op"
        }
        fixed_starts = {
            e["id"]: e["start"] for e in fixed.trace if e["kind"] == "op"
        }
        self.assertLess(dag_starts[1], dag_starts[0])
        self.assertLess(fixed_starts[0], fixed_starts[1])

    def test_incremental_ready_frontier_and_next_use_cursor(self):
        circ = core.Circuit.from_gates(2, [
            ("h", (0,)),       # op 0
            ("h", (1,)),       # op 1
            ("cx", (0, 1)),    # op 2 depends on 0 and 1
            ("t", (0,)),       # op 3 depends on 2
        ])
        cfg = core.ArchConfig(
            cr_slots=2, n_banks=1, bank_capacity=2, bank_width=1,
        )
        sim = core.Simulator(
            circ, cfg, make_policy("dag_lru"), initially_resident=True,
        )
        self.assertEqual(set(sim.ready_ops()), {0, 1})
        self.assertEqual(sim.next_use(0), 0)
        self.assertTrue(sim.try_exec(0))
        self.assertEqual(set(sim.ready_ops()), {1})
        self.assertEqual(sim.next_use(0), 2)
        self.assertEqual(sim.pending_predecessors[2], 2)

        sim.t = 3
        sim._complete()
        self.assertEqual(set(sim.ready_ops()), {1})
        self.assertTrue(sim.try_exec(1))
        sim.t = 6
        sim._complete()
        self.assertEqual(set(sim.ready_ops()), {2})
        self.assertEqual(sim.pending_predecessors[2], 0)

        self.assertTrue(sim.try_exec(2))
        self.assertEqual(set(sim.ready_ops()), set())
        self.assertEqual(sim.next_use(0), 3)
        self.assertEqual(sim.next_use(1), float("inf"))
        sim.t = 7
        sim._complete()
        self.assertEqual(set(sim.ready_ops()), {3})

    def test_incremental_ready_frontier_matches_full_dag_scan(self):
        circ = core.random_circuit(
            2, 30, p_t=0.2, p_2q=0.5, seed=91
        )
        cfg = core.ArchConfig(
            cr_slots=2,
            n_banks=1,
            bank_capacity=2,
            bank_width=1,
            max_parallel_ops=2,
            timing=core.Timing(
                magic_factories=100, magic_buffer=200, magic_interval=1000
            ),
        )
        sim = core.Simulator(
            circ, cfg, make_policy("dag_lru"), initially_resident=True
        )
        finished_all = False
        for _ in range(1000):
            expected = {
                i for i in range(len(circ.ops))
                if i not in sim.started
                and all(parent in sim.finished for parent in circ.preds[i])
            }
            self.assertEqual(set(sim.ready_ops()), expected)
            if len(sim.finished) == len(circ.ops):
                finished_all = True
                break

            progress = False
            for op_id in sorted(sim.ready_ops()):
                progress |= sim.try_exec(op_id)
                sim._complete()  # Includes zero-duration operations.
                expected = {
                    i for i in range(len(circ.ops))
                    if i not in sim.started
                    and all(parent in sim.finished for parent in circ.preds[i])
                }
                self.assertEqual(set(sim.ready_ops()), expected)

            if len(sim.finished) == len(circ.ops):
                finished_all = True
                break
            if sim.running:
                sim.t = min(end for end, _ in sim.running)
                sim._complete()
            elif not progress:
                self.fail("ready-frontier test made no progress")
        self.assertTrue(finished_all, "did not complete the small resident circuit")
        self.assertEqual(sim.next_use(0), float("inf"))
        self.assertEqual(sim.next_use(1), float("inf"))

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

    def test_validator_rejects_missing_factory_production_event(self):
        circ = core.Circuit.from_gates(1, [("t", (0,)) for _ in range(5)])
        timing = core.Timing(magic_buffer=2, magic_interval=3)
        cfg = make_cfg(1, timing=timing)
        sim = core.Simulator(
            circ, cfg, make_policy("joint", prefetch_width=1),
            initially_resident=True,
        )
        sim.run()
        trace = [dict(event) for event in sim.trace]
        production = next(e for e in trace
                          if e["kind"] == "magic_produce" and e["time"] == 12)
        trace.remove(production)
        errors = core.validate_schedule(trace, circ, cfg, sim.initial_loc)
        self.assertTrue(any("production mismatch" in error for error in errors), errors)

    def test_all_policies_produce_valid_trace(self):
        circ = core.adder(2)
        cfg = core.ArchConfig(
            cr_slots=4, n_banks=2, bank_capacity=8, bank_width=2,
        )
        for name in ("lsqca_lazy", "fixed_prefetch",
                     "fixed_farthest_next_use", "dag_lru",
                     "dag_next_use", "dag_next_use_nearest", "joint"):
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
