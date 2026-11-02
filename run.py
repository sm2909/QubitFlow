"""Reproducible QubitFlow scheduling experiments.

Run: python run.py
Outputs raw CSV rows and a compact per-workload summary. All policy comparisons
within an architecture use identical capacities, timing, and bank geometry.
"""
import argparse
import csv
import itertools
import json
import math
import os
import qf_core as core
from qf_policies import make_policy

EXPERIMENT = {
    "circuits": [
        {"kind": "qasm", "path": "benchmarks/adder_n10.qasm", "tag": "qasmb_adder10"},
        {"kind": "qasm", "path": "benchmarks/bigadder_n18.qasm", "tag": "qasmb_bigadder18"},
        {"kind": "qasm", "path": "benchmarks/multiplier_n15.qasm", "tag": "qasmb_multiplier15"},
    ],
    "arch": {
        "d": 11, "cr_slots": 4, "n_banks": 2, "bank_capacity": 32,
        "bank_width": 4, "max_parallel_ops": 4, "sam_model": "line",
        "line_move_per_row": 1,
    },
    "timing": {
        "t_2q": 1, "t_h": 3, "t_s": 2, "t_t": 3,
        "magic_interval": 15, "magic_factories": 1,
    },
    "sweep": {"cr_slots": [2, 4, 8], "n_banks": [1, 2, 4]},
    "policies": [
        ("lsqca_lazy", {}),
        ("fixed_prefetch", {"lookahead": 2}),
        ("fixed_prefetch", {"lookahead": 8}),
        ("fixed_belady", {"lookahead": 2}),
        ("fixed_belady", {"lookahead": 8}),
        ("dag_no_prefetch", {}),
        ("joint", {"prefetch_width": 2}),
        ("joint", {"prefetch_width": 4}),
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
        if key.startswith("timing."):
            timing[key.removeprefix("timing.")] = value
        else:
            arch[key] = value
    return core.ArchConfig(**arch, timing=core.Timing(**timing))


def sweep_points(sweep):
    keys = list(sweep)
    return [dict(zip(keys, values))
            for values in itertools.product(*(sweep[k] for k in keys))] or [{}]


def run(experiment, validate=True):
    rows = []
    for spec in experiment["circuits"]:
        circuit = build_circuit(spec)
        stats = circuit.stats()
        print(f"\n== {spec.get('tag', circuit.name)}: {stats}")
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
                            f"invalid schedule ({circuit.name}, {policy.name}, {point}): {errors[:10]}"
                        )
                result.update(point)
                result.update({
                    "workload_tag": spec.get("tag", circuit.name),
                    "n_qubits": circuit.n_qubits,
                    "n_ops": len(circuit.ops),
                    "gate_counts": json.dumps(stats, sort_keys=True),
                    "sam_model": cfg.sam_model,
                    "line_move_per_row": cfg.line_move_per_row,
                    "bus_lanes": cfg.bus_lanes,
                    "max_parallel_ops": cfg.max_parallel_ops,
                    "magic_factories": cfg.timing.magic_factories,
                    "magic_interval": cfg.timing.magic_interval,
                    "magic_buffer": cfg.timing.magic_buffer,
                    "policy_params": json.dumps(params, sort_keys=True),
                    "model_status": "abstract_line_sam_scheduling_model",
                })
                rows.append(result)
                print(f"  {str(point):24s} {result['policy']:24s} "
                      f"cycles={result['cycles']:7d} transfers={result['transfers']:5d} "
                      f"MSF_blocked={result['msf_stall_cycles']:5d}")
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
    if args.json:
        with open(args.json, encoding="utf-8") as handle:
            experiment = json.load(handle)
    else:
        experiment = EXPERIMENT
    run(experiment, validate=experiment.get("validate", True) and not args.no_validate)
