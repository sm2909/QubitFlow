"""Scheduling policies for QubitFlow.

FixedOrderPolicy provides program-order memory-management baselines.
JointPolicy combines DAG-ready operation selection, CR residency, demand loads,
conservative eviction, and opportunistic prefetch. It is a deterministic heuristic,
not an optimal scheduler.
"""
from qf_core import INF


class FixedOrderPolicy:
    def __init__(self, lookahead=0, eviction="lru", name=None):
        if eviction not in ("lru", "belady"):
            raise ValueError("eviction must be 'lru' or 'belady'")
        if lookahead < 0:
            raise ValueError("lookahead must be non-negative")
        self.lookahead, self.eviction = lookahead, eviction
        self.name = name or f"fixed(W={lookahead},{eviction})"

    def _victim(self, sim, protect, target_next=None):
        candidates = [q for q in sim.loc if sim.state(q) == "CR"
                      and q not in sim.busy and q not in protect]
        if not candidates:
            return None
        if self.eviction == "lru":
            return min(candidates, key=lambda q: sim.last_used[q])
        victim = max(candidates, key=lambda q: (sim.next_use(q), -sim.last_used[q]))
        if target_next is not None and sim.next_use(victim) <= target_next:
            return None
        return victim

    def step(self, sim):
        circuit, n, progress = sim.circuit, len(sim.circuit.ops), False
        while sim.head() < n and sim.try_exec(sim.head()):
            progress = True
        if sim.head() >= n:
            return progress
        head = sim.head()
        protected = set(circuit.ops[head].qubits)
        for i in range(head, min(n, head + 1 + self.lookahead)):
            demand = i == head
            for q in circuit.ops[i].qubits:
                if sim.state(q) != "SAM":
                    continue
                missing_head = sum(sim.state(x) == "SAM" for x in protected)
                if sim.free_slots() > (0 if demand else missing_head):
                    progress |= sim.try_load(q)
                elif demand or self.eviction == "belady":
                    pending = sim.pending_stores()
                    if sim.free_slots() + pending >= missing_head + (0 if demand else 1):
                        continue
                    victim = self._victim(sim, protected, None if demand else sim.next_use(q))
                    if victim is not None:
                        progress |= sim.try_store(victim)
        return progress


class JointPolicy:
    """Greedy DAG-aware scheduler coordinating issue, residency and transfer starts.

    Priority: already-resident ready ops, then critical-path urgency, then op ID.
    Demand loads outrank prefetch. Eviction protects operands of top ready ops and
    uses farthest next use (with LRU tie-break). The simulator arbitrates bank/lane
    resources through try_load/try_store.
    """
    def __init__(self, prefetch=True, prefetch_width=2, name="joint"):
        if prefetch_width < 0:
            raise ValueError("prefetch_width must be non-negative")
        self.prefetch = prefetch
        self.prefetch_width = prefetch_width
        self.name = name
        self._critical = None

    def _critical_lengths(self, sim):
        if self._critical is not None:
            return self._critical
        preds = sim.circuit.preds
        succ = [[] for _ in preds]
        for node, parents in enumerate(preds):
            for parent in parents:
                succ[parent].append(node)
        lengths = [1] * len(preds)
        for node in range(len(preds) - 1, -1, -1):
            if succ[node]:
                lengths[node] = 1 + max(lengths[x] for x in succ[node])
        self._critical = lengths
        return lengths

    def _rank(self, sim, op_id, critical):
        op = sim.circuit.ops[op_id]
        resident = sum(sim.state(q) == "CR" for q in op.qubits)
        return (resident, critical[op_id], -op_id)

    def _victim(self, sim, protected):
        candidates = [q for q in sim.loc if sim.state(q) == "CR"
                      and q not in sim.busy and q not in protected]
        if not candidates:
            return None
        # Farthest future use first; qubits with no remaining use are ideal victims.
        return max(candidates, key=lambda q: (sim.next_use(q), -sim.last_used[q]))

    def step(self, sim):
        progress = False
        critical = self._critical_lengths(sim)
        ready = sim.ready_ops()
        ranked = sorted(ready, key=lambda i: self._rank(sim, i, critical), reverse=True)

        # First issue every currently executable high-priority ready operation.
        for op_id in ranked:
            progress |= sim.try_exec(op_id)

        # Demand-load operands for high-priority ready operations.
        ready = sim.ready_ops()
        ranked = sorted(ready, key=lambda i: self._rank(sim, i, critical), reverse=True)
        protected = set()
        for op_id in ranked[:max(1, sim.cfg.cr_slots // 2)]:
            protected.update(sim.circuit.ops[op_id].qubits)

        for op_id in ranked:
            for q in sim.circuit.ops[op_id].qubits:
                if sim.state(q) != "SAM":
                    continue
                if sim.free_slots() == 0:
                    victim = self._victim(sim, protected)
                    if victim is not None:
                        progress |= sim.try_store(victim)
                else:
                    progress |= sim.try_load(q)

        # Prefetch operands for soon-to-be-ready operations only when slots and
        # transfer resources are currently available; never evict for prefetch.
        if self.prefetch and self.prefetch_width:
            remaining = [i for i in range(len(sim.circuit.ops)) if i not in sim.started]
            candidates = sorted(remaining, key=lambda i: (critical[i], -i), reverse=True)
            count = 0
            for op_id in candidates:
                if op_id in ready:
                    continue
                for q in sim.circuit.ops[op_id].qubits:
                    if count >= self.prefetch_width:
                        break
                    if sim.state(q) == "SAM" and sim.free_slots() > 0:
                        if sim.try_load(q):
                            progress = True
                            count += 1
                if count >= self.prefetch_width:
                    break
        return progress


class PolicyTemplate:
    """Duck-typed policy interface. step(sim) returns whether it started work."""
    name = "template"

    def step(self, sim):
        raise NotImplementedError


POLICIES = {
    "lsqca_lazy": lambda **kw: FixedOrderPolicy(0, "lru", "lsqca-lazy"),
    "fixed_prefetch": lambda lookahead=8, **kw: FixedOrderPolicy(lookahead, "lru", f"fixed-prefetch(W={lookahead})"),
    "fixed_belady": lambda lookahead=8, **kw: FixedOrderPolicy(lookahead, "belady", f"fixed-belady(W={lookahead})"),
    "joint": lambda prefetch=True, prefetch_width=2, **kw: JointPolicy(prefetch, prefetch_width),
    "dag_no_prefetch": lambda **kw: JointPolicy(False, 0, "dag-no-prefetch"),
}


def make_policy(name, **params):
    try:
        return POLICIES[name](**params)
    except KeyError as exc:
        raise ValueError(f"unknown policy {name!r}; choices: {', '.join(POLICIES)}") from exc
