"""Validate and summarize the reproducible QubitFlow experiment outputs.

All policy speedups are paired within identical workload and architecture groups.
Aggregate summaries report both matched-configuration and equal-workload weighting.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path

from qf_policies import make_policy


def geomean(values):
    values = list(values)
    if not values:
        return float("nan")
    if any(value <= 0 for value in values):
        raise ValueError("geometric mean requires positive values")
    return math.exp(sum(math.log(value) for value in values) / len(values))


def load_rows(path):
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def expected_comparison_groups(manifest):
    """Return expected group IDs and policy-name sets by experiment axis."""
    workloads = manifest["workloads"]
    matrices = manifest["policy_matrices"]
    expected = {}
    for workload in workloads:
        tag = workload["tag"]
        for cr_slots in manifest["architecture_sweep"]["cr_slots"]:
            for n_banks in manifest["architecture_sweep"]["n_banks"]:
                for access_cost in manifest["architecture_sweep"]["line_access_cost"]:
                    group = (
                        f"{tag}:architecture_and_access_sensitivity:"
                        f"CR{cr_slots}:B{n_banks}:access{access_cost}:t2q1"
                    )
                    expected[group] = {
                        item["policy_name"]
                        for item in matrices["architecture_and_access_sensitivity"]
                    }

        group = f"{tag}:policy_ablation:CR4:B2:access1:t2q1"
        expected[group] = {
            item["policy_name"] for item in matrices["policy_ablation"]
        }

        for t_2q in manifest["two_qubit_timing_sweep"]:
            group = (
                f"{tag}:two_qubit_timing_sensitivity:"
                f"CR4:B2:access1:t2q{t_2q}"
            )
            expected[group] = {
                item["policy_name"]
                for item in matrices["two_qubit_timing_sensitivity"]
            }
    return expected

def validate_manifest_hashes(manifest):
    mismatches = []
    for path, expected in manifest.get("code_and_input_sha256", {}).items():
        if not Path(path).is_file():
            mismatches.append(f"manifest file missing: {path}")
        else:
            actual = sha256_file(path)
            if actual != expected:
                mismatches.append(
                    f"SHA-256 mismatch for {path}: expected {expected}, got {actual}"
                )
    if mismatches:
        raise ValueError("manifest integrity check failed: " + "; ".join(mismatches[:10]))


def validate_completeness(rows, manifest):
    groups = defaultdict(dict)
    for row in rows:
        if row["policy"] == "all_qubits_in_CR_lower_bound":
            continue
        group = row["comparison_group"]
        policy = row["policy"]
        if policy in groups[group]:
            raise ValueError(f"duplicate row for group={group} policy={policy}")
        groups[group][policy] = row

    expected_groups = expected_comparison_groups(manifest)
    observed_groups = set(groups)
    missing_groups = sorted(set(expected_groups) - observed_groups)
    extra_groups = sorted(observed_groups - set(expected_groups))
    if missing_groups or extra_groups:
        raise ValueError(
            f"incomplete/extra experiment groups: missing={missing_groups[:12]}, "
            f"extra={extra_groups[:12]} (missing={len(missing_groups)}, extra={len(extra_groups)})"
        )

    for group, observed in groups.items():
        if group not in expected_groups:
            raise ValueError(f"unexpected comparison group {group}")
        expected = expected_groups[group]
        missing = sorted(expected - set(observed))
        extra = sorted(set(observed) - expected)
        if missing or extra:
            raise ValueError(
                f"incomplete policy matrix for {group}: missing={missing}, extra={extra}"
            )
        baseline = observed["lsqca-lazy"]
        matched_fields = (
            "workload_tag", "workload_class", "benchmark_source",
            "source_path", "source_sha256", "n_qubits", "n_ops",
            "cr_slots", "n_banks", "bank_capacity", "bank_width", "bank_rows",
            "total_sam_data_cells", "bus_lanes",
            "line_access_cost", "line_move_per_row", "line_initial_row",
            "t_h", "t_s", "t_pauli", "t_measure", "t_parity_measure",
            "t_t_macro", "t_2q", "magic_factories", "magic_interval", "magic_buffer",
        )
        for policy, row in observed.items():
            for field in matched_fields:
                if row[field] != baseline[field]:
                    raise ValueError(
                        f"unmatched comparison {group} field={field} "
                        f"baseline={baseline[field]} policy={policy}:{row[field]}"
                    )
            total_cells = int(row["total_sam_data_cells"])
            if total_cells != int(manifest["total_sam_data_cells"]):
                raise ValueError(
                    f"aggregate SAM capacity mismatch in {group}: "
                    f"expected {manifest['total_sam_data_cells']}, got {total_cells}"
                )
            geometry = manifest["bank_geometry_by_bank_count"][str(int(row["n_banks"]))]
            if (int(row["bank_capacity"]) != int(geometry["bank_capacity"])
                    or int(row["bank_width"]) != int(geometry["bank_width"])):
                raise ValueError(
                    f"bank geometry mismatch in {group}: expected {geometry}, "
                    f"got capacity={row['bank_capacity']} width={row['bank_width']}"
                )
    return groups


def main(results_path="results.csv", manifest_path="experiment_manifest.json"):
    rows = load_rows(results_path)
    if not rows:
        raise ValueError("results.csv is empty")
    with open(manifest_path, encoding="utf-8") as handle:
        manifest = json.load(handle)

    validate_manifest_hashes(manifest)
    if len(rows) != int(manifest["row_count"]):
        raise ValueError(
            f"results row-count mismatch: manifest={manifest['row_count']} CSV={len(rows)}"
        )
    groups = validate_completeness(rows, manifest)
    observed_axis_counts = defaultdict(set)
    for group in groups:
        axis = group.split(":", 2)[1]
        observed_axis_counts[axis].add(group)
    for axis, expected_count in manifest["expected_policy_group_count"].items():
        actual_count = len(observed_axis_counts.get(axis, set()))
        if actual_count != int(expected_count):
            raise ValueError(
                f"manifest group count mismatch for {axis}: "
                f"expected {expected_count}, got {actual_count}"
            )
    baseline_name = "lsqca-lazy"
    workload_rows = []
    per_workload = defaultdict(lambda: defaultdict(list))
    per_axis = defaultdict(lambda: defaultdict(list))
    per_class = defaultdict(lambda: defaultdict(list))

    for group, policies in sorted(groups.items()):
        baseline = policies[baseline_name]
        base_cycles = int(baseline["cycles"])
        axis = baseline["experiment_axis"]
        workload = baseline["workload_tag"]
        workload_class = baseline["workload_class"]
        if base_cycles <= 0:
            raise ValueError(f"non-positive LRU makespan in {group}")

        for policy, row in sorted(policies.items()):
            cycles = int(row["cycles"])
            if cycles <= 0:
                raise ValueError(f"non-positive makespan in {group}/{policy}")
            speedup = base_cycles / cycles
            cycle_delta = cycles - base_cycles
            transfer_delta = int(row["transfers"]) - int(baseline["transfers"])
            per_workload[(axis, workload)][policy].append(speedup)
            per_axis[axis][policy].append(speedup)
            per_class[(axis, workload_class)][policy].append(speedup)
            workload_rows.append({
                "experiment_axis": axis,
                "workload_tag": workload,
                "workload_class": workload_class,
                "comparison_group": group,
                "policy": policy,
                "baseline_cycles": base_cycles,
                "cycles": cycles,
                "speedup_vs_lru": f"{speedup:.8f}",
                "cycle_delta_vs_lru": cycle_delta,
                "baseline_transfers": baseline["transfers"],
                "transfers": row["transfers"],
                "transfer_delta_vs_lru": transfer_delta,
                "loads": row["loads"],
                "stores": row["stores"],
                "exec_busy_cycles": row["exec_busy_cycles"],
                "non_exec_cycles": row["non_exec_cycles"],
                "msf_stall_cycles": row["msf_stall_cycles"],
                "cr_slots": row["cr_slots"],
                "n_banks": row["n_banks"],
                "bank_capacity": row["bank_capacity"],
                "bank_width": row["bank_width"],
                "bank_rows": row["bank_rows"],
                "total_sam_data_cells": row["total_sam_data_cells"],
                "line_access_cost": row["line_access_cost"],
                "t_2q": row["t_2q"],
            })

    with open("workload_summary.csv", "w", newline="", encoding="utf-8") as handle:
        fields = list(workload_rows[0])
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(workload_rows)

    workload_geos = []
    for (axis, workload), policies in sorted(per_workload.items()):
        baseline = "lsqca-lazy"
        for policy, values in sorted(policies.items()):
            workload_geos.append({
                "experiment_axis": axis,
                "workload_tag": workload,
                "policy": policy,
                "configuration_count": len(values),
                "geomean_speedup_vs_lru": f"{geomean(values):.8f}",
                "median_speedup_vs_lru": f"{statistics.median(values):.8f}",
                "configuration_wins": sum(value > 1.0 for value in values),
                "configuration_losses": sum(value < 1.0 for value in values),
                "configuration_ties": sum(value == 1.0 for value in values),
            })
    with open("workload_geomeans.csv", "w", newline="", encoding="utf-8") as handle:
        fields = list(workload_geos[0])
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(workload_geos)

    policy_summary = []
    summary_by_axis = {}
    for axis, policies in sorted(per_axis.items()):
        for policy, values in sorted(policies.items()):
            if policy == baseline_name:
                continue
            workload_values = [
                geomean(per_workload[(axis, workload)][policy])
                for a, workload in per_workload
                if a == axis and policy in per_workload[(axis, workload)]
            ]
            rec = {
                "experiment_axis": axis,
                "policy": policy,
                "matched_configurations": len(values),
                "configuration_geomean_speedup": f"{geomean(values):.8f}",
                "configuration_median_speedup": f"{statistics.median(values):.8f}",
                "configuration_win_fraction": f"{sum(v > 1.0 for v in values) / len(values):.8f}",
                "equal_workload_geomean_speedup": f"{geomean(workload_values):.8f}",
                "workloads": len(workload_values),
            }
            policy_summary.append(rec)

        summary_by_axis[axis] = sorted(
            [r for r in policy_summary if r["experiment_axis"] == axis],
            key=lambda r: float(r["equal_workload_geomean_speedup"]),
            reverse=True,
        )
    with open("policy_summary.csv", "w", newline="", encoding="utf-8") as handle:
        fields = list(policy_summary[0])
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(policy_summary)

    class_summary = []
    for (axis, workload_class), policies in sorted(per_class.items()):
        for policy, values in sorted(policies.items()):
            if policy == baseline_name:
                continue
            # Equal weight per workload: first reduce configuration points to a
            # workload-specific geometric mean, then aggregate those workloads.
            tags = sorted({
                workload for a, workload in per_workload
                if a == axis and workload_class == next(
                    row["workload_class"] for row in rows
                    if row["workload_tag"] == workload
                )
            })
            tagged = [
                geomean(per_workload[(axis, workload)][policy])
                for workload in tags
                if policy in per_workload[(axis, workload)]
            ]
            class_summary.append({
                "experiment_axis": axis,
                "workload_class": workload_class,
                "policy": policy,
                "workloads": len(tagged),
                "equal_workload_geomean_speedup": f"{geomean(tagged):.8f}",
            })
    with open("workload_class_summary.csv", "w", newline="", encoding="utf-8") as handle:
        fields = list(class_summary[0])
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(class_summary)

    lines = [
        "# QubitFlow experiment report",
        "",
        f"- Source revision: `{manifest.get('source_revision', 'unknown')}`",
        f"- Run started (UTC): {manifest.get('run_started_utc', 'unknown')}",
        f"- Python: {manifest.get('python_version', 'unknown').split()[0]}",
        f"- Raw validated schedule rows: {len(rows)}",
        f"- Matched workload/configuration groups: {len(groups)}",
        "- Primary metric: makespan in abstract code beats",
        "- Baseline: program-order demand paging with LRU replacement",
        "",
        "Speedup = LRU makespan / candidate makespan; values above 1 are faster.",
        "Configuration points are paired within each exact workload and hardware group.",
        "The equal-workload aggregate gives each workload equal weight after reducing its architecture points.",
        "",
    ]
    for axis, records in summary_by_axis.items():
        lines += [
            f"## {axis.replace('_', ' ').title()}",
            "",
            "| Policy | Equal-workload geomean | Configuration geomean | Config wins | Workloads |",
            "|---|---:|---:|---:|---:|",
        ]
        for r in records:
            lines.append(
                f"| {r['policy']} | {float(r['equal_workload_geomean_speedup']):.4f}x "
                f"| {float(r['configuration_geomean_speedup']):.4f}x "
                f"| {float(r['configuration_win_fraction']):.1%} "
                f"| {r['workloads']} |"
            )
        lines.append("")

    lines += [
        "## Per-workload results",
        "",
        "| Axis | Workload | Policy | Geomean speedup | Wins / configurations |",
        "|---|---|---|---:|---:|",
    ]
    for r in workload_geos:
        if r["policy"] == baseline_name:
            continue
        lines.append(
            f"| {r['experiment_axis']} | {r['workload_tag']} | {r['policy']} "
            f"| {float(r['geomean_speedup_vs_lru']):.4f}x "
            f"| {r['configuration_wins']}/{r['configuration_count']} |"
        )

    lines += [
        "",
        "## Workload-class summaries",
        "",
        "| Axis | Workload class | Policy | Equal-workload geomean | Workloads |",
        "|---|---|---|---:|---:|",
    ]
    for r in class_summary:
        lines.append(
            f"| {r['experiment_axis']} | {r['workload_class']} | {r['policy']} "
            f"| {float(r['equal_workload_geomean_speedup']):.4f}x | {r['workloads']} |"
        )

    lines += [
        "",
        "## Interpretation boundaries",
        "",
        "- These are results from a discrete-event scheduling abstraction, not a physical surface-code or hardware prediction.",
        "- Line-SAM transfer time is row-distance plus an explicit local-overhead parameter; patch routing and port geometry are omitted.",
        "- CNOT and T-macro costs are assumptions; PM/SK latency is not explicitly modeled.",
        "- QASMBench arithmetic inputs are imported from the bundled transpiled OpenQASM files; the simulator does not execute quantum states.",
        "- The synthetic QFT interaction topology is not a functionally equivalent QFT circuit.",
        "- Fixed-order farthest-next-use is an offline Belady-inspired baseline; under nonuniform transfer times and bank contention it is not an optimal oracle.",
        "- Random workloads are stress tests. They are not independent experimental subjects or application workloads.",
        "- No p-values are reported: configuration points within one circuit are correlated, and the workload families are deliberately small.",
        "",
    ]
    with open("summary.md", "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
    print(f"Validated {len(groups)} complete matched groups across {len(rows)} rows.")
    print("Equal-workload geometric mean speedup (values above 1 are faster):")
    for axis, records in summary_by_axis.items():
        print(f"  {axis}")
        for r in records:
            print(
                f"    {r['policy']:34s} "
                f"{float(r['equal_workload_geomean_speedup']):.4f}x "
                f"({r['workloads']} workloads)"
            )


if __name__ == "__main__":
    main()
