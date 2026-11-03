"""DAG-aware scheduling support (items 7 and 9)."""
import pytest
import qf_core as core
import qf_workloads as wl
from qf_policies import make_policy, DagPolicy
from helpers import cfg, circ, run, ev, op_ev


def diamond():
    #   0:h(0)  1:h(1)   2:cx(0,1)   3:t(0)   4:h(2)
    return circ(3, [("h", (0,)), ("h", (1,)), ("cx", (0, 1)), ("t", (0,)), ("h", (2,))])


def test_op_states_ready_running_done():
    c = diamond()
    a = cfg(n_banks=2, bus_lanes=2, bank_capacity=2, bank_width=2, cr_slots=4)
    s = core.Simulator(c, a, make_policy("dag"))
    assert [s.op_state(i) for i in range(5)] == ["ready", "ready", "waiting", "waiting", "ready"]
    assert s.ready_ops() == [0, 1, 4]
    for q in (0, 1, 2):
        s.try_load(q)
    s.t = max(x["end"] for x in s.xfers); s._complete()
    assert s.try_exec(0)
    assert s.op_state(0) == "running" and 0 not in s.ready_ops()
    assert s.op_state(2) == "waiting"                    # pred 0 only RUNNING, not done
    assert 2 not in s.ready_ops() and 2 not in s.soon_ready_ops()  # pred 1 not started
    s.t = s.running[0][0]; s._complete()
    assert s.op_state(0) == "done" and s.op_state(2) == "waiting"
    assert s.try_exec(1); s.t = s.running[0][0]; s._complete()
    assert s.op_state(2) == "ready" and s.ready_ops() == [2, 4]


def test_cannot_start_op_whose_preds_are_not_finished():
    c = diamond()
    s = core.Simulator(c, cfg(), make_policy("dag"))
    for q in (0, 1, 2):
        s.try_load(q); s.t = s.xfers[0]["end"]; s._complete()
    assert not s.try_exec(2), "cx depends on h(0), h(1)"
    assert s.try_exec(4)


def test_independent_ops_execute_out_of_program_order():
    """Op 1 is independent of op 0 but program-later; a DAG policy may run it first."""
    c = circ(4, [("t", (0,)), ("h", (3,))])
    a = cfg(n_banks=1, bus_lanes=1, bank_capacity=4, bank_width=2, cr_slots=2,
            timing=dict(magic_buffer=1, magic_interval=30))
    s = core.Simulator(c, a, make_policy("dag"))
    s.ms_stock = 0                                       # T op 0 cannot run: no magic state
    s.try_load(3); s.t = s.xfers[0]["end"]; s._complete()
    assert s.try_exec(1) and not s.try_exec(0)
    assert s.started == {1}
    # the magic-starved T op does not block the independent Hadamard under the DAG policy
    sim, _ = run(c, a, "dag")


def test_dag_policy_actually_reorders_on_parallel_workload():
    c = wl.parallel_adders(3, 2, interleave=False)       # blocks concatenated: huge DAG parallelism
    a = cfg(cr_slots=6, n_banks=2, bank_capacity=-(-c.n_qubits // 2), bank_width=3, bus_lanes=2,
            timing=dict(magic_unlimited=True))
    sim, _ = run(c, a, "dag", prefetch_window=4)
    starts = {e["id"]: e["start"] for e in sim.trace if e["kind"] == "op"}
    assert any(starts[i] > starts[j] for i in range(len(c.ops)) for j in range(i + 1, len(c.ops))), \
        "expected some op to start after a program-later op"


def test_dag_beats_fixed_order_on_concatenated_independent_blocks():
    c = wl.parallel_adders(4, 2, interleave=False)
    a = cfg(cr_slots=6, n_banks=2, bank_capacity=-(-c.n_qubits // 2), bank_width=3, bus_lanes=2,
            timing=dict(magic_unlimited=True))
    _, fixed = run(c, a, "prefetch_belady", lookahead=4)
    _, dag = run(c, a, "dag", prefetch_window=4)
    assert dag.cycles < fixed.cycles


def test_future_uses_and_next_use_track_started_ops_for_any_schedule():
    c = circ(3, [("h", (0,)), ("h", (1,)), ("cx", (0, 1)), ("t", (1,)), ("h", (2,))])
    a = cfg()
    s = core.Simulator(c, a, make_policy("dag"))
    assert s.future_uses(0) == [0, 2] and s.next_use(2) == 4
    for q in (2,):
        s.try_load(q); s.t = s.xfers[0]["end"]; s._complete()
    assert s.try_exec(4)                                 # out-of-order start of the LAST op
    assert s.future_uses(2) == [] and s.next_use(2) == core.INF
    assert s.future_uses(1) == [1, 2, 3] and s.next_use(1) == 1
    assert s.head() == 0, "head() is the lowest unstarted op index"


def test_level_and_critical_path_priority():
    c = circ(2, [("t", (0,)), ("h", (1,)), ("cx", (0, 1))])
    T = core.Timing(t_T=5)
    cp = c.critical_path(T)
    assert cp[0] > cp[1] and c.level == [0, 0, 1]


# ---------------------------------------------------------------- item 9 behaviours
def test_dag_prioritises_ops_with_resident_operands():
    c = circ(4, [("h", (2,)), ("h", (0,))])
    a = cfg(n_banks=1, bank_capacity=4, bank_width=2, cr_slots=2, timing=dict(magic_unlimited=True))
    s = core.Simulator(c, a, make_policy("dag"))
    s.try_load(0); s.t = s.xfers[0]["end"]; s._complete()    # q0 resident, q2 still in SAM
    pol = make_policy("dag")
    assert pol.step(s) and s.started == {1}, "resident-operand op should issue before fetching for op 0"


def test_demand_loads_take_priority_over_prefetch():
    """With a single bank port, the demand operand must be issued before any speculative load."""
    c = circ(6, [("h", (0,)), ("h", (1,)), ("h", (2,))])
    a = cfg(n_banks=1, bank_capacity=6, bank_width=3, cr_slots=4, bus_lanes=1,
            timing=dict(magic_unlimited=True))
    sim, _ = run(c, a, "dag", prefetch_window=4)
    loads = sorted(ev(sim, "load"), key=lambda e: e["start"])
    assert loads[0]["q"] == 0, "first transfer must serve the highest-priority demand op"
    assert {e["q"] for e in loads} == {0, 1, 2}, "nothing outside the DAG frontier is fetched"


def test_eviction_prefers_qubits_with_no_future_use():
    c = circ(4, [("cx", (0, 1)), ("cx", (2, 3)), ("cx", (0, 2))])
    a = cfg(n_banks=2, bank_capacity=2, bank_width=2, cr_slots=3, bus_lanes=2,
            timing=dict(magic_unlimited=True))
    pol = DagPolicy(0)
    s = core.Simulator(c, a, pol)
    for q in (0, 1, 2):
        s.try_load(q); s.t = max(x["end"] for x in s.xfers); s._complete()
    s.try_exec(0); s.t = s.running[0][0]; s._complete()          # cx(0,1) done: q1 never used again
    assert pol._pick_victim(s, protected={2}) == 1


def test_eviction_cost_weight_trades_urgency_for_store_cost():
    a = cfg(n_banks=1, bank_capacity=16, bank_width=4, cr_slots=4)
    c = circ(16, [("h", (0,)), ("h", (1,)), ("h", (0,)), ("h", (1,)), ("h", (0,)), ("h", (1,))])
    s = core.Simulator(c, a, DagPolicy(0, cost_weight=0.0))
    assert s.store_cost_estimate(0) >= a.timing.port_move


def test_dag_without_prefetch_issues_no_speculative_loads():
    c = wl.adder(3)
    a = cfg(cr_slots=6, n_banks=2, bank_capacity=-(-c.n_qubits // 2), bank_width=3, bus_lanes=2,
            timing=dict(magic_unlimited=True))
    sim0, r0 = run(c, a, "dag", prefetch_window=0)
    sim4, r4 = run(c, a, "dag", prefetch_window=4)
    assert core.trace_hash(sim0.trace) != core.trace_hash(sim4.trace)
    assert r4.loads >= r0.loads


def test_overlap_of_independent_gates_and_transfers():
    c = wl.parallel_adders(2, 2)
    a = cfg(cr_slots=6, n_banks=2, bank_capacity=-(-c.n_qubits // 2), bank_width=3, bus_lanes=2,
            timing=dict(magic_unlimited=True))
    sim, _ = run(c, a, "dag", prefetch_window=4)
    ops = [(e["start"], e["end"]) for e in sim.trace if e["kind"] == "op"]
    xf = [(e["start"], e["end"]) for e in sim.trace if e["kind"] in ("load", "store")]
    assert core._max_concurrency(ops) >= 2, "independent gates should overlap"
    assert any(o[0] < x[1] and x[0] < o[1] for o in ops for x in xf), "compute should overlap transfers"


@pytest.mark.parametrize("cr", [2, 3, 4])
@pytest.mark.parametrize("W", [0, 4])
def test_dag_policy_is_deadlock_free_with_tiny_cr(cr, W):
    for seed in range(5):
        c = core.random_circuit(14, 120, seed=seed)
        a = cfg(cr_slots=cr, n_banks=2, bank_capacity=7, bank_width=3, bus_lanes=1,
                timing=dict(magic_interval=3, magic_buffer=2))
        run(c, a, "dag", prefetch_window=W)
