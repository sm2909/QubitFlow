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

from qf_core import INF


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
    Optional prefetch loads operands for operations whose predecessors have
    started and at least one remains in flight; it never evicts for prefetch.
    Static weighted topological depth and critical path are cached per circuit.
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
        # Qubits in this set were loaded speculatively and remain counted
        # against the configured window until the first consuming op starts.
        self.prefetched_qubits = set()
        # Active window: speculative qubits currently in transfer or resident.
        self._prefetched_next_use = {}
        # Suppression map: do not retry a speculative load for the same next use
        # after eviction; otherwise zero-cost load/evict cycles are possible.
        self._prefetch_attempted_for = {}
        self._critical = None
        self._depth = None

    def _graph(self, sim):
        """Compute duration-weighted DAG depth/critical paths once per policy."""
        if self._critical is None:
            n = len(sim.circuit.ops)
            successors = [[] for _ in range(n)]
            for node, parents in enumerate(sim.circuit.preds):
                for parent in parents:
                    successors[parent].append(node)

            durations = [
                sim.cfg.timing.op_duration(op.kind, op.name)
                for op in sim.circuit.ops
            ]
            depth = [0] * n
            for node, parents in enumerate(sim.circuit.preds):
                depth[node] = durations[node] + max(
                    (depth[parent] for parent in parents), default=0
                )

            critical = [0] * n
            for node in range(n - 1, -1, -1):
                critical[node] = durations[node] + max(
                    (critical[child] for child in successors[node]), default=0
                )
            self._critical, self._depth = critical, depth
        return self._critical, self._depth

    def _rank(self, sim, op_id, critical):
        op = sim.circuit.ops[op_id]
        resident = sum(sim.state(q) == "CR" for q in op.qubits)
        return (resident, critical[op_id], -op_id)

    def _refresh_prefetch_window(self, sim):
        """Release active slots and retry suppression after a consumer starts."""
        consumed = {
            q for q, expected_next_use in self._prefetch_attempted_for.items()
            if sim.next_use(q) != expected_next_use
        }
        for q in consumed:
            self._prefetch_attempted_for.pop(q, None)
            self._prefetched_next_use.pop(q, None)

        self.prefetched_qubits = {
            q for q in self.prefetched_qubits
            if q not in consumed and sim.state(q) in ("XFER", "CR")
        }
        self._prefetched_next_use = {
            q: expected_next_use
            for q, expected_next_use in self._prefetched_next_use.items()
            if q in self.prefetched_qubits
        }

    def _prefetch_allowed(self, sim, q):
        """Do not retry a speculative load until its snapshotted consumer starts."""
        return q not in self._prefetch_attempted_for

    def _next_use_key(self, sim, q, critical, depth):
        use = sim.next_use(q)
        if use == INF:
            return (float("inf"), 0, float("inf"))
        # Static weighted topological depth estimates when the next use becomes
        # dependency-reachable; the critical-path tie-break protects urgent uses.
        return (depth[use], -critical[use], use)

    def _victim(self, sim, protected, critical, depth):
        candidates = [q for q in sim.loc if sim.state(q) == "CR"
                      and q not in sim.busy and q not in protected]
        if not candidates:
            return None
        if self.eviction == "lru":
            return min(candidates, key=lambda q: (sim.last_used[q], q))
        return max(
            candidates,
            key=lambda q: (
                self._next_use_key(sim, q, critical, depth)[0],
                self._next_use_key(sim, q, critical, depth)[1],
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
        """Unstarted successors of in-flight ops whose every predecessor started.

        Iterate only the successors of currently running operations rather than
        scanning the full circuit on every scheduling decision.
        """
        frontier = set()
        for _, parent in sim.running:
            for op_id in sim.successors[parent]:
                if op_id in sim.started:
                    continue
                parents = sim.circuit.preds[op_id]
                if all(pred in sim.started for pred in parents):
                    frontier.add(op_id)
        return sorted(frontier)

    def step(self, sim):
        progress = False
        self._refresh_prefetch_window(sim)
        critical, depth = self._graph(sim)
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
                victim = self._victim(sim, protected, critical, depth)
                if victim is not None:
                    progress |= sim.try_store(victim)
            else:
                progress |= sim.try_load(q)

        # Prefetch operands for imminent successors of in-flight operations.
        # This lets transfer latency overlap gate execution without evicting
        # qubits needed by the currently selected ready-operation prefix.
        if self.prefetch and self.prefetch_width:
            frontier = self._prefetch_frontier(sim)
            frontier.sort(key=lambda i: (depth[i], -critical[i], i))
            prefetch_requests = []
            seen_qubits = set()
            for op_id in frontier:
                for q in sim.circuit.ops[op_id].qubits:
                    if (q not in seen_qubits and sim.state(q) == "SAM"
                            and self._prefetch_allowed(sim, q)):
                        prefetch_requests.append((op_id, q))
                        seen_qubits.add(q)
            prefetch_requests.sort(
                key=lambda item: self._transfer_key(sim, item[0], item[1], critical)
            )
            # Width is the maximum number of speculative qubits outstanding
            # (in flight or resident), not merely a per-call issue count.
            available = max(0, self.prefetch_width - len(self.prefetched_qubits))
            count = 0
            for _, q in prefetch_requests:
                if count >= available or sim.free_slots() <= 0:
                    break
                if sim.try_load(q):
                    progress = True
                    count += 1
                    self.prefetched_qubits.add(q)
                    expected_next_use = sim.next_use(q)
                    self._prefetched_next_use[q] = expected_next_use
                    self._prefetch_attempted_for[q] = expected_next_use
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
