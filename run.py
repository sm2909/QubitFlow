"""Reproducible QubitFlow experiment harness.

Run: python run.py [--json experiment.json] [--no-validate]
The default matrix is intentionally modest. Increase workload sizes after smoke/tests pass.
"""
import argparse
import csv
import itertools
import json
import math
import platform
import random
import qf_core as core
from qf_policies import make_policy

EXPERIMENT = {
    "circuits": [
        {"kind": "adder", "n": 2, "tag": "correctness"},
        {"kind": "adder", "n": 8, "tag": "ripple_adder"},
        {"kind": "qft", "n": 12, "tag": "qft_topology"},
        {"kind": "random", "n": 24, "ops": 180, "seed": 7, "tag": "high_parallelism"},
        # Optional full arithmetic benchmark via MQT Bench:
        # {"kind": "mqt", "name": "multiplier", "n": 8, "tag": "multiplier"},
        # {"kind": "qasm", "path": "benchmarks/example.qasm"},
    ],
    "arch": {"d": 11, "cr_slots": 4, "n_banks": 2, "bank_capacity": None,
             "bus_lanes": 1, "bank_width": 8, "max_parallel_ops": 2,
             "sam_model": "line"},
    "timing": {},
    "sweep": {"cr_slots": [2, 4], "n_banks": [1, 2]},
    "policies": [
        ("lsqca_lazy", {}),
        ("fixed_prefetch", {"lookahead": 1}),
        ("fixed_prefetch", {"lookahead": 2}),
        ("fixed_prefetch", {"lookahead": 4}),
        ("fixed_prefetch", {"lookahead": 8}),
        ("fixed_belady", {"lookahead": 1}),
        ("fixed_belady", {"lookahead": 2}),
        ("fixed_belady", {"lookahead": 4}),
        ("fixed_belady", {"lookahead": 8}),
        ("dag_no_prefetch", {}),
        ("joint", {"prefetch_width": 2}),
    ],
    "validate": True,
    "csv": "results.csv",
}


def build_circuit(spec):
    kind = spec["kind"]
    if kind == "adder":
        return core.adder(spec["n"])
    if kind == "qft":
        return core.qft(spec["n"])
    if kind == "random":
        return core.random_circuit(spec["n"], spec["ops"], seed=spec.get("seed", 0))
    if kind == "qasm":
        return core.from_qasm(spec["path"])
    if kind == "mqt":
        return core.from_mqt_bench(spec["name"], spec["n"])
    raise ValueError(f"unknown circuit kind: {kind}")


def build_cfg(circuit, arch, timing, point):
    arch, timing = dict(arch), dict(timing)
    for key, value in point.items():
        (timing if key.startswith("timing.") else arch)[key.removeprefix("timing.")] = value
    if arch.get("bank_capacity") is None:
        arch["bank_capacity"] = math.ceil(circuit.n_qubits / arch["n_banks"])
    return core.ArchConfig(**arch, timing=core.Timing(**timing))


def sweep_points(sweep):
    keys = list(sweep)
    return [dict(zip(keys, values))
            for values in itertools.product(*(sweep[k] for k in keys))] or [{}]


def run(experiment, validate=True):
    rows = []
    for spec in experiment["circuits"]:
        circuit = build_circuit(spec)
        print(f"\n== {circuit.name}: {circuit.stats()}")
        for point in sweep_points(experiment.get("sweep", {})):
            cfg = build_cfg(circuit, experiment["arch"], experiment.get("timing", {}), point)
            for policy_name, params in experiment["policies"]:
                policy = make_policy(policy_name, **params)
                sim = core.Simulator(circuit, cfg, policy)
                result = sim.run().asdict()
                if validate:
                    errors = core.validate_schedule(sim.trace, circuit, cfg, sim.initial_loc)
                    if errors:
                        raise AssertionError(
                            f"invalid schedule ({policy.name}, {point}): {errors[:10]}"
                        )
                result.update(point)
                result["workload_tag"] = spec.get("tag", spec["kind"])
                result["seed"] = spec.get("seed", "")
                result["sam_model"] = cfg.sam_model
                result["prefetch_params"] = json.dumps(params, sort_keys=True)
                rows.append(result)
                print(f"  {str(point):30s} {result['policy']:22s} "
                      f"cycles={result['cycles']:7d} transfers={result['transfers']:5d} "
                      f"mem_wait={result['mem_wait_cycles']:6d} "
                      f"msf_wait={result['msf_stall_cycles']:6d}")
    if experiment.get("csv") and rows:
        columns = list(dict.fromkeys(key for row in rows for key in row))
        with open(experiment["csv"], "w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
        print(f"\nWrote {experiment['csv']}")
    return rows


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--json")
    parser.add_argument("--no-validate", action="store_true")
    args = parser.parse_args()
    experiment = json.load(open(args.json)) if args.json else EXPERIMENT
    run(experiment, validate=experiment.get("validate", True) and not args.no_validate)
