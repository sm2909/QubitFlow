"""Small, validated simulator smoke test. Not an experiment result."""
import math

import qf_core as core
from qf_policies import make_policy


circ = core.adder(2)
cfg = core.ArchConfig(
    d=11,
    cr_slots=2,
    n_banks=2,
    bank_capacity=72,
    bank_width=9,
    bus_lanes=2,
    max_parallel_ops=2,
    line_access_cost=1,
)

policies = [
    ("lsqca_lazy", {}),
    ("fixed_prefetch", {"lookahead": 2}),
    ("fixed_farthest_next_use", {"lookahead": 0}),
    ("dag_lru", {}),
    ("dag_next_use", {}),
    ("dag_next_use_nearest", {}),
    ("joint", {"prefetch_width": 2}),
]

for key, params in policies:
    policy = make_policy(key, **params)
    sim = core.Simulator(circ, cfg, policy)
    result = sim.run()
    errors = core.validate_schedule(sim.trace, circ, cfg, sim.initial_loc)
    print(f"{policy.name}: cycles={result.cycles}, transfers={result.transfers}, "
          f"trace_events={len(sim.trace)}, validation_errors={len(errors)}")
    assert not errors, errors
