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
    "d": 11, "cr_slots": 4, "n_banks": 2, "bank_capacity": 32,
    "bank_width": 8, "bus_lanes": None, "max_parallel_ops": 4,
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
                    "comparison_group": f"{spec['tag']}:CR{cr_slots}:B{n_banks}:access{line_access_cost}",
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
                    "line_move_per_row": cfg.line_move_per_row,
                    "magic_factories": cfg.timing.magic_factories,
                    "magic_interval": cfg.timing.magic_interval,
                    "magic_buffer": cfg.timing.magic_buffer,
                    "python_version": platform.python_version(),
                    "policy_params": "{}",
                    "comparison_group": f"{spec['tag']}:CR{cr_slots}:B{n_banks}",
                    "policy": "all_qubits_in_CR_lower_bound",
                })
                rows.append(result)

    with open("results.csv", "w", newline="") as handle:
        fields = list(dict.fromkeys(k for row in rows for k in row))
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nWrote {len(rows)} validated rows to results.csv")
    return rows


if __name__ == "__main__":
    run()
