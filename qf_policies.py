"""Scheduling policies for QubitFlow.

FixedOrderPolicy supplies program-order LRU and offline Belady baselines.
JointPolicy is a deterministic DAG-aware heuristic coordinating operation
selection, CR residency, eviction, demand loading, and conservative prefetch.
It is not an optimal scheduler.
"""


class FixedOrderPolicy:
    def __init__(self, lookahead=0, eviction="lru", name=None):
        if eviction not in ("lru", "belady"):
            raise ValueError("eviction must be 'lru' or 'belady'")
        if not isinstance(lookahead, int) or lookahead < 0:
            raise ValueError("lookahead must be a non-negative integer")
        self.lookahead, self.eviction = lookahead, eviction
        self.name = name or f"fixed(W={lookahead},{eviction})"

    def _victim(self, sim, protect):
        candidates = [q for q in sim.loc if sim.state(q) == "CR"
                      and q not in sim.busy and q not in protect]
        if not candidates:
            return None
        if self.eviction == "lru":
            return min(candidates, key=lambda q: (sim.last_used[q], q))
        # Offline Belady oracle for fixed program-order issue.
        victim = max(candidates, key=lambda q: (sim.next_use(q), -sim.last_used[q]))
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
                reserve = 0 if demand else missing_head
                if sim.free_slots() > reserve:
                    progress |= sim.try_load(q)
                elif demand:
                    # Evict only to satisfy the current head operation. Prefetch
                    # is opportunistic and must never evict; otherwise Belady
                    # lookahead can oscillate between future operands.
                    pending = sim.pending_stores()
                    if sim.free_slots() + pending >= missing_head:
                        continue
                    victim = self._victim(sim, protected)
                    if victim is not None:
                        progress |= sim.try_store(victim)
        return progress


class JointPolicy:
    """Greedy DAG scheduler with weighted critical paths and DAG-aware eviction.

    Ready-operation priority is (resident operands, weighted critical path,
    stable operation ID). For a qubit, next use is estimated from the longest
    remaining dependency path to each not-yet-started use. Eviction chooses the
    qubit whose first future use is furthest away, protecting higher-criticality
    uses on ties. Prefetch is conservative: it is disabled while a ready
    operation still has nonresident operands, and it never evicts for prefetch.
    """

    def __init__(self, prefetch=True, prefetch_width=2, name="joint"):
        if not isinstance(prefetch_width, int) or prefetch_width < 0:
            raise ValueError("prefetch_width must be a non-negative integer")
        self.prefetch = bool(prefetch)
        self.prefetch_width = prefetch_width
        self.name = name

    def _graph(self, sim):
        n = len(sim.circuit.ops)
        succ = [[] for _ in range(n)]
        for node, parents in enumerate(sim.circuit.preds):
            for parent in parents:
                succ[parent].append(node)

        durations = [
            sim.cfg.timing.op_duration(op.kind, op.name)
            for op in sim.circuit.ops
        ]
        critical = [0] * n
        for node in range(n - 1, -1, -1):
            critical[node] = durations[node] + max(
                (critical[child] for child in succ[node]), default=0
            )

        remaining_memo = {}
        def remaining_path(node):
            if node in sim.finished:
                return 0
            if node not in remaining_memo:
                parents = [p for p in sim.circuit.preds[node]
                           if p not in sim.finished]
                remaining_memo[node] = durations[node] + max(
                    (remaining_path(parent) for parent in parents), default=0
                )
            return remaining_memo[node]

        remaining = [remaining_path(i) for i in range(n)]
        return critical, remaining

    def _rank(self, sim, op_id, critical):
        op = sim.circuit.ops[op_id]
        resident = sum(sim.state(q) == "CR" for q in op.qubits)
        return (resident, critical[op_id], -op_id)

    def _next_use_key(self, sim, q, critical, remaining):
        uses = [i for i in sim.circuit.uses[q] if i not in sim.started]
        if not uses:
            return (float("inf"), 0, float("inf"))
        # Earliest dependency-feasible future use, not smallest operation ID.
        use = min(uses, key=lambda i: (remaining[i], -critical[i], i))
        return (remaining[use], -critical[use], use)

    def _victim(self, sim, protected, critical, remaining):
        candidates = [q for q in sim.loc if sim.state(q) == "CR"
                      and q not in sim.busy and q not in protected]
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda q: (
                self._next_use_key(sim, q, critical, remaining)[0],
                self._next_use_key(sim, q, critical, remaining)[1],
                -sim.last_used[q],
                -q,
            ),
        )

    def step(self, sim):
        progress = False
        critical, remaining = self._graph(sim)
        ready = sim.ready_ops()
        ranked = sorted(ready, key=lambda i: self._rank(sim, i, critical), reverse=True)

        # Issue all currently executable operations in deterministic priority order.
        for op_id in ranked:
            progress |= sim.try_exec(op_id)

        # Demand-load operands of ready operations before considering prefetch.
        ready = sim.ready_ops()
        ranked = sorted(ready, key=lambda i: self._rank(sim, i, critical), reverse=True)
        # Select a priority prefix whose unique operands fit in the CR. Do not
        # simultaneously demand-load mutually incompatible ready operations:
        # that creates zero-time eviction/load ping-pong when CR is small.
        selected = []
        protected = set()
        for op_id in ranked:
            operands = set(sim.circuit.ops[op_id].qubits)
            if len(protected | operands) <= sim.cfg.cr_slots:
                selected.append(op_id)
                protected.update(operands)
            if len(protected) == sim.cfg.cr_slots:
                break

        for op_id in selected:
            for q in sim.circuit.ops[op_id].qubits:
                if sim.state(q) != "SAM":
                    continue
                if sim.free_slots() == 0:
                    victim = self._victim(sim, protected, critical, remaining)
                    if victim is not None:
                        progress |= sim.try_store(victim)
                else:
                    progress |= sim.try_load(q)

        # Prefetch only during dependency bubbles, never displacing demand operands.
        demand_pending = any(
            any(sim.state(q) != "CR" for q in sim.circuit.ops[i].qubits)
            for i in ready
        )
        if self.prefetch and self.prefetch_width and not demand_pending and not ready:
            future = [i for i in range(len(sim.circuit.ops))
                      if i not in sim.started and remaining[i] > 0]
            future.sort(key=lambda i: (remaining[i], -critical[i], i))
            count = 0
            seen_qubits = set()
            for op_id in future:
                for q in sim.circuit.ops[op_id].qubits:
                    if count >= self.prefetch_width or sim.free_slots() <= 0:
                        break
                    if q in seen_qubits or sim.state(q) != "SAM":
                        continue
                    if sim.try_load(q):
                        progress = True
                        count += 1
                        seen_qubits.add(q)
                if count >= self.prefetch_width or sim.free_slots() <= 0:
                    break
        return progress


POLICIES = {
    "lsqca_lazy": lambda **kw: FixedOrderPolicy(0, "lru", "lsqca-lazy"),
    "fixed_prefetch": lambda lookahead=8, **kw: FixedOrderPolicy(
        lookahead, "lru", f"fixed-prefetch(W={lookahead})"
    ),
    "fixed_belady": lambda lookahead=8, **kw: FixedOrderPolicy(
        lookahead, "belady", f"fixed-belady(W={lookahead})"
    ),
    "joint": lambda prefetch=True, prefetch_width=2, **kw: JointPolicy(
        prefetch, prefetch_width,
        f"joint-prefetch(W={prefetch_width})" if prefetch else "dag-no-prefetch",
    ),
    "dag_no_prefetch": lambda **kw: JointPolicy(False, 0, "dag-no-prefetch"),
}


def make_policy(name, **params):
    try:
        return POLICIES[name](**params)
    except KeyError as exc:
        raise ValueError(f"unknown policy {name!r}; choices: {', '.join(POLICIES)}") from exc
