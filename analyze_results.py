"""Summarize validated QubitFlow experiment output using only the stdlib."""
import csv
import statistics
from collections import defaultdict


def main(path="results.csv"):
    with open(path, newline="") as handle:
        rows = list(csv.DictReader(handle))
    groups = defaultdict(dict)
    for row in rows:
        if row["policy"] != "all_qubits_in_CR_lower_bound":
            groups[row["comparison_group"]][row["policy"]] = row

    workload_rows = []
    for group, policies in sorted(groups.items()):
        baseline = policies.get("lsqca-lazy")
        if baseline is None:
            raise ValueError(f"missing LRU baseline for {group}")
        base_cycles = int(baseline["cycles"])
        for policy, row in sorted(policies.items()):
            cycles = int(row["cycles"])
            speedup = base_cycles / cycles if cycles else 1.0
            delta = cycles - base_cycles
            workload_rows.append({
                "experiment_axis": row.get("experiment_axis", "architecture_and_access_sensitivity"),
                "comparison_group": group,
                "workload_tag": row["workload_tag"],
                "cr_slots": row["cr_slots"],
                "n_banks": row["n_banks"],
                "line_access_cost": row.get("line_access_cost", ""),
                "t_2q": row.get("t_2q", ""),
                "experiment_axis": row.get("experiment_axis", ""),
                "policy": policy,
                "baseline_cycles": base_cycles,
                "cycles": cycles,
                "speedup_vs_lru": f"{speedup:.6f}",
                "cycle_delta_vs_lru": delta,
                "transfer_delta_vs_lru": int(row["transfers"]) - int(baseline["transfers"]),
                "loads": row["loads"],
                "stores": row["stores"],
                "non_exec_cycles": row["non_exec_cycles"],
                "msf_stall_cycles": row["msf_stall_cycles"],
            })

    with open("workload_summary.csv", "w", newline="") as handle:
        fields = list(workload_rows[0])
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(workload_rows)

    summary = []
    axis_samples = defaultdict(lambda: defaultdict(list))
    for row in workload_rows:
        axis_samples[row["experiment_axis"]][row["policy"]].append((
            float(row["speedup_vs_lru"]),
            int(row["cycle_delta_vs_lru"]),
            int(row["transfer_delta_vs_lru"]),
        ))
    for axis, policies in sorted(axis_samples.items()):
        for policy, samples in sorted(policies.items()):
            if policy == "lsqca-lazy":
                continue
            speeds = [x[0] for x in samples]
            deltas = [x[1] for x in samples]
            transfer_deltas = [x[2] for x in samples]
            summary.append({
                "experiment_axis": axis,
                "policy": policy,
                "groups": len(samples),
                "median_speedup_vs_lru": f"{statistics.median(speeds):.6f}",
                "mean_speedup_vs_lru": f"{statistics.mean(speeds):.6f}",
                "fraction_groups_faster": f"{sum(x > 1 for x in speeds) / len(speeds):.6f}",
                "median_cycle_delta": statistics.median(deltas),
                "median_transfer_delta": statistics.median(transfer_deltas),
            })

    with open("policy_summary.csv", "w", newline="") as handle:
        fields = list(summary[0])
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(summary)

    print(f"Analyzed {len(groups)} workload/architecture groups.")
    print("Median speedup vs program-order LRU (speedup > 1 is better):")
    for row in sorted(summary, key=lambda x: (x["experiment_axis"], -float(x["median_speedup_vs_lru"]))):
        print(
            f"  {row['experiment_axis']:34s} {row['policy']:24s} median={float(row['median_speedup_vs_lru']):.3f}x "
            f"mean={float(row['mean_speedup_vs_lru']):.3f}x "
            f"faster={float(row['fraction_groups_faster']):.1%} "
            f"groups={row['groups']}"
        )


if __name__ == "__main__":
    main()
