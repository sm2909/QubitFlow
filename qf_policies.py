"""Scheduling policies for QubitFlow.

The policy families form explicit ablations:
- fixed-order LRU: no gate reordering, demand paging.
- DAG-LRU: reorder ready gates, hold the replacement policy fixed.
- DAG-farthest-next-use: change residency replacement, hold gate ordering fixed.
- Joint: add dependency-frontier prefetch to DAG scheduling and replacement.
- nearest-transfer variants: change transfer-request arbitration only.

Farthest-next-use is Belady-inspired. With nonuniform transfer costs and bank
contention it is not an optimal replacement oracle.
"""


class FixedOrderPolicy:
    def __init__(self, lookahead=0, eviction="lru", name=None):
        if eviction not in ("lru", "farthest_next_use"):
            raise ValueError("eviction must be 'lru' or 'farthest_next_use'")
        if not isinstance(lookahead, int) or isinstance(lookahead, bool) or lookahead < 0:
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
        # Offline farthest-next-use replacement for the fixed operation order.
        return max(candidates, key=lambda q: (sim.next_use(q), -sim.last_used[q], -q))

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
                    # Prefetch is opportunistic: never evict solely to prefetch.
                    if sim.free_slots() + sim.pending_stores() >= missing_head:
                        continue
                    victim = self._victim(sim, protected)
                    if victim is not None:
                        progress |= sim.try_store(victim)
        return progress


class JointPolicy:
    """Deterministic DAG-aware scheduler with independently selectable ablations.

    Gate issue ranks dependency-ready operations by resident operand count,
    duration-weighted critical path, then stable operation ID. Residency uses
    either LRU or a DAG-aware farthest-next-use estimate. Demand transfers are
    ordered either by operation criticality or by shortest current row travel.
    Optional prefetch loads operands for operations whose remaining predecessors
    are already in flight; it never evicts a resident qubit to prefetch.
    """

    def __init__(
        self,
        prefetch=False,
        prefetch_width=0,
        eviction="lru",
        transfer_priority="critical",
        name="dag",
    ):
        if not isinstance(prefetch_width, int) or isinstance(prefetch_width, bool) or prefetch_width < 0:
            raise ValueError("prefetch_width must be a non-negative integer")
        if eviction not in ("lru", "farthest_next_use"):
            raise ValueError("eviction must be 'lru' or 'farthest_next_use'")
        if transfer_priority not in ("critical", "nearest"):
            raise ValueError("transfer_priority must be 'critical' or 'nearest'")
        if prefetch and prefetch_width == 0:
            raise ValueError("prefetch requires prefetch_width > 0")
        self.prefetch = bool(prefetch)
        self.prefetch_width = prefetch_width
        self.eviction = eviction
        self.transfer_priority = transfer_priority
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
                parents = [p for p in sim.circuit.preds[node] if p not in sim.finished]
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
        use = min(uses, key=lambda i: (remaining[i], -critical[i], i))
        return (remaining[use], -critical[use], use)

    def _victim(self, sim, protected, critical, remaining):
        candidates = [q for q in sim.loc if sim.state(q) == "CR"
                      and q not in sim.busy and q not in protected]
        if not candidates:
            return None
        if self.eviction == "lru":
            return min(candidates, key=lambda q: (sim.last_used[q], q))
        return max(
            candidates,
            key=lambda q: (
                self._next_use_key(sim, q, critical, remaining)[0],
                self._next_use_key(sim, q, critical, remaining)[1],
                -sim.last_used[q],
                -q,
            ),
        )

    def _transfer_key(self, sim, op_id, q, critical):
        _, bank, cell = sim.loc[q]
        latency = sim.cfg.xfer_time(cell, sim.scan_row[bank])
        if self.transfer_priority == "nearest":
            return (latency, -critical[op_id], op_id, q)
        return (-critical[op_id], latency, op_id, q)

    def _prefetch_frontier(self, sim):
        """Operations blocked only by predecessors already started/in flight."""
        frontier = []
        for op_id, parents in enumerate(sim.circuit.preds):
            if op_id in sim.started or not parents:
                continue
            if (any(parent in sim.started and parent not in sim.finished for parent in parents)
                    and all(parent in sim.started for parent in parents)):
                frontier.append(op_id)
        return frontier

    def step(self, sim):
        progress = False
        critical, remaining = self._graph(sim)
        ready = sim.ready_ops()
        ranked = sorted(ready, key=lambda i: self._rank(sim, i, critical), reverse=True)

        # Issue every currently executable ready operation in priority order.
        for op_id in ranked:
            progress |= sim.try_exec(op_id)

        # Select a priority prefix with a feasible CR operand working set.
        ready = sim.ready_ops()
        ranked = sorted(ready, key=lambda i: self._rank(sim, i, critical), reverse=True)
        selected = []
        protected = set()
        for op_id in ranked:
            operands = set(sim.circuit.ops[op_id].qubits)
            if len(protected | operands) <= sim.cfg.cr_slots:
                selected.append(op_id)
                protected.update(operands)
            if len(protected) == sim.cfg.cr_slots:
                break

        # Dispatch demand loads in the configured transfer-arbitration order.
        requests = [
            (op_id, q)
            for op_id in selected
            for q in sim.circuit.ops[op_id].qubits
            if sim.state(q) == "SAM"
        ]
        requests.sort(key=lambda item: self._transfer_key(sim, item[0], item[1], critical))
        for op_id, q in requests:
            if sim.free_slots() == 0:
                victim = self._victim(sim, protected, critical, remaining)
                if victim is not None:
                    progress |= sim.try_store(victim)
            else:
                progress |= sim.try_load(q)

        # Prefetch operands for imminent successors of in-flight operations.
        # This lets transfer latency overlap gate execution without evicting
        # qubits needed by the currently selected ready-operation prefix.
        if self.prefetch and self.prefetch_width:
            frontier = self._prefetch_frontier(sim)
            frontier.sort(key=lambda i: (remaining[i], -critical[i], i))
            prefetch_requests = []
            seen_qubits = set()
            for op_id in frontier:
                for q in sim.circuit.ops[op_id].qubits:
                    if q not in seen_qubits and sim.state(q) == "SAM":
                        prefetch_requests.append((op_id, q))
                        seen_qubits.add(q)
            prefetch_requests.sort(
                key=lambda item: self._transfer_key(sim, item[0], item[1], critical)
            )
            count = 0
            for _, q in prefetch_requests:
                if count >= self.prefetch_width or sim.free_slots() <= 0:
                    break
                if sim.try_load(q):
                    progress = True
                    count += 1
        return progress


POLICIES = {
    "lsqca_lazy": lambda **kw: FixedOrderPolicy(0, "lru", "lsqca-lazy"),
    "fixed_prefetch": lambda lookahead=8, **kw: FixedOrderPolicy(
        lookahead, "lru", f"fixed-prefetch(W={lookahead})"
    ),
    "fixed_farthest_next_use": lambda lookahead=8, **kw: FixedOrderPolicy(
        lookahead, "farthest_next_use", f"fixed-farthest-next-use(W={lookahead})"
    ),
    "dag_lru": lambda **kw: JointPolicy(
        False, 0, "lru", "critical", "dag-lru"
    ),
    "dag_next_use": lambda **kw: JointPolicy(
        False, 0, "farthest_next_use", "critical", "dag-farthest-next-use"
    ),
    "dag_next_use_nearest": lambda **kw: JointPolicy(
        False, 0, "farthest_next_use", "nearest", "dag-nearest-transfer"
    ),
    "joint": lambda prefetch_width=2, transfer_priority="critical", **kw: JointPolicy(
        True, prefetch_width, "farthest_next_use", transfer_priority,
        f"joint-prefetch(W={prefetch_width},{transfer_priority})"
    ),
}


def make_policy(name, **params):
    try:
        return POLICIES[name](**params)
    except KeyError as exc:
        raise ValueError(f"unknown policy {name!r}; choices: {', '.join(POLICIES)}") from exc
