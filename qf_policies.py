"""Scheduling policies.

FIXED-ORDER baselines (class FixedOrderPolicy): issue strictly in program order (op i+1 never
starts before op i has started), but ops overlap in time when independent.
  demand_lru            lookahead=0, LRU    -> fetch only when the head op needs it  (= lsqca_lazy)
  demand_belady         lookahead=0, Belady -> same, evict the qubit used farthest ahead
  prefetch_lru(W)       lookahead=W, LRU    -> start loads for the next W ops early  (= fixed_prefetch)
  prefetch_belady(W)    lookahead=W, Belady -> same, Belady eviction                 (= fixed_belady)
Write-back is lazy: a qubit leaves CR only when a slot is needed. NOTE: 'lsqca_lazy' is OUR
reading of the paper's on-demand policy (the paper keeps CR minimal and stores immediately).

DAG-AWARE policy (class DagPolicy): see its docstring.

Interface (duck-typed): `.name`, `.spec` (dict of parameters, logged by experiments), and
`.step(sim) -> bool` (called repeatedly at one timestamp until it returns False). Actions:
sim.try_exec(op_id), sim.try_load(q, slot=None), sim.try_store(q, bank=None, cell=None).
Read-only queries: sim.state(q), sim.free_slots(), sim.pending_stores(), sim.pending_loads(),
sim.ready_ops(), sim.op_state(i), sim.future_uses(q), sim.next_use(q), sim.store_cost_estimate(q),
sim.xfer_of, sim.busy, sim.last_used, sim.circuit.{ops,preds,succs,uses,level,critical_path}.
"""
from qf_core import INF


class FixedOrderPolicy:
    def __init__(self, lookahead=0, eviction="lru", name=None):
        if eviction not in ("lru", "belady"):
            raise ValueError("eviction must be 'lru' or 'belady'")
        if not isinstance(lookahead, int) or lookahead < 0:
            raise ValueError("lookahead must be an integer >= 0")
        self.lookahead, self.eviction = lookahead, eviction
        self.name = name or f"fixed(W={lookahead},{eviction})"
        self.spec = {"family": "fixed", "lookahead": lookahead, "eviction": eviction}

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

    @staticmethod
    def _check_topological(circuit):
        for i, ps in enumerate(circuit.preds):
            if any(p > i for p in ps):
                raise ValueError(
                    f"fixed-order policies require program order to be a topological order, but op {i} "
                    f"depends on a later op; use the DAG-aware policy ('dag') for such circuits")

    def step(self, sim) -> bool:
        if not getattr(self, "_checked", None) is sim.circuit:
            self._check_topological(sim.circuit)
            self._checked = sim.circuit
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


class DagPolicy:
    """DAG-aware joint scheduler (first version).

    Each step():
      (crit_tol: ops whose critical-path lengths fall in the same bucket of `crit_tol` beats are
       treated as equal priority and ordered by program index, preserving Line-SAM row locality.)
      1. ISSUE: start every dependency-ready op whose operands are all resident+idle in CR,
         ordered by critical-path length (longest duration-weighted path to a sink).
      2. DEMAND: walk the remaining ready ops by priority (operands-already-in-CR first, then
         critical path, then program index). An op is *committed* only if all its missing
         operands fit in the slot budget (free slots + write-backs in flight + idle unprotected
         CR qubits) -- all-or-nothing, so two half-loaded ops can never deadlock a small CR.
         Committed ops' operands are protected from eviction (sticky until the op starts).
         Loads are issued for committed ops; if slots are short, idle CR qubits are evicted.
      3. EVICT: victim = idle, unprotected CR qubit maximising
              next_use_level(q) - cost_weight * store_cost(q)
         i.e. farthest next use in the remaining DAG (ASAP depth, a Belady analogue for DAGs;
         INF if never used again), discounted by the Line-SAM cost of writing it back. Ties: LRU.
      4. PREFETCH (only if prefetch_window > 0 and no demand load is still waiting for a
         resource): for the first `prefetch_window` soon-ready ops (all preds finished or
         running) not fully resident, load missing operands into FREE slots; if
         `prefetch_evict`, may evict a victim whose next use is strictly later than the target's.
         Demand therefore always has priority over speculation.
    Deterministic: no randomness; all ties broken by indices.
    """

    def __init__(self, prefetch_window=0, cost_weight=0.25, prefetch_evict=True, urgency="crit",
                 crit_tol=1, name=None):
        if not isinstance(prefetch_window, int) or prefetch_window < 0:
            raise ValueError("prefetch_window must be an integer >= 0")
        if cost_weight < 0:
            raise ValueError("cost_weight must be >= 0")
        if urgency not in ("crit", "level"):
            raise ValueError("urgency must be 'crit' or 'level'")
        if not isinstance(crit_tol, int) or crit_tol < 1:
            raise ValueError("crit_tol must be an integer >= 1")
        self.W, self.cw, self.pe, self.urg = prefetch_window, cost_weight, prefetch_evict, urgency
        self.tol = crit_tol
        self.name = name or (f"dag(W={prefetch_window})" if prefetch_window else "dag(demand)")
        self.spec = {"family": "dag", "prefetch_window": prefetch_window,
                     "cost_weight": cost_weight, "prefetch_evict": prefetch_evict,
                     "urgency": urgency, "crit_tol": crit_tol}
        self._sticky = set()
        self._cref = 0

    # ---- helpers
    def _crit(self, sim):
        return sim.circuit.critical_path(sim.cfg.timing)

    def _in_cr(self, sim, i):
        return all(sim.state(q) == "CR" and q not in sim.busy for q in sim.circuit.ops[i].qubits)

    def _next_level(self, sim, q):
        """'Distance' to the next use of q (larger = needed later; INF = never).
        urgency='crit' : critical-path distance in beats, C_ref - crit[next op], where C_ref is the
                         largest crit among ops that could run now. Execution order follows
                         critical-path priority, so this tracks actual issue order.
        urgency='level': static ASAP depth of the next op (a coarser Belady analogue)."""
        u = sim.future_uses(q)
        if not u:
            return INF
        if self.urg == "level":
            return sim.circuit.level[u[0]]
        return self._cref - sim.circuit.critical_path(sim.cfg.timing)[u[0]]

    def _target_dist(self, sim, i):
        return (c_lvl := sim.circuit.level[i]) if self.urg == "level" else \
            self._cref - self._crit(sim)[i]

    def _victim_key(self, sim, q):
        nl = self._next_level(sim, q)
        cost = sim.store_cost_estimate(q)
        pen = self.cw * cost if cost != INF else 0
        return (nl - pen if nl != INF else INF, -sim.last_used[q])

    def _pick_victim(self, sim, protected, must_exceed=None):
        cands = [q for q in sim.loc if sim.state(q) == "CR" and q not in sim.busy
                 and q not in protected]
        if not cands:
            return None
        v = max(cands, key=lambda q: self._victim_key(sim, q) + (-q,))
        if must_exceed is not None and not self._next_level(sim, v) > must_exceed:
            return None
        return v

    def step(self, sim) -> bool:
        c, crit, progress = sim.circuit, self._crit(sim), False
        self._sticky = {i for i in self._sticky if i not in sim.started}
        soon = sim.soon_ready_ops()
        self._cref = max((crit[i] for i in soon), default=0)

        # 1) ISSUE -------------------------------------------------------------------
        runnable = [i for i in sim.ready_ops() if self._in_cr(sim, i)]
        for i in sorted(runnable, key=lambda i: (-(crit[i] // self.tol), i)):
            if sim.try_exec(i):
                progress = True
        if progress:
            return True

        # 2) DEMAND: commit ops under the slot budget -----------------------------------
        ready = [i for i in sim.ready_ops()]
        def pkey(i):
            qs = c.ops[i].qubits
            return (-sum(sim.state(q) == "CR" for q in qs), -(crit[i] // self.tol), i)
        order = sorted((i for i in ready if i in self._sticky), key=pkey) + \
                sorted((i for i in ready if i not in self._sticky), key=pkey)
        evictable = [q for q in sim.loc if sim.state(q) == "CR" and q not in sim.busy]
        avail = sim.free_slots() + sim.pending_stores() + len(evictable)
        protected, committed = set(), []
        for i in order:
            qs = c.ops[i].qubits
            if any(sim.state(q) == "XFER" and sim.xfer_of.get(q, {}).get("kind") == "store" for q in qs):
                continue                                   # operand still being written back
            n_load = sum(sim.state(q) == "SAM" for q in qs)
            c_move = sum(sim.state(q) == "CR" and q not in protected for q in qs)
            if n_load + c_move <= avail:
                avail -= n_load + c_move
                protected.update(qs)
                committed.append(i)
                self._sticky.add(i)
            elif i in self._sticky:
                self._sticky.discard(i)
        demand_loads = [q for i in committed for q in c.ops[i].qubits if sim.state(q) == "SAM"]
        unissued = []
        for q in demand_loads:
            if sim.free_slots() > 0 and sim.try_load(q):
                progress = True
            else:
                unissued.append(q)
        # slots needed beyond what is free or already being freed
        for _ in range(max(0, len(unissued) - sim.free_slots() - sim.pending_stores())):
            v = self._pick_victim(sim, protected)
            if v is None or not sim.try_store(v):
                break
            progress = True
        if progress:
            return True

        # 4) PREFETCH ----------------------------------------------------------------------
        if self.W > 0 and not unissued:
            cands = [i for i in sim.soon_ready_ops() if i not in protected]
            cands = sorted(cands, key=lambda i: (-(crit[i] // self.tol), i))[: self.W]
            for i in cands:
                for q in c.ops[i].qubits:
                    if sim.state(q) != "SAM":
                        continue
                    if sim.free_slots() > 0:
                        progress |= sim.try_load(q)
                    elif self.pe and sim.free_slots() + sim.pending_stores() == 0:
                        v = self._pick_victim(sim, protected | set(c.ops[i].qubits),
                                              must_exceed=self._target_dist(sim, i))
                        if v is not None:
                            progress |= sim.try_store(v)
        return progress


POLICIES = {
    "demand_lru":      lambda **kw: FixedOrderPolicy(0, "lru", "demand-lru"),
    "demand_belady":   lambda **kw: FixedOrderPolicy(0, "belady", "demand-belady"),
    "prefetch_lru":    lambda lookahead=8, **kw: FixedOrderPolicy(lookahead, "lru", f"prefetch-lru(W={lookahead})"),
    "prefetch_belady": lambda lookahead=8, **kw: FixedOrderPolicy(lookahead, "belady", f"prefetch-belady(W={lookahead})"),
    "dag":             lambda prefetch_window=0, **kw: DagPolicy(prefetch_window, **kw),
    # legacy aliases (kept so older configs / smoke.py still work)
    "lsqca_lazy":      lambda **kw: FixedOrderPolicy(0, "lru", "lsqca-lazy"),
    "fixed_prefetch":  lambda lookahead=8, **kw: FixedOrderPolicy(lookahead, "lru", f"fixed-prefetch(W={lookahead})"),
    "fixed_belady":    lambda lookahead=8, **kw: FixedOrderPolicy(lookahead, "belady", f"fixed-belady(W={lookahead})"),
}


def make_policy(name, **params):
    if name not in POLICIES:
        raise KeyError(f"unknown policy {name!r}; available: {sorted(POLICIES)}")
    return POLICIES[name](**params)
