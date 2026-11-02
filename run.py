"""Reproducible QubitFlow experiment suite.

Run: python run.py
All policy comparisons use identical hardware within each configuration.
The architecture sweep is reported separately from policy comparisons.
"""
import csv
import itertools
import json
import math
import platform
from collections import defaultdict
import qf_core as core
from qf_policies import make_policy

POLICIES = [
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
]

CIRCUITS = [
    {"kind": "adder", "n": 2, "tag": "cuccaro_adder2_synthetic"},
    {"kind": "adder", "n": 8, "tag": "cuccaro_adder8_synthetic"},
    {"kind": "qasm", "path": "benchmarks/qasmb_adder_n28.qasm",
     "tag": "qasmb_adder_n28"},
    {"kind": "qasm", "path": "benchmarks/qasmb_multiplier_n15.qasm",
     "tag": "qasmb_multiplier_n15"},
    {"kind": "qft_topology", "n": 12, "tag": "qft12_topology_only"},
    *[
        {"kind": "random", "n": 24, "ops": 180, "seed": seed,
         "tag": f"random24x180_seed{seed}"}
        for seed in (11, 17, 23)
    ],
]

ARCH_BASE = {
    "d": 11, "cr_slots": 4, "n_banks": 2, "bank_capacity": 36,
    "bank_width": 6, "bus_lanes": None, "max_parallel_ops": 4,
    "sam_model": "line", "line_access_cost": 0, "line_move_per_row": 1, "line_initial_row": 0,
}
ARCH_SWEEP = {"cr_slots": [2, 4, 8], "n_banks": [1, 2, 4],
              "line_access_cost": [0, 1, 2]}
TIMING_BASE = {}


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


def build_cfg(circuit, point):
    arch = dict(ARCH_BASE)
    timing = dict(TIMING_BASE)
    arch.update({k: v for k, v in point.items() if not k.startswith("timing.")})
    timing.update({k.removeprefix("timing."): v for k, v in point.items()
                   if k.startswith("timing.")})
    # Capacity is held fixed across bank-count sensitivity. Fail loudly if the
    # workload cannot be placed; never silently shrink the memory to fit.
    if math.ceil(circuit.n_qubits / arch["n_banks"]) > arch["bank_capacity"]:
        raise ValueError("selected architecture has insufficient aggregate SAM capacity")
    return core.ArchConfig(**arch, timing=core.Timing(**timing))


def run():
    rows = []
    for spec in CIRCUITS:
        circuit = build_circuit(spec)
        print(f"\n== {spec['tag']}: {circuit.stats()}")
        for cr_slots, n_banks, line_access_cost in itertools.product(
            ARCH_SWEEP["cr_slots"], ARCH_SWEEP["n_banks"],
            ARCH_SWEEP["line_access_cost"],
        ):
            point = {
                "cr_slots": cr_slots,
                "n_banks": n_banks,
                "line_access_cost": line_access_cost,
            }
            cfg = build_cfg(circuit, point)
            for policy_name, params in POLICIES:
                policy = make_policy(policy_name, **params)
                sim = core.Simulator(circuit, cfg, policy)
                result = sim.run().asdict()
                errors = core.validate_schedule(sim.trace, circuit, cfg, sim.initial_loc)
                if errors:
                    raise AssertionError(
                        f"invalid trace {spec['tag']} {point} {policy.name}: {errors[:8]}"
                    )
                result.update({
                    "workload_tag": spec["tag"],
                    "source_kind": spec["kind"],
                    "seed": spec.get("seed", ""),
                    "cr_slots": cr_slots,
                    "n_banks": n_banks,
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
                    "t_t": cfg.timing.t_t,
                    "t_2q": cfg.timing.t_2q,
                    "magic_factories": cfg.timing.magic_factories,
                    "magic_interval": cfg.timing.magic_interval,
                    "magic_buffer": cfg.timing.magic_buffer,
                    "n_qubits": circuit.n_qubits,
                    "n_ops": len(circuit.ops),
                    "gate_counts": json.dumps(circuit.stats(), sort_keys=True),
                    "python_version": platform.python_version(),
                    "policy_params": json.dumps(params, sort_keys=True),
                    "experiment_axis": "architecture_and_access_sensitivity",
                    "comparison_group": f"{spec['tag']}:arch:CR{cr_slots}:B{n_banks}:access{line_access_cost}:t2q{cfg.timing.t_2q}",
                })
                rows.append(result)
                print(f"  CR={cr_slots:2d} banks={n_banks} "
                      f"{policy.name:22s} cycles={result['cycles']:7d} "
                      f"xfers={result['transfers']:5d} "
                      f"noncompute={result['non_exec_cycles']:6d}")

            # An all-qubits-resident reference is useful only when the register
            # fits in the selected CR. It is a lower-bound reference, not a
            # competing memory-management policy.
            if circuit.n_qubits <= cr_slots:
                sim = core.Simulator(
                    circuit, cfg, make_policy("joint"),
                    initially_resident=True,
                )
                result = sim.run().asdict()
                errors = core.validate_schedule(sim.trace, circuit, cfg, sim.initial_loc)
                if errors:
                    raise AssertionError(f"invalid all-CR trace: {errors[:8]}")
                result.update({
                    "workload_tag": spec["tag"],
                    "source_kind": spec["kind"],
                    "seed": spec.get("seed", ""),
                    "cr_slots": cr_slots,
                    "n_banks": n_banks,
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
                    "t_t": cfg.timing.t_t,
                    "t_2q": cfg.timing.t_2q,
                    "n_qubits": circuit.n_qubits,
                    "n_ops": len(circuit.ops),
                    "gate_counts": json.dumps(circuit.stats(), sort_keys=True),
                    "experiment_axis": "architecture_and_access_sensitivity",
                    "magic_factories": cfg.timing.magic_factories,
                    "magic_interval": cfg.timing.magic_interval,
                    "magic_buffer": cfg.timing.magic_buffer,
                    "python_version": platform.python_version(),
                    "policy_params": "{}",
                    "comparison_group": f"{spec['tag']}:arch:CR{cr_slots}:B{n_banks}:access{line_access_cost}:t2q{cfg.timing.t_2q}",
                    "policy": "all_qubits_in_CR_lower_bound",
                })
                rows.append(result)

    # Isolate two-qubit gate-duration sensitivity at one fixed architecture.
    # These groups are separate from the architecture/access matrix above.
    for spec in CIRCUITS:
        circuit = build_circuit(spec)
        for t_2q in (1, 2, 4):
            point = {
                "cr_slots": 4,
                "n_banks": 2,
                "line_access_cost": 1,
                "timing.t_2q": t_2q,
            }
            cfg = build_cfg(circuit, point)
            for policy_name, params in POLICIES:
                policy = make_policy(policy_name, **params)
                sim = core.Simulator(circuit, cfg, policy)
                result = sim.run().asdict()
                errors = core.validate_schedule(sim.trace, circuit, cfg, sim.initial_loc)
                if errors:
                    raise AssertionError(
                        f"invalid t2q-sensitivity trace {spec['tag']} t2q={t_2q} "
                        f"{policy.name}: {errors[:8]}"
                    )
                result.update({
                    "workload_tag": spec["tag"],
                    "source_kind": spec["kind"],
                    "seed": spec.get("seed", ""),
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
                    "t_t": cfg.timing.t_t,
                    "t_2q": cfg.timing.t_2q,
                    "magic_factories": cfg.timing.magic_factories,
                    "magic_interval": cfg.timing.magic_interval,
                    "magic_buffer": cfg.timing.magic_buffer,
                    "n_qubits": circuit.n_qubits,
                    "n_ops": len(circuit.ops),
                    "gate_counts": json.dumps(circuit.stats(), sort_keys=True),
                    "python_version": platform.python_version(),
                    "policy_params": json.dumps(params, sort_keys=True),
                    "experiment_axis": "two_qubit_timing_sensitivity",
                    "comparison_group": f"{spec['tag']}:t2q_sensitivity:CR4:B2:access1:t2q{t_2q}",
                })
                rows.append(result)

    with open("results.csv", "w", newline="") as handle:
        fields = list(dict.fromkeys(k for row in rows for k in row))
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    # Summarize only within identical workload/architecture groups. Never pool
    # raw cycle counts across different circuits or hardware configurations.
    grouped = defaultdict(dict)
    for row in rows:
        if row["policy"] != "all_qubits_in_CR_lower_bound":
            grouped[row["comparison_group"]][row["policy"]] = row
    policy_speedups = defaultdict(list)
    workload_speedups = defaultdict(lambda: defaultdict(list))
    access_speedups = defaultdict(lambda: defaultdict(list))
    t2q_speedups = defaultdict(lambda: defaultdict(list))
    for group, policies in grouped.items():
        baseline = policies.get("lsqca-lazy")
        if baseline is None or baseline["cycles"] <= 0:
            continue
        workload = baseline["workload_tag"]
        for policy, result in policies.items():
            if result["cycles"] <= 0:
                continue
            speedup = baseline["cycles"] / result["cycles"]
            policy_speedups[policy].append(speedup)
            workload_speedups[workload][policy].append(speedup)
            if baseline.get("experiment_axis") == "architecture_and_access_sensitivity":
                access_speedups[int(baseline["line_access_cost"])][policy].append(speedup)
            elif baseline.get("experiment_axis") == "two_qubit_timing_sensitivity":
                t2q_speedups[int(baseline["t_2q"])][policy].append(speedup)

    def geo(values):
        return math.exp(sum(math.log(x) for x in values) / len(values)) if values else float("nan")

    lines = [
        "# QubitFlow experiment summary",
        "",
        "All speedups are computed within the same workload and architecture configuration.",
        "Speedup = lazy-LRU makespan / policy makespan; values above 1 are faster.",
        "This is an abstract scheduling-model study, not a physical-hardware prediction.",
        "",
        "## Aggregate policy comparison",
        "",
        "| Policy | Geomean speedup vs lazy | Config wins / comparisons |",
        "|---|---:|---:|",
    ]
    for policy in sorted(policy_speedups):
        vals = policy_speedups[policy]
        wins = sum(v > 1.0 for v in vals)
        lines.append(f"| {policy} | {geo(vals):.4f}x | {wins}/{len(vals)} |")
    lines += ["", "## Per-workload geomean speedup", "",
              "| Workload | " + " | ".join(sorted(policy_speedups)) + " |",
              "|---|" + "---:|" * len(policy_speedups)]
    for workload in sorted(workload_speedups):
        cells = [f"{geo(workload_speedups[workload].get(p, [])):.4f}x"
                 if workload_speedups[workload].get(p) else "—"
                 for p in sorted(policy_speedups)]
        lines.append("| " + workload + " | " + " | ".join(cells) + " |")

    lines += ["", "## Local transfer-overhead sensitivity", "",
              "| Access overhead (beats) | " + " | ".join(sorted(policy_speedups)) + " |",
              "|---|" + "---:|" * len(policy_speedups)]
    for access in sorted(access_speedups):
        cells = [f"{geo(access_speedups[access].get(p, [])):.4f}x"
                 if access_speedups[access].get(p) else "—"
                 for p in sorted(policy_speedups)]
        lines.append(f"| {access} | " + " | ".join(cells) + " |")

    lines += ["", "## Two-qubit gate-duration sensitivity", "",
              "| t_2q (beats) | " + " | ".join(sorted(policy_speedups)) + " |",
              "|---|" + "---:|" * len(policy_speedups)]
    for t_2q in sorted(t2q_speedups):
        cells = [f"{geo(t2q_speedups[t_2q].get(p, [])):.4f}x"
                 if t2q_speedups[t_2q].get(p) else "—"
                 for p in sorted(policy_speedups)]
        lines.append(f"| {t_2q} | " + " | ".join(cells) + " |")

    lines += [
        "",
        "## Interpretation limits",
        "",
        "- The Line-SAM model captures row-distance only; it omits physical patch routing and port geometry.",
        "- CNOT and T-macro durations are explicit assumptions; consult the CSV for every timing parameter.",
        "- QFT-topology and random circuits are stress workloads, not functionally validated quantum applications.",
        "- Belady is an offline fixed-order oracle, not an online implementation.",
        "- All-qubits-in-CR rows are lower bounds and are excluded from speedup aggregates.",
        "",
    ]
    with open("summary.md", "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
    print(f"\nWrote {len(rows)} validated rows to results.csv and summary.md")
    return rows


if __name__ == "__main__":
    run()
