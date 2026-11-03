"""Line-SAM model (item 6). Checks against statements in LSQCA (Kobori et al., HPCA'25,
arXiv:2412.20486), Sec. IV-C.3 and V-B:
  [P1] "The latency of the load operations in the line SAM equals the y-axis distance H,
        which is 0.5*sqrt(n) in the worst case."
  [P2] "...the high efficiency of continuous access to data cells on the same line without
        additional operations for cell movement."
  [P3] Locality-aware store: data cells stored sequentially go to the same / neighbouring lines.
  [P4] The scan line / initial position is at the middle of the bank (worst case = half height).
Constants NOT stated in the text (final port move = 1 beat, 1 beat per row shift) are explicit
Timing parameters (port_move, line_shift) -- see docs/ASSUMPTIONS.md."""
import math
import pytest
import qf_core as core
from helpers import cfg, circ, run, ev


def sq_bank(n):                                # L x L bank holding n = L^2 cells
    L = math.isqrt(n)
    return cfg(n_banks=1, bank_capacity=n, bank_width=L, cr_slots=4)


def test_P1_load_latency_is_row_distance_plus_port_move():
    a = cfg(n_banks=1, bank_capacity=16, bank_width=4)        # 4 rows, scan starts at row 2
    assert a.initial_scan_row() == 2
    for cell in range(16):
        r = cell // 4
        assert a.line_cost(2, cell) == abs(r - 2) * a.timing.line_shift + a.timing.port_move


def test_P1_worst_case_is_half_the_height():
    for n in (16, 64, 100, 400):
        a = sq_bank(n)
        L = math.isqrt(n)
        worst = max(abs(a.row_of(c) - a.initial_scan_row()) for c in range(n))
        assert worst == L // 2 + (L % 2 == 1) * 0 or worst == math.ceil(L / 2)   # ~ 0.5 sqrt(n)
        assert abs(worst - 0.5 * math.sqrt(n)) <= 1


def test_P2_same_row_access_needs_no_scan_movement():
    a = cfg(n_banks=1, bank_capacity=16, bank_width=4, bus_lanes=1)
    c = circ(16, [("h", (0,)), ("h", (1,)), ("h", (2,)), ("h", (3,))])    # all in row 0
    sim, _ = run(c, a, "prefetch_lru", lookahead=3)
    loads = sorted(ev(sim, "load"), key=lambda e: e["start"])
    shifts = [e["shift"] for e in loads]
    assert shifts[0] == 2 and shifts[1:] == [0, 0, 0]
    assert all(e["end"] - e["start"] == a.timing.port_move for e in loads[1:])


def test_P2_repeated_row_beats_alternating_rows():
    a = cfg(n_banks=1, bank_capacity=16, bank_width=4, cr_slots=2, bus_lanes=1,
            timing=dict(magic_unlimited=True))
    same = circ(16, [("h", (q,)) for q in (0, 1, 2, 3, 0, 1, 2, 3)])           # row 0 only
    alt = circ(16, [("h", (q,)) for q in (0, 12, 1, 13, 2, 14, 3, 15)])        # rows 0 and 3
    _, r_same = run(same, a)
    _, r_alt = run(alt, a)
    assert r_same.shift_cycles < r_alt.shift_cycles
    assert r_same.cycles < r_alt.cycles


def test_scan_state_persists_between_transfers():
    a = cfg(n_banks=1, bank_capacity=16, bank_width=4, bus_lanes=1)
    s = core.Simulator(circ(16, [("h", (0,))]), a, __import__("qf_policies").make_policy("demand_lru"))
    assert s.scan_row == [2]
    s.try_load(12)                                  # row 3
    assert s.scan_row == [3]
    s.t = s.xfers[0]["end"]; s._complete()
    s.try_load(0)                                   # row 0 : shift 3
    t = s.trace[-1]
    assert (t["scan_before"], t["row"], t["shift"]) == (3, 0, 3)
    assert t["end"] - t["start"] == 3 * a.timing.line_shift + a.timing.port_move


def test_different_rows_cost_more_than_same_row():
    a = cfg(n_banks=1, bank_capacity=16, bank_width=4)
    costs = {row: a.line_cost(0, row * 4) for row in range(4)}
    assert costs == {0: 1, 1: 2, 2: 3, 3: 4}


def test_P3_locality_aware_store_prefers_row_at_scan_line():
    a = cfg(n_banks=1, bank_capacity=16, bank_width=4, cr_slots=4, bus_lanes=1)
    s = core.Simulator(circ(16, [("h", (0,))]), a, __import__("qf_policies").make_policy("demand_lru"))
    s.try_load(0); s.t = s.xfers[0]["end"]; s._complete()      # opens a hole in row 0, scan at row 0
    s.try_load(1); s.t = s.xfers[0]["end"]; s._complete()      # same row, scan stays at row 0
    assert s.scan_row == [0]
    s.try_store(0)
    st = s.trace[-1]
    assert a.row_of(st["cell"]) == 0 and st["shift"] == 0, "store should reuse the hole in the scan row"
    assert st["end"] - st["start"] == a.timing.port_move


def test_store_falls_back_to_nearest_row_with_space():
    a = cfg(n_banks=1, bank_capacity=8, bank_width=4, cr_slots=2)     # 2 rows, 8 qubits: full
    s = core.Simulator(circ(8, [("h", (0,))]), a, __import__("qf_policies").make_policy("demand_lru"))
    s.try_load(5); s.t = s.xfers[0]["end"]; s._complete()             # hole in row 1
    s.try_load(0); s.t = s.xfers[0]["end"]; s._complete()             # hole in row 0, scan at row 0
    s.try_store(5)
    st = s.trace[-1]
    assert a.row_of(st["cell"]) == 0 and st["shift"] == 0


def test_validator_replays_scan_line_state():
    a = cfg(n_banks=1, bank_capacity=16, bank_width=4, bus_lanes=1)
    c = circ(16, [("h", (0,)), ("h", (12,)), ("h", (0,))])
    sim, _ = run(c, a)
    bad = [dict(e) for e in sim.trace]
    l2 = [e for e in bad if e["kind"] == "load"][1]
    l2["end"] -= 1                                   # a load that is too fast for the scan distance
    assert any("Line-SAM cost" in e for e in core.validate_schedule(bad, c, a, sim.initial_loc))


def test_banks_have_independent_scan_lines():
    a = cfg(n_banks=2, bank_capacity=8, bank_width=2, bus_lanes=2)
    s = core.Simulator(circ(4, [("h", (0,))]), a, __import__("qf_policies").make_policy("demand_lru"))
    s.try_load(2)                                    # q2 -> bank 0, cell 1 (row 0)
    assert s.scan_row[1] == a.initial_scan_row() == 2 and s.scan_row[0] == 0


def test_area_model_counts_scan_line():
    a = cfg(n_banks=2, bank_capacity=16, bank_width=4, factory_tiles=0, bus_tiles_per_lane=0, cr_slots=2)
    sam = 2 * (4 * 4 + 4)                            # 4 data rows + 1 scan line (width 4) per bank
    assert a.tiles() == 2 * 2 + sam
