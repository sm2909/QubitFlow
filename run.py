"""run.py -- configure and run baseline experiments.  Usage:
    python run.py                      # run EXPERIMENT below
    python run.py --json cfg.json      # same schema, from a file
    python run.py --no-validate        # skip the trace checker (faster sweeps)
Edit EXPERIMENT; everything else is plumbing.
"""
import argparse, csv, itertools, json, math
import qf_core as core
from qf_policies import make_policy

EXPERIMENT = {
    # ---- workloads: kind in {adder, random, qasm, mqt}
    "circuits": [
        {"kind": "adder", "n": 8},
        {"kind": "random", "n": 40, "ops": 400, "seed": 0},
        # {"kind": "qasm", "path": "my.qasm"},
        # {"kind": "mqt", "name": "qft", "n": 12},
    ],
    # ---- base architecture (any ArchConfig field). bank_capacity=None -> smallest that fits.
    "arch": {"d": 11, "cr_slots": 8, "n_banks": 2, "bank_capacity": None, "bus_lanes": 1,
             "bank_width": 8, "max_parallel_ops": 4},
    # ---- timing overrides (any Timing field), e.g. {"xfer_per_depth": 2, "t_T": 3}
    "timing": {},
    # ---- cartesian sweep over any arch/timing field; key prefix "timing." for Timing fields
    "sweep": {"cr_slots": [4, 8, 12], "n_banks": [1, 2]},
    # ---- (policy name, params) from qf_policies.POLICIES
    "policies": [("lsqca_lazy", {}), ("fixed_prefetch", {"lookahead": 8}),
                 ("fixed_belady", {"lookahead": 8})],
    "validate": True,
    "csv": "results.csv",          # or None
}


def build_circuit(c):
    k = c["kind"]
    if k == "adder":  return core.adder(c["n"])
    if k == "random": return core.random_circuit(c["n"], c["ops"], seed=c.get("seed", 0))
    if k == "qasm":   return core.from_qasm(c["path"])
    if k == "mqt":    return core.from_mqt_bench(c["name"], c["n"])
    raise ValueError(k)


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
                rows.append(res)
                print(f"  {str(point):42s} {res['policy']:24s} cycles={res['cycles']:7d} "
                      f"xfers={res['transfers']:6d} wait={res['mem_wait_cycles']:6d} "
                      f"phys={res['physical_qubits']}")
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