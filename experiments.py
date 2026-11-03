"""Reproducible controlled experiment suites.

    python experiments.py --suite all --out results/          # full study
    python experiments.py --suite policy --quick --out /tmp/r # tiny workloads (CI / sanity)

Guarantees
  * every policy in a configuration is run on the *same* Circuit object and the *same* ArchConfig
    (fingerprints are stored and asserted equal);
  * every run is executed TWICE and the schedule hashes must match (determinism check);
  * every schedule is validated by the independent checker (qf_core.validate_schedule);
  * the CSV contains no wall-clock data, so re-running yields byte-identical files;
  * <suite>.manifest.json records the full spec, seeds, environment and git commit.
Suites: baseline | policy | ablation | msf | seeds   (see docs/EXPERIMENTS.md)
"""
import argparse, csv, itertools, json, math, os, platform, subprocess, sys
import qf_core as core
import qf_workloads as wl
from qf_policies import make_policy

# ---------------------------------------------------------------- workloads
FULL = {
    "adder_small":  {"kind": "adder", "n": 4},
    "adder_large":  {"kind": "adder", "n": 16},
    "multiplier":   {"kind": "multiplier", "n": 5},
    "qft":          {"kind": "qft", "n": 16, "max_k": 6},
    "high_parallel": {"kind": "parallel_adders", "k": 6, "n": 4},
}
QUICK = {
    "adder_small":  {"kind": "adder", "n": 3},
    "adder_large":  {"kind": "adder", "n": 6},
    "multiplier":   {"kind": "multiplier", "n": 2},
    "qft":          {"kind": "qft", "n": 6, "max_k": 4},
    "high_parallel": {"kind": "parallel_adders", "k": 3, "n": 2},
}

# policy ids -> (registry name, params)
FIXED = {
    "demand_lru":         ("demand_lru", {}),
    "demand_belady":      ("demand_belady", {}),
    "prefetch_lru_W4":    ("prefetch_lru", {"lookahead": 4}),
    "prefetch_belady_W4": ("prefetch_belady", {"lookahead": 4}),
}
DAG = {
    "dag_demand": ("dag", {"prefetch_window": 0}),
    "dag_W4":     ("dag", {"prefetch_window": 4}),
}
FIXED_IDS = list(FIXED)


def make_arch(circ, cr, banks, magic_unlimited=False, magic_interval=15, **over):
    """Architecture used for ALL policies of a configuration. Square-ish banks as in the paper
    (L x L or L x (L+1)); one bus lane per bank; paper timing preset."""
    cap = math.ceil(circ.n_qubits / banks)
    timing = core.Timing.lsqca_paper(magic_unlimited=magic_unlimited, magic_interval=magic_interval,
                                     **over.pop("timing", {}))
    kw = dict(d=11, cr_slots=cr, n_banks=banks, bank_capacity=cap,
              bank_width=max(1, math.ceil(math.sqrt(cap))), bus_lanes=banks,
              max_parallel_ops=4, timing=timing, sam_model="line")
    kw.update(over)
    return core.ArchConfig(**kw)


# ---------------------------------------------------------------- one run (x2 for determinism)
def run_one(circ, cfg, pname, pparams, validate=True):
    out = []
    for _ in range(2):
        sim = core.Simulator(circ, cfg, make_policy(pname, **pparams))
        res = sim.run()
        errs = core.validate_schedule(sim.trace, circ, cfg, sim.initial_loc) if validate else []
        out.append((res, core.trace_hash(sim.trace), errs, sim.policy))
    (r1, h1, e1, pol), (r2, h2, e2, _) = out
    if e1 or e2:
        raise AssertionError(f"INVALID schedule: {pol.name} on {circ.name}: {(e1 or e2)[:3]}")
    if h1 != h2 or r1.asdict() != r2.asdict():
        raise AssertionError(f"NON-DETERMINISTIC: {pol.name} on {circ.name}")
    return r1, h1, pol


def _flat(prefix, d):
    return {f"{prefix}{k}": v for k, v in d.items()}


def record(suite, wname, wspec, circ, cfg, pid, pol, res, thash, extra=None):
    T = cfg.timing
    row = dict(suite=suite, workload=wname, workload_spec=json.dumps(wspec, sort_keys=True),
               seed=wspec.get("seed", ""), n_qubits=circ.n_qubits, n_ops=len(circ.ops),
               circuit_fp=circ.fingerprint(), arch_fp=cfg.fingerprint(),
               cr_slots=cfg.cr_slots, n_banks=cfg.n_banks, bank_capacity=cfg.bank_capacity,
               bank_width=cfg.bank_width, bus_lanes=cfg.bus_lanes, max_parallel_ops=cfg.max_parallel_ops,
               code_distance=cfg.d, sam_model=cfg.sam_model,
               magic_unlimited=T.magic_unlimited, magic_interval=T.magic_interval, magic_buffer=T.magic_buffer,
               t_1q=T.t_1q, t_2q=T.t_2q, t_T=T.t_T, t_h=T.t_h, t_s=T.t_s,
               line_shift=T.line_shift, port_move=T.port_move,
               policy_id=pid, policy_name=pol.name, policy_params=json.dumps(pol.spec, sort_keys=True),
               cycles=res.cycles, loads=res.loads, stores=res.stores, transfers=res.transfers,
               mem_wait_cycles=res.mem_wait_cycles, exec_busy_cycles=res.exec_busy_cycles,
               msf_stall_cycles=res.msf_stall_cycles, msf_stalled_ops=res.msf_stalled_ops,
               xfer_busy_cycles=res.xfer_busy_cycles, shift_cycles=res.shift_cycles,
               physical_qubits=res.physical_qubits, schedule_hash=thash, deterministic=True, valid=True)
    row.update(extra or {})
    return row


def _grid(suite, workloads, crs, banks, magics, policies, extra_fn=None, log=print):
    rows = []
    for wname, wspec in workloads.items():
        circ = wl.build_workload(wspec)                       # built ONCE per workload
        for cr, nb, mg in itertools.product(crs, banks, magics):
            cfg = make_arch(circ, cr, nb, magic_unlimited=mg.get("unlimited", False),
                            magic_interval=mg.get("interval", 15))       # built ONCE per config
            fp = (circ.fingerprint(), cfg.fingerprint())
            for pid, (pn, pp) in policies.items():
                res, h, pol = run_one(circ, cfg, pn, pp)
                assert (circ.fingerprint(), cfg.fingerprint()) == fp
                rows.append(record(suite, wname, wspec, circ, cfg, pid, pol, res, h,
                                   extra_fn(mg) if extra_fn else None))
            log(f"  [{suite}] {wname:14s} CR={cr} banks={nb} {mg}  ({len(policies)} policies)")
    return rows


MAGIC_FIN, MAGIC_UNL = {"unlimited": False}, {"unlimited": True}


def suite_baseline(wls, log):
    """Item 8: demand/prefetch x LRU/Belady, W sweep, finite + unlimited magic states."""
    pol = {"demand_lru": FIXED["demand_lru"], "demand_belady": FIXED["demand_belady"]}
    for W in (1, 2, 4, 8):
        pol[f"prefetch_lru_W{W}"] = ("prefetch_lru", {"lookahead": W})
        pol[f"prefetch_belady_W{W}"] = ("prefetch_belady", {"lookahead": W})
    return _grid("baseline", wls, (4, 8), (2,), (MAGIC_FIN, MAGIC_UNL), pol,
                 lambda mg: {"magic_mode": "unlimited" if mg["unlimited"] else "finite"}, log)


def suite_policy(wls, log):
    """Item 10: main comparison. CR in {2,4}, banks in {1,2,4}, finite and unlimited magic."""
    pol = {**FIXED, **DAG}
    return _grid("policy", wls, (2, 4), (1, 2, 4), (MAGIC_FIN, MAGIC_UNL), pol,
                 lambda mg: {"magic_mode": "unlimited" if mg["unlimited"] else "finite"}, log)


def suite_ablation(wls, log):
    """DAG scheduler design ablation (CR=4, banks=2, unlimited magic to expose memory effects)."""
    pol = {}
    for W in (0, 2, 4, 8):
        for cw in (0.0, 0.25, 1.0):
            pol[f"dag_W{W}_cw{cw}"] = ("dag", {"prefetch_window": W, "cost_weight": cw})
    for urg in ("level", "crit"):
        pol[f"dag_W4_urg{urg}"] = ("dag", {"prefetch_window": 4, "urgency": urg})
    for tol in (4, 16, 64):
        pol[f"dag_W4_tol{tol}"] = ("dag", {"prefetch_window": 4, "crit_tol": tol})
    pol["dag_W4_noevict"] = ("dag", {"prefetch_window": 4, "prefetch_evict": False})
    pol["dag_W8_noevict"] = ("dag", {"prefetch_window": 8, "prefetch_evict": False})
    return _grid("ablation", wls, (2, 4), (2,), (MAGIC_UNL,), pol,
                 lambda mg: {"magic_mode": "unlimited"}, log)


def suite_msf(wls, log):
    """When does memory latency become visible? Sweep factory interval (beats per magic state)."""
    pol = {k: FIXED[k] for k in ("demand_lru", "prefetch_belady_W4")}
    pol.update(DAG)
    rows = []
    for itv in (1, 2, 4, 8, 15):
        rows += _grid("msf", wls, (4,), (2,), ({"unlimited": False, "interval": itv},), pol,
                      lambda mg: {"magic_mode": "finite"}, log)
    return rows


def suite_seeds(quick, log):
    """Seed variability on random Clifford+T workloads."""
    n, ops = (12, 150) if quick else (40, 800)
    wls = {f"random_s{s}": {"kind": "random", "n": n, "ops": ops, "seed": s} for s in range(5)}
    pol = {**FIXED, **DAG}
    return _grid("seeds", wls, (4,), (2,), (MAGIC_UNL,), pol,
                 lambda mg: {"magic_mode": "unlimited"}, log)


SUITES = ["baseline", "policy", "ablation", "msf", "seeds"]


def env_info():
    def sh(*a):
        try:
            return subprocess.check_output(a, stderr=subprocess.DEVNULL, text=True).strip()
        except Exception:
            return None
    ver = {}
    for m in ("qiskit", "numpy"):
        try:
            ver[m] = __import__(m).__version__
        except Exception:
            ver[m] = None
    return dict(python=sys.version.split()[0], platform=platform.platform(),
                git_commit=sh("git", "rev-parse", "HEAD"), git_dirty=bool(sh("git", "status", "--porcelain", "--", ".", ":!results")),
                packages=ver)


def run_suite(name, quick=False, out="results", log=print):
    wls = QUICK if quick else FULL
    fn = {"baseline": suite_baseline, "policy": suite_policy, "ablation": suite_ablation,
          "msf": suite_msf}.get(name)
    rows = suite_seeds(quick, log) if name == "seeds" else fn(wls, log)
    os.makedirs(out, exist_ok=True)
    cols = list(dict.fromkeys(k for r in rows for k in r))
    path = os.path.join(out, f"{name}.csv")
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, lineterminator="\n")
        w.writeheader(); w.writerows(rows)
    manifest = dict(suite=name, quick=quick, n_runs=len(rows), workloads=wls if name != "seeds" else "see CSV",
                    timing_preset="Timing.lsqca_paper", sam_model="line", environment=env_info(),
                    notes="every run executed twice (hash-checked) and validated; no wall-clock data in CSV")
    with open(os.path.join(out, f"{name}.manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True, default=str)
    log(f"wrote {path} ({len(rows)} runs)")
    return rows


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--suite", default="all", choices=SUITES + ["all"])
    ap.add_argument("--out", default="results")
    ap.add_argument("--quick", action="store_true", help="tiny workloads for sanity checks")
    a = ap.parse_args()
    for s in (SUITES if a.suite == "all" else [a.suite]):
        run_suite(s, a.quick, a.out)
