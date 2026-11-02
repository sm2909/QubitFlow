"""qf_core.py -- engine for the QubitFlow baseline study (single file, no dependencies).

Sections (in order):
  1. CONFIG    ArchConfig, Timing         <- all architecture knobs live here
  2. CIRCUIT   Circuit DAG + loaders      <- synthetic / Qiskit / QASM / MQT Bench
  3. SIM       Simulator                  <- owns state, enforces every rule
  4. VALIDATE  validate_schedule          <- independent trace checker

!! All numeric defaults are PLACEHOLDERS, not values from the LSQCA paper. Calibrate first.
Units: time = "beat" (~d code cycles, one lattice-surgery step); space = "tile" (2d^2-1 physical qubits).

Simulator rules: a qubit is in exactly one of ("CR",slot) | ("SAM",bank,cell) | ("XFER",).
A transfer holds one bus lane + the bank's single port for its duration, reserves its
destination at start, frees its source at completion. An op starts only if preds are
finished, operands are resident+idle in CR, a parallel-op slot is free, and (T) a magic
state is in stock.
"""
from __future__ import annotations
import bisect, math, random
from dataclasses import dataclass, field, asdict

INF = float("inf")


# ============================================================================
# 1. CONFIG
# ============================================================================



@dataclass(frozen=True)
class Timing:
    t_1q: int = 1            # Clifford 1-qubit gate in CR (H, S, X, Z ...)
    t_2q: int = 2            # CX / CZ via lattice surgery inside CR
    t_T: int = 2             # T via magic-state injection + correction
    t_meas: int = 1
    xfer_base: int = 2       # fixed cost of one CR<->SAM transfer
    xfer_per_depth: int = 1  # extra cost per row of distance from the bank port
    magic_interval: int = 4  # factory emits one magic state every N beats
    magic_buffer: int = 4    # max magic states stored (also the initial stock)

    def op_duration(self, kind: str) -> int:
        return {"1q": self.t_1q, "2q": self.t_2q, "T": self.t_T, "meas": self.t_meas}[kind]


@dataclass(frozen=True)
class ArchConfig:
    d: int = 11                    # code distance (fixed in all experiments)
    cr_slots: int = 8              # computational-register capacity (logical qubits)
    n_banks: int = 2               # number of SAM banks, each with ONE access port
    bank_capacity: int = 64        # cells per bank (logical qubits)
    bank_width: int = 8            # cells per row; depth(cell) = cell // bank_width
    bus_lanes: int = 1             # simultaneous CR<->SAM transfers (shared routes)
    max_parallel_ops: int = 4      # concurrent lattice-surgery ops in CR (ancilla limit)
    timing: Timing = field(default_factory=Timing)

    # --- area model (tiles). Charge EVERYTHING that holds or moves qubits. ---
    cr_ancilla_ratio: float = 1.0  # routing/ancilla tiles per CR slot
    sam_overhead: float = 0.10     # port/scan/vacancy overhead per bank cell
    bus_tiles_per_lane: int = 8    # route + temporary buffer tiles per bus lane
    factory_tiles: int = 40        # magic-state factory footprint

    def tiles(self) -> float:
        cr = self.cr_slots * (1 + self.cr_ancilla_ratio)
        sam = self.n_banks * self.bank_capacity * (1 + self.sam_overhead)
        bus = self.bus_lanes * self.bus_tiles_per_lane
        return cr + sam + bus + self.factory_tiles

    def physical_qubits(self) -> int:
        return round(self.tiles() * (2 * self.d ** 2 - 1))

    def depth(self, cell: int) -> int:
        return cell // self.bank_width

    def xfer_time(self, cell: int) -> int:
        return self.timing.xfer_base + self.timing.xfer_per_depth * self.depth(cell)

# ============================================================================
# 2. CIRCUIT
# ============================================================================


_KIND = {"cx": "2q", "cz": "2q", "cy": "2q", "cp": "2q", "swap": "2q",
         "t": "T", "tdg": "T", "measure": "meas"}


@dataclass(frozen=True)
class Op:
    id: int
    kind: str
    qubits: tuple
    name: str


class Circuit:
    def __init__(self, n_qubits: int, ops: list, name: str = "circuit"):
        self.n_qubits, self.ops, self.name = n_qubits, ops, name
        last = {}
        self.preds = []
        self.uses = {q: [] for q in range(n_qubits)}
        for op in ops:
            self.preds.append(sorted({last[q] for q in op.qubits if q in last}))
            for q in op.qubits:
                last[q] = op.id
                self.uses[q].append(op.id)

    @classmethod
    def from_gates(cls, n_qubits, gates, name="circuit"):
        """gates: iterable of (gate_name, (q0, q1, ...))."""
        ops = [Op(i, _KIND.get(g, "1q"), tuple(qs), g) for i, (g, qs) in enumerate(gates)]
        assert all(1 <= len(o.qubits) <= 2 for o in ops), "decompose 3+ qubit gates first"
        return cls(n_qubits, ops, name)

    def stats(self):
        k = {}
        for o in self.ops:
            k[o.kind] = k.get(o.kind, 0) + 1
        return {"qubits": self.n_qubits, "ops": len(self.ops), **k}


# ---------------------------------------------------------------- synthetic benchmarks
def _toffoli(a, b, t):  # standard 7-T decomposition (controls a,b; target t)
    return [("h", (t,)), ("cx", (b, t)), ("tdg", (t,)), ("cx", (a, t)), ("t", (t,)),
            ("cx", (b, t)), ("tdg", (t,)), ("cx", (a, t)), ("t", (b,)), ("t", (t,)),
            ("h", (t,)), ("cx", (a, b)), ("t", (a,)), ("tdg", (b,)), ("cx", (a, b))]


def adder(n_bits: int) -> Circuit:
    """Cuccaro-style ripple-carry adder (2n+2 qubits), Clifford+T."""
    cin, z = 0, 2 * n_bits + 1
    A = lambda i: 1 + 2 * i
    B = lambda i: 2 + 2 * i
    C = lambda i: cin if i == 0 else A(i - 1)
    g = []
    for i in range(n_bits):                                  # MAJ
        g += [("cx", (A(i), B(i))), ("cx", (A(i), C(i)))] + _toffoli(C(i), B(i), A(i))
    g.append(("cx", (A(n_bits - 1), z)))
    for i in reversed(range(n_bits)):                        # UMA
        g += _toffoli(C(i), B(i), A(i)) + [("cx", (A(i), C(i))), ("cx", (C(i), B(i)))]
    return Circuit.from_gates(2 * n_bits + 2, g, f"adder{n_bits}")


def random_circuit(n_qubits: int, n_ops: int, p_t=0.3, p_2q=0.4, seed=0) -> Circuit:
    r = random.Random(seed)
    g = []
    for _ in range(n_ops):
        x = r.random()
        if x < p_2q:
            a, b = r.sample(range(n_qubits), 2); g.append(("cx", (a, b)))
        elif x < p_2q + p_t:
            g.append(("t", (r.randrange(n_qubits),)))
        else:
            g.append(("h", (r.randrange(n_qubits),)))
    return Circuit.from_gates(n_qubits, g, f"random{n_qubits}x{n_ops}")


# ---------------------------------------------------------------- Qiskit / MQT Bench
def from_qiskit(qc, rz_t_count: int = 4, name=None) -> Circuit:
    """Transpile to {cx,h,s,sdg,t,tdg,x,z,rz} (unrolls ccx etc.), then build the DAG.
    Arbitrary rz angles are charged as `rz_t_count` T gates (crude synthesis cost)."""
    from qiskit import transpile
    basis = ["cx", "h", "s", "sdg", "t", "tdg", "x", "z", "rz", "measure"]
    qc = transpile(qc, basis_gates=basis, optimization_level=0)
    gates = []
    for inst in qc.data:
        nm = inst.operation.name
        qs = tuple(qc.find_bit(q).index for q in inst.qubits)
        if nm in ("barrier", "delay"):
            continue
        if nm == "rz":
            ang = float(inst.operation.params[0])
            if abs(ang / (math.pi / 2) - round(ang / (math.pi / 2))) < 1e-9:
                gates.append(("s", qs))                       # Clifford angle
            elif abs(ang / (math.pi / 4) - round(ang / (math.pi / 4))) < 1e-9:
                gates.append(("t", qs))                       # odd multiple of pi/4
            else:
                gates += [("t", qs)] * rz_t_count
        else:
            gates.append((nm, qs))
    return Circuit.from_gates(qc.num_qubits, gates, name or getattr(qc, "name", "qiskit"))


def from_qasm(path: str, **kw) -> Circuit:
    from qiskit import QuantumCircuit
    return from_qiskit(QuantumCircuit.from_qasm_file(path), **kw)


def from_mqt_bench(benchmark: str, size: int, **kw) -> Circuit:
    """MQT Bench's API changed across releases; try the new signature, then the old."""
    try:
        from mqt.bench import get_benchmark, BenchmarkLevel
        qc = get_benchmark(benchmark=benchmark, level=BenchmarkLevel.ALG, circuit_size=size)
    except (ImportError, TypeError):
        from mqt.bench import get_benchmark
        qc = get_benchmark(benchmark_name=benchmark, level="alg", circuit_size=size)
    return from_qiskit(qc, name=f"{benchmark}{size}", **kw)

# ============================================================================
# 3. SIM
# ============================================================================

class DeadlockError(RuntimeError):
    pass


@dataclass
class Result:
    circuit: str
    policy: str
    cycles: int
    loads: int
    stores: int
    transfers: int
    exec_busy_cycles: int      # cycles with >=1 op executing
    mem_wait_cycles: int       # cycles - exec_busy: nothing computing (waiting on memory/deps/T)
    physical_qubits: int
    cr_slots: int
    n_banks: int
    bank_capacity: int

    def asdict(self):
        return asdict(self)


class Simulator:
    def __init__(self, circuit: Circuit, cfg: ArchConfig, policy):
        B = cfg.n_banks
        if cfg.cr_slots < 2:
            raise ValueError("cr_slots must be >= 2 (two-qubit ops need both operands resident)")
        if -(-circuit.n_qubits // B) > cfg.bank_capacity:
            raise ValueError("banks too small to hold all logical qubits")
        self.circuit, self.cfg, self.policy = circuit, cfg, policy
        self.t = 0
        self.loc = {}                                   # q -> ("CR",slot)|("SAM",bank,cell)|("XFER",)
        self.slot_owner = [None] * cfg.cr_slots
        self.cells = [[None] * cfg.bank_capacity for _ in range(B)]
        self.port_free = [0] * B
        self.lane_free = [0] * cfg.bus_lanes
        self.busy = set()                               # qubits inside a running op
        self.last_used = [0] * circuit.n_qubits
        self.started, self.finished = set(), set()
        self._head = 0
        self.running = []                               # (end, op_id)
        self.xfers = []                                 # dicts: end, kind, q, ...
        self.trace = []
        self.ms_stock, self.ms_next = cfg.timing.magic_buffer, cfg.timing.magic_interval
        self.initial_loc = {}
        nxt = [0] * B                                   # round-robin initial placement
        for q in range(circuit.n_qubits):
            b = q % B
            c = nxt[b]; nxt[b] += 1
            self.cells[b][c] = q
            self.loc[q] = self.initial_loc[q] = ("SAM", b, c)

    # ------------------------------------------------------------ queries (read-only)
    def state(self, q):
        return self.loc[q][0] if self.loc[q][0] != "XFER" else "XFER"

    def free_slots(self):
        return sum(o is None for o in self.slot_owner)

    def pending_stores(self):
        return sum(x["kind"] == "store" for x in self.xfers)

    def head(self):
        """Lowest-index op not yet started."""
        while self._head in self.started:
            self._head += 1
        return self._head

    def next_use(self, q):
        """Index of the first not-yet-started op using q (INF if none). Assumes the
        started set is a prefix (true for fixed-order policies)."""
        u = self.circuit.uses[q]
        i = bisect.bisect_left(u, self.head())
        return u[i] if i < len(u) else INF

    # ------------------------------------------------------------ actions
    def try_exec(self, i):
        c, cfg = self.circuit, self.cfg
        op = c.ops[i]
        if i in self.started or any(p not in self.finished for p in c.preds[i]):
            return False
        if len(self.running) >= cfg.max_parallel_ops:
            return False
        if any(self.state(q) != "CR" or q in self.busy for q in op.qubits):
            return False
        if op.kind == "T":
            self._tick()
            if self.ms_stock <= 0:
                return False
            if self.ms_stock == cfg.timing.magic_buffer:   # factory was stalled; restart it
                self.ms_next = self.t + cfg.timing.magic_interval
            self.ms_stock -= 1
        end = self.t + cfg.timing.op_duration(op.kind)
        self.started.add(i); self.running.append((end, i)); self.busy.update(op.qubits)
        for q in op.qubits:
            self.last_used[q] = self.t
        self.trace.append(dict(kind="op", id=i, start=self.t, end=end, qubits=op.qubits))
        return True

    def _lane(self):
        for k, f in enumerate(self.lane_free):
            if f <= self.t:
                return k
        return None

    def try_load(self, q, slot=None):
        """SAM -> CR. Destination slot reserved now; source cell freed on completion."""
        if self.state(q) != "SAM" or q in self.busy:
            return False
        _, b, cell = self.loc[q]
        lane = self._lane()
        if lane is None or self.port_free[b] > self.t:
            return False
        if slot is None:
            free = [s for s, o in enumerate(self.slot_owner) if o is None]
            if not free:
                return False
            slot = free[0]
        elif self.slot_owner[slot] is not None:
            return False
        end = self.t + self.cfg.xfer_time(cell)
        self.slot_owner[slot] = q
        self.port_free[b] = self.lane_free[lane] = end
        self.loc[q] = ("XFER",)
        self.xfers.append(dict(end=end, kind="load", q=q, bank=b, cell=cell, slot=slot))
        self.trace.append(dict(kind="load", q=q, start=self.t, end=end, bank=b, cell=cell,
                               slot=slot, lane=lane))
        return True

    def try_store(self, q, bank=None, cell=None):
        """CR -> SAM. Destination cell reserved now; CR slot freed on completion.
        Default placement: shallowest free cell over banks whose port is free."""
        if self.state(q) != "CR" or q in self.busy:
            return False
        lane = self._lane()
        if lane is None:
            return False
        slot = self.loc[q][1]
        cands = []
        for b in ([bank] if bank is not None else range(self.cfg.n_banks)):
            if self.port_free[b] > self.t:
                continue
            for cc in ([cell] if cell is not None else range(self.cfg.bank_capacity)):
                if self.cells[b][cc] is None:
                    cands.append((cc, b)); break          # shallowest free cell in this bank
        if not cands:
            return False
        cell, b = min(cands)
        end = self.t + self.cfg.xfer_time(cell)
        self.cells[b][cell] = q
        self.port_free[b] = self.lane_free[lane] = end
        self.loc[q] = ("XFER",)
        self.xfers.append(dict(end=end, kind="store", q=q, bank=b, cell=cell, slot=slot))
        self.trace.append(dict(kind="store", q=q, start=self.t, end=end, bank=b, cell=cell,
                               slot=slot, lane=lane))
        return True

    # ------------------------------------------------------------ engine
    def _tick(self):
        T = self.cfg.timing
        while self.ms_next <= self.t and self.ms_stock < T.magic_buffer:
            self.ms_stock += 1; self.ms_next += T.magic_interval

    def _complete(self):
        for e, i in [r for r in self.running if r[0] <= self.t]:
            self.running.remove((e, i)); self.finished.add(i)
            self.busy.difference_update(self.circuit.ops[i].qubits)
        for x in [x for x in self.xfers if x["end"] <= self.t]:
            self.xfers.remove(x); q = x["q"]
            if x["kind"] == "load":
                self.cells[x["bank"]][x["cell"]] = None
                self.loc[q] = ("CR", x["slot"]); self.last_used[q] = self.t
            else:
                self.slot_owner[x["slot"]] = None
                self.loc[q] = ("SAM", x["bank"], x["cell"])

    def run(self) -> Result:
        n = len(self.circuit.ops)
        while len(self.finished) < n:
            self._tick()
            while self.policy.step(self):
                pass
            cands = [e for e, _ in self.running] + [x["end"] for x in self.xfers]
            if self.ms_stock < self.cfg.timing.magic_buffer and self.ms_next > self.t:
                cands.append(self.ms_next)
            if not cands:
                raise DeadlockError(f"no progress at t={self.t}, policy={self.policy.name}")
            self.t = min(cands)
            self._complete()
        ops = sorted((e["start"], e["end"]) for e in self.trace if e["kind"] == "op")
        busy_cycles, cur_s, cur_e = 0, None, None
        for s, e in ops:                                  # union of op intervals
            if cur_e is None or s > cur_e:
                if cur_e is not None:
                    busy_cycles += cur_e - cur_s
                cur_s, cur_e = s, e
            else:
                cur_e = max(cur_e, e)
        busy_cycles += (cur_e - cur_s) if cur_e is not None else 0
        L = sum(e["kind"] == "load" for e in self.trace)
        S = sum(e["kind"] == "store" for e in self.trace)
        return Result(self.circuit.name, self.policy.name, self.t, L, S, L + S, busy_cycles,
                      self.t - busy_cycles, self.cfg.physical_qubits(), self.cfg.cr_slots,
                      self.cfg.n_banks, self.cfg.bank_capacity)

# ============================================================================
# 4. VALIDATE
# ============================================================================

def _no_overlap(ivs, label, errs):
    ivs = sorted(ivs, key=lambda x: x[:2])
    for a, b in zip(ivs, ivs[1:]):
        if b[0] < a[1]:
            errs.append(f"{label}: overlap {a} vs {b}")


def _max_concurrency(ivs):
    ev = [(s, 1) for s, e in ivs] + [(e, -1) for s, e in ivs]
    cur = best = 0
    for _, d in sorted(ev):                     # ends (-1) sort before starts (+1) at ties
        cur += d; best = max(best, cur)
    return best


def validate_schedule(trace, circuit, cfg, initial_loc):
    errs = []
    ops = [e for e in trace if e["kind"] == "op"]
    seen = {}
    for e in ops:
        if e["id"] in seen:
            errs.append(f"op {e['id']} executed twice")
        seen[e["id"]] = e
    if len(seen) != len(circuit.ops):
        errs.append(f"{len(circuit.ops) - len(seen)} ops never executed")
    for i, e in seen.items():
        for p in circuit.preds[i]:
            if p in seen and seen[p]["end"] > e["start"]:
                errs.append(f"dependency violation: op {i} starts {e['start']} before pred {p} ends {seen[p]['end']}")
        if e["end"] - e["start"] != cfg.timing.op_duration(circuit.ops[i].kind):
            errs.append(f"op {i} has wrong duration")

    per_q = {q: [] for q in range(circuit.n_qubits)}
    for e in trace:
        for q in (e["qubits"] if e["kind"] == "op" else (e["q"],)):
            per_q[q].append(e)
    slot_iv, cell_iv = {}, {}
    for q, evs in per_q.items():
        evs.sort(key=lambda e: (e["start"], e["end"]))
        state, prev_end = "SAM", 0
        _, b0, c0 = initial_loc[q]
        open_cell, open_slot = (b0, c0, 0), None
        for e in evs:
            if e["start"] < prev_end:
                errs.append(f"q{q}: events overlap in time at {e['start']}")
            prev_end = max(prev_end, e["end"])
            if e["kind"] == "load":
                if state != "SAM": errs.append(f"q{q}: load while in {state} (t={e['start']})")
                if open_cell is None or open_cell[:2] != (e["bank"], e["cell"]):
                    errs.append(f"q{q}: load from wrong cell")
                else:
                    cell_iv.setdefault(open_cell[:2], []).append((open_cell[2], e["end"], q))
                open_cell, open_slot, state = None, (e["slot"], e["start"]), "CR"
            elif e["kind"] == "store":
                if state != "CR": errs.append(f"q{q}: store while in {state} (t={e['start']})")
                if open_slot is None or open_slot[0] != e["slot"]: errs.append(f"q{q}: store from wrong slot")
                else: slot_iv.setdefault(e["slot"], []).append((open_slot[1], e["end"], q))
                open_cell, open_slot, state = (e["bank"], e["cell"], e["start"]), None, "SAM"
            else:
                if state != "CR": errs.append(f"q{q}: op {e['id']} at t={e['start']} but qubit not in CR (not arrived)")
        if open_cell is not None:
            cell_iv.setdefault(open_cell[:2], []).append((open_cell[2], INF, q))
        if open_slot is not None:
            slot_iv.setdefault(open_slot[0], []).append((open_slot[1], INF, q))
    for s, iv in slot_iv.items():
        _no_overlap(iv, f"CR slot {s}", errs)
    for k, iv in cell_iv.items():
        _no_overlap(iv, f"SAM cell {k}", errs)

    xf = [e for e in trace if e["kind"] in ("load", "store")]
    for b in range(cfg.n_banks):
        _no_overlap([(e["start"], e["end"], e["q"]) for e in xf if e["bank"] == b], f"bank {b} port", errs)
    if _max_concurrency([(e["start"], e["end"]) for e in xf]) > cfg.bus_lanes:
        errs.append("bus-lane limit exceeded")
    if _max_concurrency([(e["start"], e["end"]) for e in ops]) > cfg.max_parallel_ops:
        errs.append("parallel-op limit exceeded")
    return errs