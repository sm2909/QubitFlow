"""Baseline policies. All three share one class: FixedOrderPolicy.

  lsqca_lazy()            lookahead=0, LRU   -> fetch only when the next op needs it
  fixed_prefetch(W)       lookahead=W, LRU   -> same order, start loads for next W ops early
  fixed_belady(W)         lookahead=W, Belady-> same, but evict the qubit used farthest ahead

Issue is strictly in program order (op i+1 never starts before op i has started), but
ops overlap in time when independent. Write-back is lazy: a qubit leaves CR only when
a slot is needed. NOTE: 'lsqca_lazy' is OUR reading of the paper's on-demand policy;
confirm eviction/placement details against the paper (docs/DESIGN.md, calibration list).

To add the joint scheduler: implement `name` and `step(sim)` in a new class, using only
sim.try_exec / try_load / try_store and the read-only queries.
"""
from qf_core import INF


class FixedOrderPolicy:
    def __init__(self, lookahead=0, eviction="lru", name=None):
        assert eviction in ("lru", "belady")
        self.lookahead, self.eviction = lookahead, eviction
        self.name = name or f"fixed(W={lookahead},{eviction})"

    def _victim(self, sim, protect, target_next=None):
        cands = [q for q in sim.loc if sim.state(q) == "CR" and q not in sim.busy
                 and q not in protect]
        if not cands:
            return None
        if self.eviction == "lru":
            return min(cands, key=lambda q: sim.last_used[q])
        v = max(cands, key=lambda q: (sim.next_use(q), -sim.last_used[q]))
        if target_next is not None and not sim.next_use(v) > target_next:
            return None                      # prefetch must not evict something needed sooner
        return v

    def step(self, sim) -> bool:
        c, n, progress = sim.circuit, len(sim.circuit.ops), False
        # 1) in-order issue
        while sim.head() < n and sim.try_exec(sim.head()):
            progress = True
        if sim.head() >= n:
            return progress
        head = sim.head()
        head_qs = set(c.ops[head].qubits)
        # 2) fetch operands of the next (1 + lookahead) ops, head first
        for i in range(head, min(n, head + 1 + self.lookahead)):
            demand = (i == head)
            for q in c.ops[i].qubits:
                if sim.state(q) != "SAM":
                    continue
                need_head = sum(sim.state(h) == "SAM" for h in head_qs)
                if sim.free_slots() > (0 if demand else need_head):
                    progress |= sim.try_load(q)
                elif demand or self.eviction == "belady":
                    if sim.free_slots() + sim.pending_stores() >= need_head + (0 if demand else 1):
                        continue                       # a slot is already on its way
                    v = self._victim(sim, head_qs, None if demand else sim.next_use(q))
                    if v is not None:
                        progress |= sim.try_store(v)
        return progress



# ---------------------------------------------------------------------------------------
# Registry: policies are referenced by name from run.py's EXPERIMENT config.
# To add a method: write a class with `.name` and `.step(sim)`, then register a factory.
# ---------------------------------------------------------------------------------------
class PolicyTemplate:
    """Interface (duck-typed). step() is called repeatedly at the same timestamp until it
    returns False. Read-only: sim.state(q), sim.free_slots(), sim.pending_stores(),
    sim.next_use(q), sim.head(), sim.t, sim.port_free, sim.lane_free, sim.ms_stock,
    sim.circuit.{ops,preds,uses}, sim.started/finished/busy. Actions (False if illegal now):
    sim.try_exec(op_id), sim.try_load(q, slot=None), sim.try_store(q, bank=None, cell=None).
    NOTE: sim.head()/next_use() assume ops start in program order; a reordering policy
    should derive its own next-use from sim.started."""
    name = "template"

    def step(self, sim) -> bool:
        raise NotImplementedError


POLICIES = {
    "lsqca_lazy":     lambda **kw: FixedOrderPolicy(0, "lru", "lsqca-lazy"),
    "fixed_prefetch": lambda lookahead=8, **kw: FixedOrderPolicy(lookahead, "lru", f"fixed-prefetch(W={lookahead})"),
    "fixed_belady":   lambda lookahead=8, **kw: FixedOrderPolicy(lookahead, "belady", f"fixed-belady(W={lookahead})"),
    # "joint": lambda **kw: JointPolicy(**kw),
}


def make_policy(name, **params):
    return POLICIES[name](**params)