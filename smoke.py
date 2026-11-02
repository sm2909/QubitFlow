import math
import qf_core as core
from qf_policies import make_policy

# Small circuit: 6 logical qubits
circ = core.adder(1000)

cfg = core.ArchConfig(
    d=11,
    cr_slots=2,
    n_banks=2,
    bank_capacity=math.ceil(circ.n_qubits / 2),
    bank_width=2,
    bus_lanes=1,
    max_parallel_ops=2,
)

policies = [
    ("lsqca_lazy", {}),
    ("fixed_prefetch", {"lookahead": 2}),
    ("fixed_belady", {"lookahead": 2}),
]

for name, params in policies:
    policy = make_policy(name, **params)
    sim = core.Simulator(circ, cfg, policy)

    result = sim.run()

    errors = core.validate_schedule(
        sim.trace, circ, cfg, sim.initial_loc
    )

    print(f"\nPolicy: {policy.name}")
    print(f"Result: {result.asdict()}")
    print(f"Trace events: {len(sim.trace)}")
    print(f"Validation errors: {errors}")

    assert not errors, errors