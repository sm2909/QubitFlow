"""Summarise results/*.csv into results/SUMMARY.md and PNG figures.   python analyze.py [results_dir]
Requires pandas + matplotlib (see requirements.txt). All numbers come straight from the CSVs."""
import sys, os, math
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

D = sys.argv[1] if len(sys.argv) > 1 else "results"
FIXED = ["demand_lru", "demand_belady", "prefetch_lru_W4", "prefetch_belady_W4"]
KEY = ["workload", "cr_slots", "n_banks", "magic_mode"]
out = []


def md(df, floatfmt=2):
    df = df.reset_index()
    cols = list(df.columns)
    lines = ["| " + " | ".join(map(str, cols)) + " |", "|" + "---|" * len(cols)]
    for _, r in df.iterrows():
        lines.append("| " + " | ".join(f"{v:.{floatfmt}f}" if isinstance(v, float) else str(v) for v in r) + " |")
    return "\n".join(lines)


def gmean(s):
    return math.exp(sum(math.log(x) for x in s) / len(s))


# ---------------- policy suite
p = pd.read_csv(f"{D}/policy.csv")
cyc = p.pivot_table(index=KEY, columns="policy_id", values="cycles")
cyc["best_fixed"] = cyc[FIXED].min(axis=1)
cyc["dag_demand_vs_best_fixed"] = cyc.best_fixed / cyc.dag_demand
cyc["dag_W4_vs_best_fixed"] = cyc.best_fixed / cyc.dag_W4
cyc["dag_best_vs_best_fixed"] = cyc.best_fixed / cyc[["dag_demand", "dag_W4"]].min(axis=1)
cyc["prefetch_gain_dag"] = cyc.dag_demand / cyc.dag_W4
nsched = p[p.policy_id.isin(FIXED)].groupby(KEY).schedule_hash.nunique().rename("distinct_fixed_schedules")
out.append("# QubitFlow results summary\n\nSpeedup = best-fixed cycles / DAG cycles (>1 means DAG is faster). 'best fixed' is the "
           "per-configuration oracle minimum over the four fixed-order policies, a deliberately strong baseline. "
           "Timing: `Timing.lsqca_paper` (see docs/ASSUMPTIONS.md); Line-SAM; deterministic simulator, so no run-to-run noise.\n")
out.append("## 1. Fixed-order vs DAG-aware scheduling (policy suite)\n")
for mode in ("finite", "unlimited"):
    sub = cyc.xs(mode, level="magic_mode")
    out.append(f"### Magic states: {mode} ({'one state / 15 beats, buffer 2' if mode=='finite' else 'isolates memory scheduling'})\n")
    out.append(md(sub[["best_fixed", "dag_demand", "dag_W4", "dag_best_vs_best_fixed"]]))
    out.append("")
    g = sub.groupby(level="workload").dag_best_vs_best_fixed.apply(gmean).rename("geomean_speedup")
    out.append(md(g.to_frame()))
    out.append(f"\nOverall geomean speedup ({mode}): **{gmean(sub.dag_best_vs_best_fixed):.3f}**; "
               f"configs where DAG is slower: {(sub.dag_best_vs_best_fixed < 0.999).sum()} / {len(sub)}\n")
out.append("### Configurations where fixed-order policies produce different schedules\n")
out.append(md(nsched.to_frame().query("distinct_fixed_schedules > 1")) if (nsched > 1).any() else "none")
out.append("")
# figure
fig, ax = plt.subplots(1, 2, figsize=(11, 3.6), sharey=True)
for a, mode in zip(ax, ("finite", "unlimited")):
    s = cyc.xs(mode, level="magic_mode").dag_best_vs_best_fixed.unstack(["cr_slots", "n_banks"])
    s.plot.bar(ax=a, legend=False, width=0.85)
    a.axhline(1, color="k", lw=0.8); a.set_title(f"magic states: {mode}"); a.set_xlabel("")
ax[0].set_ylabel("speedup of DAG over best fixed-order"); ax[1].legend(title="(CR, banks)", fontsize=7, ncol=2)
plt.tight_layout(); plt.savefig(f"{D}/fig_speedup.png", dpi=150); plt.close()

# ---------------- baseline sweep
b = pd.read_csv(f"{D}/baseline.csv")
out.append("## 2. Baseline policies and prefetch-window sweep (2 banks)\n")
for mode in ("finite", "unlimited"):
    t = b[b.magic_mode == mode].pivot_table(index=["workload", "cr_slots"], columns="policy_id", values="cycles")
    out.append(f"### cycles, magic {mode}\n"); out.append(md(t, 0)); out.append("")
    t2 = b[b.magic_mode == mode].groupby(["workload", "cr_slots"]).schedule_hash.nunique().rename("distinct_schedules")
    out.append("distinct schedules among the 10 baseline policies:\n"); out.append(md(t2.to_frame())); out.append("")

# ---------------- MSF sweep
m = pd.read_csv(f"{D}/msf.csv")
out.append("## 3. When does memory latency become visible? (magic-state interval sweep, CR=4, 2 banks)\n")
t = m.pivot_table(index=["workload", "magic_interval"], columns="policy_id", values="cycles")
out.append(md(t, 0)); out.append("")
fig, ax = plt.subplots(1, 5, figsize=(15, 3), sharex=True)
for a, (w, g) in zip(ax, m.groupby("workload")):
    for pid, gg in g.groupby("policy_id"):
        a.plot(gg.magic_interval, gg.cycles, marker="o", label=pid)
    a.set_title(w); a.set_xscale("log"); a.set_yscale("log"); a.set_xlabel("beats per magic state")
ax[0].set_ylabel("cycles"); ax[-1].legend(fontsize=6)
plt.tight_layout(); plt.savefig(f"{D}/fig_msf_sweep.png", dpi=150); plt.close()

# ---------------- ablation
a_ = pd.read_csv(f"{D}/ablation.csv")
out.append("## 4. DAG scheduler ablation (unlimited magic states, 2 banks)\n")
t = a_.pivot_table(index=["workload", "cr_slots"], columns="policy_id", values="cycles")
out.append(md(t, 0)); out.append("")
rank = a_.assign(rel=a_.cycles / a_.groupby(["workload", "cr_slots"]).cycles.transform("min")) \
         .groupby("policy_id").rel.apply(gmean).sort_values().rename("geomean_slowdown_vs_best_variant")
out.append("Variants ranked by geomean slowdown relative to the best variant per (workload, CR):\n")
out.append(md(rank.to_frame(), 3)); out.append("")

# ---------------- seeds
s = pd.read_csv(f"{D}/seeds.csv")
out.append("## 5. Seed variability (random Clifford+T, 5 seeds, CR=4, 2 banks, unlimited magic)\n")
agg = s.groupby("policy_id").cycles.agg(["mean", "std", "min", "max"])
out.append(md(agg, 1)); out.append("")
sp = s.pivot_table(index="seed", columns="policy_id", values="cycles")
sp["speedup"] = sp[FIXED].min(axis=1) / sp[["dag_demand", "dag_W4"]].min(axis=1)
out.append(f"DAG speedup over best fixed per seed: {', '.join(f'{x:.2f}' for x in sp.speedup)}  (mean {sp.speedup.mean():.2f}, std {sp.speedup.std():.2f})\n")

# ---------------- transfer / area
out.append("## 6. Transfer traffic and area (policy suite, unlimited magic, CR=4, 2 banks)\n")
t = p[(p.magic_mode == "unlimited") & (p.cr_slots == 4) & (p.n_banks == 2)].pivot_table(
    index="workload", columns="policy_id", values="transfers")
out.append(md(t, 0)); out.append("")
ph = p[(p.cr_slots == 4)].groupby("n_banks").physical_qubits.first()
out.append("Physical-qubit estimate depends on architecture only (not on policy):\n")
out.append(md(p.groupby(["workload", "cr_slots", "n_banks"]).physical_qubits.first().unstack("n_banks"), 0))
open(f"{D}/SUMMARY.md", "w").write("\n".join(out) + "\n")
print(f"wrote {D}/SUMMARY.md and figures")
