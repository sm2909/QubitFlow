"""run.py -- configure and run baseline experiments.  Usage:
    python run.py                      # run EXPERIMENT below
    python run.py --json cfg.json      # same schema, from a file
    python run.py --no-validate        # skip the trace checker (faster sweeps)
Edit EXPERIMENT; everything else is plumbing.
"""
import argparse, csv, itertools, json, math
import qf_core as core
import qf_workloads as wl
from qf_policies import make_policy

EXPERIMENT = {
    # ---- workloads: kind in {adder, multiplier, qft, parallel_adders, random, qasm, mqt}
    "circuits": [
        {"kind": "adder", "n": 8},
        {"kind": "random", "n": 40, "ops": 400, "seed": 0},
        # {"kind": "qasm", "path": "my.qasm"},
        # {"kind": "mqt", "name": "qft", "n": 12},
    ],
    # ---- base architecture (any ArchConfig field). bank_capacity=None -> smallest that fits.
    "arch": {"d": 11, "cr_slots": 8, "n_banks": 2, "bank_capacity": None, "bus_lanes": 1,
             "bank_width": 4, "max_parallel_ops": 4, "sam_model": "line"},
    # ---- timing overrides (any Timing field), e.g. {"xfer_per_depth": 2, "t_T": 3}
    "timing": {},                 # e.g. {"magic_unlimited": True}; paper preset: see experiments.make_arch
    # ---- cartesian sweep over any arch/timing field; key prefix "timing." for Timing fields
    "sweep": {"cr_slots": [4, 8, 12], "n_banks": [1, 2]},
    # ---- (policy name, params) from qf_policies.POLICIES
    "policies": [("demand_lru", {}), ("demand_belady", {}), ("prefetch_lru", {"lookahead": 8}),
                 ("prefetch_belady", {"lookahead": 8}), ("dag", {"prefetch_window": 4})],
    "validate": True,
    "csv": "results.csv",          # or None
}


def build_circuit(c):
    return wl.build_workload(c)       # adder|multiplier|qft|parallel_adders|random(seed required)|qasm|mqt


def build_cfg(circ, arch, timing, point):
    arch, timing = dict(arch), dict(timing)
    for key, v in point.items():
        (timing if key.startswith("timing.") else arch)[key.removeprefix("timing.")] = v
    if arch.get("bank_capacity") is None:
        arch["bank_capacity"] = math.ceil(circ.n_qubits / arch["n_banks"])
    return core.ArchConfig(**arch, timing=core.Timing(**timing))


def sweep_points(sweep):
    keys = list(sweep)
    return [dict(zip(keys, vals)) for vals in itertools.product(*(sweep[k] for k in keys))] or [{}]


def run(exp, validate=True):
    rows = []
    for cspec in exp["circuits"]:
        circ = build_circuit(cspec)
        print(f"\n== {circ.name}  {circ.stats()}")
        for point in sweep_points(exp.get("sweep", {})):
            cfg = build_cfg(circ, exp["arch"], exp.get("timing", {}), point)
            for pname, params in exp["policies"]:
                sim = core.Simulator(circ, cfg, make_policy(pname, **params))
                res = sim.run().asdict()
                if validate:
                    errs = core.validate_schedule(sim.trace, circ, cfg, sim.initial_loc)
                    assert not errs, f"INVALID schedule ({sim.policy.name}, {point}): {errs[:5]}"
                res.update(point)
                res.update(circuit_fp=circ.fingerprint(), arch_fp=cfg.fingerprint(), n_ops=len(circ.ops),
                           seed=cspec.get("seed", ""), policy_params=json.dumps(sim.policy.spec, sort_keys=True),
                           bank_width=cfg.bank_width, magic_interval=cfg.timing.magic_interval,
                           magic_unlimited=cfg.timing.magic_unlimited, schedule_hash=core.trace_hash(sim.trace))
                rows.append(res)
                print(f"  {str(point):42s} {res['policy']:24s} cycles={res['cycles']:7d} "
                      f"xfers={res['transfers']:6d} wait={res['mem_wait_cycles']:6d} "
                      f"msf_stall={res['msf_stall_cycles']:6d} phys={res['physical_qubits']}")
    if exp.get("csv") and rows:
        cols = list(dict.fromkeys(k for r in rows for k in r))
        with open(exp["csv"], "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(rows)
        print(f"\nwrote {exp['csv']}")
    return rows


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--json"); ap.add_argument("--no-validate", action="store_true")
    a = ap.parse_args()
    exp = json.load(open(a.json)) if a.json else EXPERIMENT
    run(exp, validate=exp.get("validate", True) and not a.no_validate)