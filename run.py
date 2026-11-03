"""Reproducible QubitFlow experiment suite.

Run from the repository root:
    python run.py
    python analyze_results.py

The primary comparisons are matched within workload/architecture groups.
Raw event traces are emitted for selected application workloads and policies.
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import itertools
import json
import os
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

import qf_core as core
from qf_policies import make_policy


POLICIES = [
    ("lsqca_lazy", {}),
    ("fixed_prefetch", {"lookahead": 1}),
    ("fixed_prefetch", {"lookahead": 2}),
    ("fixed_prefetch", {"lookahead": 4}),
    ("fixed_prefetch", {"lookahead": 8}),
    ("fixed_farthest_next_use", {"lookahead": 0}),
    ("dag_lru", {}),
    ("dag_next_use", {}),
    ("dag_next_use_nearest", {}),
    ("joint", {"prefetch_width": 1}),
    ("joint", {"prefetch_width": 2}),
    ("joint", {"prefetch_width": 4}),
    ("joint", {"prefetch_width": 2, "transfer_priority": "nearest"}),
]

CIRCUITS = [
    {
        "kind": "adder", "n": 2, "tag": "cuccaro_adder2",
        "workload_class": "synthetic_arithmetic", "source": "QubitFlow synthetic Cuccaro-style adder",
    },
    {
        "kind": "adder", "n": 8, "tag": "cuccaro_adder8",
        "workload_class": "synthetic_arithmetic", "source": "QubitFlow synthetic Cuccaro-style adder",
    },
    {
        "kind": "qasm", "path": "benchmarks/qasmb_adder_n28.qasm",
        "tag": "qasmb_adder_n28", "workload_class": "qasmbench_arithmetic",
        "source": "PNNL QASMBench large/adder_n28",
    },
    {
        "kind": "qasm", "path": "benchmarks/qasmb_multiplier_n15.qasm",
        "tag": "qasmb_multiplier_n15", "workload_class": "qasmbench_arithmetic",
        "source": "PNNL QASMBench medium/multiplier_n15",
    },
    {
        "kind": "qasm", "path": "benchmarks/qasmb_adder_n118.qasm",
        "tag": "qasmb_adder_n118", "workload_class": "qasmbench_arithmetic",
        "source": "PNNL QASMBench large/adder_n118",
    },
    {
        "kind": "qasm", "path": "benchmarks/qasmb_multiplier_n45.qasm",
        "tag": "qasmb_multiplier_n45", "workload_class": "qasmbench_arithmetic",
        "source": "PNNL QASMBench large/multiplier_n45",
    },
    {
        "kind": "qft_topology", "n": 12, "tag": "synthetic_qft_topology_n12",
        "workload_class": "synthetic_topology_stress",
        "source": "QubitFlow controlled-interaction topology generator; not a gate-equivalent QFT",
    },
    *[
        {
            "kind": "random", "n": 24, "ops": 180, "seed": seed,
            "tag": f"random24x180_seed{seed}",
            "workload_class": "synthetic_random_stress",
            "source": f"QubitFlow seeded random circuit, seed={seed}",
        }
        for seed in (11, 17, 23)
    ],
]

# A fixed per-bank capacity is used for every policy/configuration comparison.
# 144 cells in a 12x12 array permits all bundled workloads, including 118 qubits,
# to fit in the SAM even in the one-bank architecture point.
ARCH_BASE = {
    "d": 11,
    "cr_slots": 4,
    "n_banks": 2,
    "bank_capacity": 144,
    "bank_width": 12,
    "bus_lanes": None,
    "max_parallel_ops": 4,
    "sam_model": "line",
    "line_access_cost": 0,
    "line_move_per_row": 1,
    "line_initial_row": 0,
}
ARCH_SWEEP = {
    "cr_slots": [2, 4, 8],
    "n_banks": [1, 2, 4],
    "line_access_cost": [0, 1, 2],
}
TWO_QUBIT_TIMINGS = [1, 2, 4]
TRACE_WORKLOADS = {"qasmb_adder_n28", "qasmb_multiplier_n15"}
TRACE_POLICIES = {
    "lsqsa_lazy",
    "dag_lru",
    "dag_next_use",
    "dag_next_use_nearest",
    "joint-prefetch(W=2,critical)",
}
TRACE_CONFIG = {"cr_slots": 4, "n_banks": 2, "line_access_cost": 1}


def build_circuit(spec):
    if spec["kind"] == "adder":
        return core.adder(spec["n"])
    if spec["kind"] == "qft_topology":
        return core.qft(spec["n"])
    if spec["kind"] == "random":
        return core.random_circuit(spec["n"], spec["ops"], seed=spec["seed"])
    if spec["kind"] == "qasm":
        return core.from_qasm(spec["path"])
    raise ValueError(f"unknown circuit kind: {spec['kind']}")


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_cfg(circuit, point):
    arch = dict(ARCH_BASE)
    timing_overrides = {
        key.removeprefix("timing."): value
        for key, value in point.items()
        if key.startswith("timing.")
    }
    arch.update({k: v for k, v in point.items() if not k.startswith("timing.")})
    if (circuit.n_qubits + arch["n_banks"] - 1) // arch["n_banks"] > arch["bank_capacity"]:
        raise ValueError("selected architecture has insufficient aggregate SAM capacity")
    return core.ArchConfig(**arch, timing=core.Timing(**timing_overrides))


def run_one(spec, circuit, cfg, policy_name, params, axis, point,
            initially_resident=False):
    policy = make_policy(policy_name, **params)
    sim = core.Simulator(
        circuit, cfg, policy, initially_resident=initially_resident
    )
    result = sim.run().asdict()
    errors = core.validate_schedule(sim.trace, circuit, cfg, sim.initial_loc)
    if errors:
        raise AssertionError(
            f"invalid trace {spec['tag']} {point} {policy.name}: {errors[:10]}"
        )

    qasm_path = spec.get("path")
    row = {
        **result,
        "workload_tag": spec["tag"],
        "workload_class": spec["workload_class"],
        "benchmark_source": spec["source"],
        "source_path": qasm_path or "",
        "source_sha256": sha256_file(qasm_path) if qasm_path else "",
        "seed": spec.get("seed", ""),
        "n_qubits": circuit.n_qubits,
        "n_ops": len(circuit.ops),
        "gate_counts": json.dumps(circuit.stats(), sort_keys=True),
        "experiment_axis": axis,
        "cr_slots": cfg.cr_slots,
        "n_banks": cfg.n_banks,
        "bank_capacity": cfg.bank_capacity,
        "bank_width": cfg.bank_width,
        "bus_lanes": cfg.bus_lanes,
        "sam_model": cfg.sam_model,
        "line_initial_row": cfg.line_initial_row,
        "line_access_cost": cfg.line_access_cost,
        "line_move_per_row": cfg.line_move_per_row,
        "d": cfg.d,
        "max_parallel_ops": cfg.max_parallel_ops,
        "t_h": cfg.timing.t_h,
        "t_s": cfg.timing.t_s,
        "t_pauli": cfg.timing.t_pauli,
        "t_measure": cfg.timing.t_measure,
        "t_parity_measure": cfg.timing.t_parity_measure,
        "t_t_macro": cfg.timing.t_t,
        "t_2q": cfg.timing.t_2q,
        "magic_factories": cfg.timing.magic_factories,
        "magic_interval": cfg.timing.magic_interval,
        "magic_buffer": cfg.timing.magic_buffer,
        "policy_params": json.dumps(params, sort_keys=True),
        "python_version": platform.python_version(),
        "source_revision": os.environ.get("GITHUB_SHA", "unknown"),
    }
    row["comparison_group"] = (
        f"{spec['tag']}:{axis}:CR{cfg.cr_slots}:B{cfg.n_banks}:"
        f"access{cfg.line_access_cost}:t2q{cfg.timing.t_2q}"
    )
    return sim, row


def write_trace(path, spec, cfg, policy_name, params, sim, result):
    path.parent.mkdir(parents=True, exist_ok=True)
    header = {
        "record_type": "metadata",
        "workload": spec["tag"],
        "source": spec["source"],
        "policy_key": policy_name,
        "policy_params": params,
        "architecture": {
            "cr_slots": cfg.cr_slots,
            "n_banks": cfg.n_banks,
            "bank_capacity": cfg.bank_capacity,
            "bank_width": cfg.bank_width,
            "bus_lanes": cfg.bus_lanes,
            "line_access_cost": cfg.line_access_cost,
            "line_initial_row": cfg.line_initial_row,
        },
        "timing": {
            "h": cfg.timing.t_h, "s": cfg.timing.t_s,
            "pauli": cfg.timing.t_pauli, "measure": cfg.timing.t_measure,
            "t_macro": cfg.timing.t_t, "two_qubit": cfg.timing.t_2q,
            "magic_factories": cfg.timing.magic_factories,
            "magic_interval": cfg.timing.magic_interval,
            "magic_buffer": cfg.timing.magic_buffer,
        },
        "result": result.asdict(),
        "validation_errors": core.validate_schedule(
            sim.trace, sim.circuit, cfg, sim.initial_loc
        ),
    }
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write(json.dumps(header, sort_keys=True) + "\n")
        for event in sim.trace:
            handle.write(json.dumps({"record_type": "event", **event},
                                    sort_keys=True) + "\n")


def main():
    run_started = datetime.now(timezone.utc).isoformat()
    rows = []
    built = {}

    for spec in CIRCUITS:
        circuit = build_circuit(spec)
        built[spec["tag"]] = circuit
        print(f"\n== {spec['tag']}: {circuit.stats()}", flush=True)

        for cr_slots, n_banks, access_cost in itertools.product(
            ARCH_SWEEP["cr_slots"], ARCH_SWEEP["n_banks"],
            ARCH_SWEEP["line_access_cost"],
        ):
            point = {
                "cr_slots": cr_slots,
                "n_banks": n_banks,
                "line_access_cost": access_cost,
            }
            cfg = build_cfg(circuit, point)
            for policy_name, params in POLICIES:
                sim, row = run_one(
                    spec, circuit, cfg, policy_name, params,
                    "architecture_and_access_sensitivity", point,
                )
                rows.append(row)
                print(
                    f"  CR={cr_slots} B={n_banks} access={access_cost} "
                    f"{row['policy']:34s} cycles={row['cycles']}",
                    flush=True,
                )

            # All-qubits-in-CR is a lower-bound reference, not a policy result.
            if circuit.n_qubits <= cr_slots:
                _, row = run_one(
                    spec, circuit, cfg, "dag_lru", {},
                    "architecture_and_access_sensitivity", point,
                    initially_resident=True,
                )
                row["policy"] = "all_qubits_in_CR_lower_bound"
                row["policy_params"] = "{}"
                row["comparison_group"] += ":lower-bound"
                rows.append(row)

        # Preserve raw traces for a small, pre-registered representative set.
        if spec["tag"] in TRACE_WORKLOADS:
            cfg = build_cfg(circuit, TRACE_CONFIG)
            for policy_name, params in POLICIES:
                policy = make_policy(policy_name, **params)
                if policy.name not in TRACE_POLICIES:
                    continue
                sim = core.Simulator(circuit, cfg, policy)
                result = sim.run()
                errors = core.validate_schedule(
                    sim.trace, circuit, cfg, sim.initial_loc
                )
                if errors:
                    raise AssertionError(f"invalid trace artifact: {errors[:10]}")
                safe_policy = policy.name.replace("/", "_").replace(" ", "_")
                write_trace(
                    Path("traces") / f"{spec['tag']}__{safe_policy}.jsonl.gz",
                    spec, cfg, policy_name, params, sim, result,
                )

    # Isolate CNOT-duration sensitivity at fixed CR/bank/Line-SAM parameters.
    for spec in CIRCUITS:
        circuit = built[spec["tag"]]
        for t_2q in TWO_QUBIT_TIMINGS:
            point = {
                "cr_slots": 4,
                "n_banks": 2,
                "line_access_cost": 1,
                "timing.t_2q": t_2q,
            }
            cfg = build_cfg(circuit, point)
            for policy_name, params in POLICIES:
                _, row = run_one(
                    spec, circuit, cfg, policy_name, params,
                    "two_qubit_timing_sensitivity", point,
                )
                rows.append(row)

    if not rows:
        raise RuntimeError("experiment suite produced no rows")
    with open("results.csv", "w", newline="", encoding="utf-8") as handle:
        fields = list(dict.fromkeys(k for row in rows for k in row))
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    tracked_files = [
        "qf_core.py", "qf_policies.py", "run.py", "analyze_results.py",
        *[spec["path"] for spec in CIRCUITS if spec.get("path")],
    ]
    manifest = {
        "schema_version": 1,
        "run_started_utc": run_started,
        "run_finished_utc": datetime.now(timezone.utc).isoformat(),
        "source_revision": os.environ.get("GITHUB_SHA", "unknown"),
        "python_version": sys.version,
        "platform": platform.platform(),
        "architecture_base": ARCH_BASE,
        "architecture_sweep": ARCH_SWEEP,
        "two_qubit_timing_sweep": TWO_QUBIT_TIMINGS,
        "policies": [{"key": key, "params": params} for key, params in POLICIES],
        "workloads": [
            {
                **{k: v for k, v in spec.items() if k != "path"},
                "source_path": spec.get("path", ""),
                "source_sha256": sha256_file(spec["path"]) if spec.get("path") else "",
                "qubits": built[spec["tag"]].n_qubits,
                "operations": len(built[spec["tag"]].ops),
                "gate_counts": built[spec["tag"]].stats(),
            }
            for spec in CIRCUITS
        ],
        "code_and_input_sha256": {
            path: sha256_file(path) for path in tracked_files if Path(path).exists()
        },
        "trace_workloads": sorted(TRACE_WORKLOADS),
        "trace_policies": sorted(TRACE_POLICIES),
        "trace_config": TRACE_CONFIG,
        "row_count": len(rows),
    }
    with open("experiment_manifest.json", "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")

    print(
        f"\nWrote {len(rows)} rows to results.csv, experiment_manifest.json, "
        "and representative traces/",
        flush=True,
    )
    return rows


if __name__ == "__main__":
    main()
