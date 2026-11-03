import math
import qf_core as core
from qf_policies import make_policy


def cfg(**kw):
    """Small default architecture (Line-SAM). Override any ArchConfig field; `timing` may be a
    dict of Timing overrides."""
    t = kw.pop("timing", {})
    base = dict(d=11, cr_slots=4, n_banks=1, bank_capacity=8, bank_width=4, bus_lanes=1,
                max_parallel_ops=4, timing=core.Timing(**t))
    base.update(kw)
    return core.ArchConfig(**base)


def circ(n, gates, name="t"):
    return core.Circuit.from_gates(n, gates, name)


def run(c, a, policy="demand_lru", validate=True, **pp):
    sim = core.Simulator(c, a, make_policy(policy, **pp))
    res = sim.run()
    if validate:
        errs = core.validate_schedule(sim.trace, c, a, sim.initial_loc)
        assert not errs, errs
    return sim, res


def ev(sim, kind):
    return [e for e in sim.trace if e["kind"] == kind]


def op_ev(sim, i):
    return next(e for e in sim.trace if e["kind"] == "op" and e["id"] == i)
