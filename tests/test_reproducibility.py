"""Determinism and experiment-harness guarantees (item 5)."""
import csv, json, os, subprocess, sys
import qf_core as core
import qf_workloads as wl
import experiments as ex
from qf_policies import make_policy
from helpers import cfg

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_repeated_runs_are_bit_identical():
    c = wl.multiplier(2)
    a = ex.make_arch(c, 4, 2, magic_unlimited=True)
    hs = set()
    for _ in range(3):
        for name, kw in [("prefetch_belady", {"lookahead": 4}), ("dag", {"prefetch_window": 4})]:
            sim = core.Simulator(c, a, make_policy(name, **kw)); sim.run()
            hs.add((name, core.trace_hash(sim.trace)))
    assert len(hs) == 2


def test_results_independent_of_python_hash_seed():
    code = ("import sys; sys.path.insert(0, %r); import qf_core as core, qf_workloads as wl, experiments as ex;"
            "from qf_policies import make_policy; c = wl.qft(7, max_k=4); a = ex.make_arch(c, 4, 2);"
            "s = core.Simulator(c, a, make_policy('dag', prefetch_window=4)); s.run(); print(core.trace_hash(s.trace))") % ROOT
    outs = {subprocess.check_output([sys.executable, "-c", code], env={**os.environ, "PYTHONHASHSEED": str(h)}, text=True)
            for h in (0, 1, 12345)}
    assert len(outs) == 1


def test_same_seed_same_circuit_different_seed_different():
    a = wl.build_workload({"kind": "random", "n": 10, "ops": 80, "seed": 5})
    b = wl.build_workload({"kind": "random", "n": 10, "ops": 80, "seed": 5})
    c = wl.build_workload({"kind": "random", "n": 10, "ops": 80, "seed": 6})
    assert a.fingerprint() == b.fingerprint() != c.fingerprint()


def test_experiment_output_is_byte_reproducible_and_complete(tmp_path):
    quiet = lambda *_: None
    p1, p2 = tmp_path / "a", tmp_path / "b"
    ex.run_suite("seeds", quick=True, out=str(p1), log=quiet)
    ex.run_suite("seeds", quick=True, out=str(p2), log=quiet)
    assert (p1 / "seeds.csv").read_bytes() == (p2 / "seeds.csv").read_bytes()
    rows = list(csv.DictReader(open(p1 / "seeds.csv")))
    need = {"workload", "seed", "n_qubits", "n_ops", "circuit_fp", "arch_fp", "cr_slots", "n_banks",
            "bank_capacity", "bank_width", "bus_lanes", "magic_interval", "magic_buffer", "policy_name",
            "policy_params", "cycles", "loads", "stores", "transfers", "mem_wait_cycles",
            "exec_busy_cycles", "msf_stall_cycles", "physical_qubits", "schedule_hash"}
    assert need <= set(rows[0])
    assert {r["seed"] for r in rows} == {"0", "1", "2", "3", "4"}
    assert all(r["valid"] == "True" and r["deterministic"] == "True" for r in rows)
    m = json.load(open(p1 / "seeds.manifest.json"))
    assert m["n_runs"] == len(rows) and "python" in m["environment"]


def test_all_policies_see_identical_circuit_and_architecture(tmp_path):
    rows = ex.run_suite("policy", quick=True, out=str(tmp_path), log=lambda *_: None)
    groups = {}
    for r in rows:
        groups.setdefault((r["workload"], r["cr_slots"], r["n_banks"], r["magic_mode"]), set()).add(
            (r["circuit_fp"], r["arch_fp"]))
    assert groups and all(len(v) == 1 for v in groups.values())
    assert len({r["policy_id"] for r in rows}) == 6


def test_experiment_runner_detects_invalid_schedule(monkeypatch):
    import pytest
    monkeypatch.setattr(core, "validate_schedule", lambda *a, **k: ["boom"])
    c = wl.adder(2)
    with pytest.raises(AssertionError, match="INVALID"):
        ex.run_one(c, ex.make_arch(c, 4, 1), "demand_lru", {})
