"""Correctness tests for the simulator (item 4 of the plan). Every test also runs the
independent schedule validator on the produced trace."""
import copy
import pytest
import qf_core as core
from qf_policies import make_policy, POLICIES
import qf_workloads as wl
from helpers import cfg, circ, run, ev, op_ev


# ---------------------------------------------------------------- basic circuits
def test_empty_circuit():
    c = circ(3, [])
    sim, r = run(c, cfg())
    assert r.cycles == 0 and r.transfers == 0 and sim.trace == []


def test_single_qubit_gate():
    a = cfg()
    sim, r = run(circ(2, [("h", (0,))]), a)
    load = ev(sim, "load")[0]
    # q0 sits in row 0, scan line starts at row rows//2 = 1 -> 1 shift + 1 port move
    assert load["end"] - load["start"] == a.line_cost(a.initial_scan_row(), 0) == 2
    assert op_ev(sim, 0)["start"] == load["end"]
    assert r.cycles == 2 + a.timing.t_1q


def test_two_qubit_gate_needs_both_operands():
    a = cfg(n_banks=2, bus_lanes=2, bank_capacity=2, bank_width=2)
    sim, r = run(circ(2, [("cx", (0, 1))]), a)
    loads = ev(sim, "load")
    assert len(loads) == 2
    assert op_ev(sim, 0)["start"] >= max(l["end"] for l in loads)
    assert r.cycles == max(l["end"] for l in loads) + a.timing.t_2q


def test_sequential_dependent_gates():
    sim, r = run(circ(1 + 1, [("h", (0,)), ("t", (0,)), ("h", (0,))]), cfg())
    e = [op_ev(sim, i) for i in range(3)]
    assert e[0]["end"] <= e[1]["start"] and e[1]["end"] <= e[2]["start"]
    assert r.cycles == e[2]["end"]


def test_independent_gates_run_in_parallel():
    a = cfg(n_banks=2, bus_lanes=2, bank_capacity=2, bank_width=2)
    c = circ(2, [("h", (0,)), ("h", (1,))])
    for pol, kw in [("prefetch_lru", {"lookahead": 1}), ("dag", {})]:   # policies that can overlap
        sim, r = run(c, a, pol, **kw)
        e0, e1 = op_ev(sim, 0), op_ev(sim, 1)
        assert e0["start"] == e1["start"], f"{pol}: independent ops should start together"
    # demand-only (lookahead 0) fetches op1's operand only after op0 started: ops run back-to-back
    sim, _ = run(c, a, "demand_lru")
    assert op_ev(sim, 1)["start"] >= op_ev(sim, 0)["start"]


def test_operation_blocked_until_operands_loaded():
    a = cfg(timing=dict(port_move=5))
    sim, _ = run(circ(2, [("cx", (0, 1))]), a)
    assert op_ev(sim, 0)["start"] >= 2 * 5 or op_ev(sim, 0)["start"] >= max(l["end"] for l in ev(sim, "load"))
    sim2 = core.Simulator(circ(2, [("h", (0,))]), a, make_policy("demand_lru"))
    assert not sim2.try_exec(0), "op must not start while its operand is in SAM"


# ---------------------------------------------------------------- CR capacity / eviction
def test_cr_capacity_exhaustion_is_enforced():
    a = cfg(cr_slots=2, n_banks=2, bus_lanes=2, bank_capacity=2, bank_width=2)
    s = core.Simulator(circ(4, [("h", (0,))]), a, make_policy("demand_lru"))
    assert s.try_load(0) and s.try_load(1)
    assert s.free_slots() == 0
    assert not s.try_load(2), "no free CR slot -> load must be refused"


def test_eviction_when_cr_is_full():
    a = cfg(cr_slots=2, n_banks=2, bus_lanes=2, bank_capacity=2, bank_width=2)
    gates = [("cx", (0, 1)), ("cx", (2, 3))]
    sim, r = run(circ(4, gates), a)
    stores = ev(sim, "store")
    assert len(stores) >= 2, "both q0 and q1 must be written back before q2/q3 can load"
    assert max(l["end"] for l in ev(sim, "load") if l["q"] in (2, 3)) <= op_ev(sim, 1)["start"]
    assert op_ev(sim, 1)["start"] >= min(s["end"] for s in stores)


@pytest.mark.parametrize("pol", ["demand_lru", "demand_belady", "dag"])
def test_small_cr_never_deadlocks(pol):
    c = core.random_circuit(10, 80, seed=1)
    run(c, cfg(cr_slots=2, n_banks=2, bank_capacity=5, bank_width=3, bus_lanes=1), pol)


# ---------------------------------------------------------------- reservation / release
def test_load_reserves_slot_now_and_frees_cell_on_completion():
    a = cfg()
    s = core.Simulator(circ(2, [("h", (0,))]), a, make_policy("demand_lru"))
    cell = s.loc[0][2]
    assert s.try_load(0)
    assert s.slot_owner.count(0) == 1, "destination slot reserved at issue"
    assert s.cells[0][cell] == 0, "source cell stays occupied until the load completes"
    assert s.state(0) == "XFER"
    s.t = s.xfers[0]["end"]; s._complete()
    assert s.cells[0][cell] is None and s.state(0) == "CR"


def test_store_reserves_cell_now_and_frees_slot_on_completion():
    a = cfg()
    s = core.Simulator(circ(2, [("h", (0,))]), a, make_policy("demand_lru"))
    s.try_load(0); s.t = s.xfers[0]["end"]; s._complete()
    slot = s.loc[0][1]
    assert s.try_store(0)
    dest = s.xfers[0]["cell"]
    assert s.cells[0][dest] == 0, "destination cell reserved at issue"
    assert s.slot_owner[slot] == 0, "CR slot held until the store completes"
    s.t = s.xfers[0]["end"]; s._complete()
    assert s.slot_owner[slot] is None and s.loc[0] == ("SAM", 0, dest)


# ---------------------------------------------------------------- ports and lanes
def test_same_bank_port_serialises_transfers():
    a = cfg(bus_lanes=2)                     # lanes are NOT the limit here, the bank port is
    s = core.Simulator(circ(4, [("h", (0,))]), a, make_policy("demand_lru"))
    assert s.try_load(0)
    assert not s.try_load(1), "single bank port busy"
    s.t = s.port_free[0]; s._complete()
    assert s.try_load(1)


def test_different_banks_overlap_when_lanes_allow():
    a = cfg(n_banks=2, bus_lanes=2, bank_capacity=2, bank_width=2)
    sim, _ = run(circ(2, [("cx", (0, 1))]), a)
    l = ev(sim, "load")
    assert l[0]["bank"] != l[1]["bank"] and l[0]["start"] == l[1]["start"] == 0


def test_bus_lane_contention():
    a = cfg(n_banks=2, bus_lanes=1, bank_capacity=2, bank_width=2)
    sim, _ = run(circ(2, [("cx", (0, 1))]), a)
    l = sorted(ev(sim, "load"), key=lambda e: e["start"])
    assert l[1]["start"] >= l[0]["end"], "one lane -> transfers to different banks must serialise"
    assert {e["lane"] for e in l} == {0}


# ---------------------------------------------------------------- magic states
def test_t_gate_waits_for_magic_state():
    a = cfg(n_banks=2, bus_lanes=2, bank_capacity=2, bank_width=2,
            timing=dict(magic_buffer=1, magic_interval=20, t_T=2))
    sim, r = run(circ(2, [("t", (0,)), ("t", (1,))]), a)
    t0, t1 = sorted([op_ev(sim, 0)["start"], op_ev(sim, 1)["start"]])
    assert t1 - t0 >= 20 - 0, "second T must wait one factory interval after the first consumed the buffer"
    assert r.msf_stall_cycles > 0 and r.msf_stalled_ops == 1


def test_unlimited_magic_never_stalls():
    a = cfg(n_banks=2, bus_lanes=2, bank_capacity=2, bank_width=2,
            timing=dict(magic_buffer=1, magic_interval=20, magic_unlimited=True))
    sim, r = run(circ(2, [("t", (0,)), ("t", (1,))]), a)
    assert r.msf_stall_cycles == 0 and not ev(sim, "msf")


def test_magic_factory_production_and_full_buffer():
    a = cfg(timing=dict(magic_buffer=2, magic_interval=3))
    s = core.Simulator(circ(2, [("t", (0,))]), a, make_policy("demand_lru"))
    s.ms_stock = 0
    s.t = 7; s._tick()
    assert s.ms_stock == 2 and s.ms_next == 9, "t=3 and t=6 productions only"
    s.t = 1000; s._tick()
    assert s.ms_stock == 2, "buffer-full: production halts at capacity"
    prod = [e for e in s.trace if e["kind"] == "msf"]
    assert [e["t"] for e in prod] == [3, 6] and all(e["delta"] == 1 for e in prod)


def test_factory_restarts_after_full_buffer_consumption():
    a = cfg(timing=dict(magic_buffer=2, magic_interval=10))
    sim, _ = run(circ(2, [("t", (0,)), ("t", (0,)), ("t", (0,))]), a)
    starts = sorted(op_ev(sim, i)["start"] for i in range(3))
    # initial stock 2 -> third T needs a NEW state: factory restarted when stock left 'full'
    assert starts[2] >= starts[0] + 10


# ---------------------------------------------------------------- validator rejects bad schedules
def _good():
    a = cfg(n_banks=2, bus_lanes=2, bank_capacity=3, bank_width=2, cr_slots=2,
            timing=dict(magic_buffer=1, magic_interval=7))
    c = circ(4, [("cx", (0, 1)), ("t", (0,)), ("cx", (2, 3)), ("t", (2,)), ("h", (1,))])
    sim, _ = run(c, a)
    return c, a, sim


def _errs(c, a, sim, mutate):
    tr = copy.deepcopy(sim.trace)
    mutate(tr)
    return core.validate_schedule(tr, c, a, sim.initial_loc)


def _first(tr, kind):
    return next(e for e in tr if e["kind"] == kind)


def test_validator_accepts_good_schedule():
    c, a, sim = _good()
    assert core.validate_schedule(sim.trace, c, a, sim.initial_loc) == []


@pytest.mark.parametrize("name,mutate,needle", [
    ("drop op",           lambda tr: tr.remove(_first(tr, "op")), "never executed"),
    ("duplicate op",      lambda tr: tr.append(dict(_first(tr, "op"))), "twice"),
    ("wrong op qubits",   lambda tr: _first(tr, "op").update(qubits=(3, 2)), "differ from circuit"),
    ("op too short",      lambda tr: _first(tr, "op").update(end=_first(tr, "op")["end"] - 1), "wrong duration"),
    ("op before load",    lambda tr: (_first(tr, "op").update(start=0, end=_first(tr, "op")["end"] - _first(tr, "op")["start"])), ""),
    ("load wrong cell",   lambda tr: _first(tr, "load").update(cell=_first(tr, "load")["cell"] ^ 1), ""),
    ("load wrong duration", lambda tr: _first(tr, "load").update(end=_first(tr, "load")["end"] + 3), "Line-SAM cost"),
    ("slot out of range", lambda tr: _first(tr, "load").update(slot=99), "out of range"),
    ("lane out of range", lambda tr: _first(tr, "load").update(lane=5), "out of range"),
    ("bank out of range", lambda tr: _first(tr, "load").update(bank=9), "out of range"),
])
def test_validator_rejects_mutations(name, mutate, needle):
    c, a, sim = _good()
    errs = _errs(c, a, sim, mutate)
    assert errs, f"mutation '{name}' was not detected"
    if needle:
        assert any(needle in e for e in errs), errs


def test_validator_rejects_dependency_violation():
    c, a, sim = _good()
    def m(tr):
        e1 = next(e for e in tr if e["kind"] == "op" and e["id"] == 1)   # T depends on cx(0,1)
        d = e1["end"] - e1["start"]; e1["start"] = 0; e1["end"] = d
    assert any("dependency" in e or "not resident" in e for e in _errs(c, a, sim, m))


def test_validator_rejects_bank_port_overlap():
    a = cfg(n_banks=1, bus_lanes=2)
    c = circ(4, [("h", (0,)), ("h", (1,))])
    sim, _ = run(c, a)
    def m(tr):
        l = [e for e in tr if e["kind"] == "load"]
        l[1]["start"] = l[0]["start"]; l[1]["end"] = l[0]["end"]; l[1]["lane"] = 1
    assert any("port" in e for e in _errs(c, a, sim, m))


def test_validator_rejects_bus_lane_overlap():
    a = cfg(n_banks=2, bus_lanes=2, bank_capacity=2, bank_width=2)
    c = circ(2, [("cx", (0, 1))])
    sim, _ = run(c, a)
    a1 = cfg(n_banks=2, bus_lanes=1, bank_capacity=2, bank_width=2)
    errs = core.validate_schedule(sim.trace, c, a1, sim.initial_loc)    # same trace, 1 lane
    assert any("bus-lane" in e or "bus lane" in e for e in errs)


def test_validator_rejects_parallel_op_limit():
    a = cfg(n_banks=2, bus_lanes=2, bank_capacity=2, bank_width=2, max_parallel_ops=2)
    c = circ(2, [("h", (0,)), ("h", (1,))])
    sim, _ = run(c, a, "dag")
    a1 = cfg(n_banks=2, bus_lanes=2, bank_capacity=2, bank_width=2, max_parallel_ops=1)
    assert any("parallel-op" in e for e in core.validate_schedule(sim.trace, c, a1, sim.initial_loc))


def test_validator_rejects_cr_slot_and_cell_double_allocation():
    a = cfg(n_banks=2, bus_lanes=2, bank_capacity=2, bank_width=2)
    c = circ(2, [("cx", (0, 1))])
    sim, _ = run(c, a)
    errs = _errs(c, a, sim, lambda tr: [e.update(slot=0) for e in tr if e["kind"] == "load"])
    assert any("CR slot" in e for e in errs)


def test_validator_rejects_negative_magic_inventory():
    c, a, sim = _good()
    def m(tr):                                           # an extra, unmatched consumption
        tr.append(dict(kind="msf", t=0, delta=-1, stock=-1, op=999))
    errs = _errs(c, a, sim, m)
    assert any("negative" in e or "mismatch" in e or "do not match" in e for e in errs)


def test_validator_rejects_t_before_any_state_could_exist():
    a = cfg(n_banks=2, bus_lanes=2, bank_capacity=2, bank_width=2,
            timing=dict(magic_buffer=1, magic_interval=50))
    c = circ(2, [("t", (0,)), ("t", (1,))])
    sim, _ = run(c, a)
    def m(tr):                                           # pretend the second T ran immediately
        ts = sorted((e for e in tr if e["kind"] == "op"), key=lambda e: e["start"])
        shift = ts[1]["start"] - ts[0]["start"]
        ts[1]["start"] -= shift - 1; ts[1]["end"] -= shift - 1
        for e in tr:
            if e["kind"] == "msf" and e["delta"] < 0 and e["t"] == ts[1]["start"] + shift - 1:
                e["t"] = ts[1]["start"]
    assert _errs(c, a, sim, m)


def test_validator_rejects_unknown_event_kind():
    c, a, sim = _good()
    assert core.validate_schedule(sim.trace + [dict(kind="bogus")], c, a, sim.initial_loc)


# ---------------------------------------------------------------- every policy -> valid schedule
POLICY_CASES = [(n, p) for n, p in [
    ("demand_lru", {}), ("demand_belady", {}), ("prefetch_lru", {"lookahead": 4}),
    ("prefetch_belady", {"lookahead": 4}), ("lsqca_lazy", {}), ("fixed_prefetch", {"lookahead": 2}),
    ("fixed_belady", {"lookahead": 2}), ("dag", {}), ("dag", {"prefetch_window": 4}),
    ("dag", {"prefetch_window": 8, "cost_weight": 1.0, "prefetch_evict": False})]]


@pytest.mark.parametrize("name,params", POLICY_CASES)
@pytest.mark.parametrize("workload", [
    lambda: wl.adder(3), lambda: wl.qft(5, max_k=3), lambda: wl.parallel_adders(2, 2),
    lambda: core.random_circuit(12, 120, seed=7)])
@pytest.mark.parametrize("sam_model", ["line", "generic"])
def test_every_policy_produces_valid_schedule(name, params, workload, sam_model):
    c = workload()
    a = cfg(cr_slots=3, n_banks=2, bank_capacity=-(-c.n_qubits // 2), bank_width=3, bus_lanes=2,
            sam_model=sam_model, timing=dict(magic_interval=5, magic_buffer=2))
    run(c, a, name, **params)


def test_all_registered_policies_are_covered():
    covered = {n for n, _ in POLICY_CASES}
    assert covered == set(POLICIES)


# ---------------------------------------------------------------- regression vs original engine
def test_legacy_generic_model_matches_original_engine():
    """Golden numbers produced by the ORIGINAL (pre-refactor) engine on smoke.py's workload."""
    c = core.adder(2)
    a = core.ArchConfig(d=11, cr_slots=2, n_banks=2, bank_capacity=3, bank_width=2, bus_lanes=1,
                        max_parallel_ops=2, sam_model="generic")
    for n, p in [("lsqca_lazy", {}), ("fixed_prefetch", {"lookahead": 2}), ("fixed_belady", {"lookahead": 2})]:
        _, r = run(c, a, n, **p)
        assert (r.cycles, r.loads, r.stores, r.exec_busy_cycles) == (229, 36, 34, 114)


def test_cr2_makes_all_fixed_policies_identical_by_construction():
    """Why smoke.py shows identical results: with cr_slots == 2 == max operand arity, the head op
    needs both slots, so there is never a spare slot to prefetch into and never a victim choice."""
    c = core.adder(3)
    a = cfg(cr_slots=2, n_banks=2, bus_lanes=1, bank_capacity=5, bank_width=3)
    hashes = set()
    for n, p in [("demand_lru", {}), ("prefetch_lru", {"lookahead": 4}), ("prefetch_belady", {"lookahead": 4})]:
        sim, _ = run(c, a, n, **p)
        hashes.add(core.trace_hash(sim.trace))
    assert len(hashes) == 1


def test_policies_differ_when_cr_has_slack():
    c = wl.parallel_adders(3, 3)
    a = cfg(cr_slots=6, n_banks=2, bus_lanes=2, bank_capacity=-(-c.n_qubits // 2), bank_width=4,
            timing=dict(magic_unlimited=True))
    h = {}
    for n, p in [("demand_lru", {}), ("prefetch_belady", {"lookahead": 4}), ("dag", {"prefetch_window": 4})]:
        sim, r = run(c, a, n, **p)
        h[n] = (core.trace_hash(sim.trace), r.cycles)
    assert len({v[0] for v in h.values()}) == 3
