"""Smoke test: all registered policies must run and produce valid schedules on a tiny circuit.
    python smoke.py

NOTE: with cr_slots == 2 (== max operand arity) the three fixed-order policies are IDENTICAL BY
CONSTRUCTION: the head op needs both CR slots, so there is never a spare slot to prefetch into and
never a real victim choice (tests/test_simulator.py::test_cr2_makes_all_fixed_policies_identical_by_construction).
We therefore also run cr_slots=4, where the policies differ.
"""
import math
import qf_core as core
from qf_policies import make_policy

circ = core.adder(2)                       # 6 logical qubits
POLICIES = [("lsqca_lazy", {}), ("fixed_prefetch", {"lookahead": 2}), ("fixed_belady", {"lookahead": 2}),
            ("demand_belady", {}), ("dag", {}), ("dag", {"prefetch_window": 4})]

for cr in (2, 4):
    cfg = core.ArchConfig(d=11, cr_slots=cr, n_banks=2, bank_capacity=math.ceil(circ.n_qubits / 2),
                          bank_width=2, bus_lanes=1, max_parallel_ops=2,
                          timing=core.Timing(magic_unlimited=True))
    print(f"\n=== cr_slots={cr} ===")
    hashes = set()
    for name, params in POLICIES:
        sim = core.Simulator(circ, cfg, make_policy(name, **params))
        res = sim.run()
        errors = core.validate_schedule(sim.trace, circ, cfg, sim.initial_loc)
        hashes.add(core.trace_hash(sim.trace))
        print(f"{sim.policy.name:24s} cycles={res.cycles:5d} transfers={res.transfers:4d} "
              f"events={len(sim.trace):4d} validation_errors={errors}")
        assert not errors, errors
    print(f"distinct schedules: {len(hashes)} / {len(POLICIES)}")
print("\nsmoke OK")
