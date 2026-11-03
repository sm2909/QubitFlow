"""qf_core.py -- engine for the QubitFlow study (single file, no required dependencies).

Sections (in order):
  1. CONFIG    ArchConfig, Timing         <- all architecture knobs live here (validated)
  2. CIRCUIT   Circuit DAG + loaders      <- synthetic / Qiskit / QASM / MQT Bench
  3. SIM       Simulator                  <- owns state, enforces every rule
  4. VALIDATE  validate_schedule          <- independent trace checker

Units: time = "beat" (~d code cycles, one lattice-surgery step); space = "tile".

SAM models (ArchConfig.sam_model):
  "line"     Line-SAM following LSQCA (Kobori et al., HPCA'25, arXiv:2412.20486, Sec. IV-C.3):
             an explicit scan-line position per bank; load latency = row distance to the
             scan line (+ a constant port move); same-row accesses are free; locality-aware
             stores. See docs/ASSUMPTIONS.md for which constants are paper-derived.
  "generic"  LEGACY PLACEHOLDER (xfer_base + xfer_per_depth * row). Not a faithful SAM model;
             kept so earlier baseline numbers remain reproducible.
  Point-SAM is NOT implemented (raises NotImplementedError).

Simulator rules: a qubit is in exactly one of ("CR",slot) | ("SAM",bank,cell) | ("XFER",).
A transfer holds one bus lane + the bank's single port for its duration, reserves its
destination at start, frees its source at completion. An op starts only if preds are
finished, operands are resident+idle in CR, a parallel-op slot is free, and (T) a magic
state is in stock (unless magic_unlimited).
"""
from __future__ import annotations
import hashlib, json, math, random
from dataclasses import dataclass, field, asdict, fields

INF = float("inf")


class ConfigError(ValueError):
    """Invalid architecture / timing configuration."""


class CircuitError(ValueError):
    """Invalid circuit (indices, arity, duplicates, dependencies)."""


def _is_int(x):
    return isinstance(x, int) and not isinstance(x, bool)


def _req_int(name, v, lo=1):
    if not _is_int(v) or v < lo:
        raise ConfigError(f"{name} must be an integer >= {lo}, got {v!r}")


# ============================================================================
# 1. CONFIG
# ============================================================================
OP_KINDS = ("1q", "h", "s", "2q", "T", "meas")
_ARITY = {"1q": 1, "h": 1, "s": 1, "T": 1, "meas": 1, "2q": 2}


@dataclass(frozen=True)
class Timing:
    t_1q: int = 1            # Pauli / other 1-qubit gate in CR
    t_2q: int = 2            # CX / CZ via lattice surgery inside CR   (PLACEHOLDER)
    t_T: int = 2             # T via magic-state injection + correction (PLACEHOLDER)
    t_meas: int = 1
    t_h: int | None = None   # Hadamard; None -> t_1q   (paper: 3 beats)
    t_s: int | None = None   # S / Sdg;   None -> t_1q   (paper: 2 beats)
    # --- generic (legacy placeholder) SAM transfer model
    xfer_base: int = 2
    xfer_per_depth: int = 1
    # --- Line-SAM transfer model
    line_shift: int = 1      # beats to move the scan line by one row (paper: latency = row distance)
    port_move: int = 1       # beats for the final cell move into/out of CR (ASSUMPTION)
    # --- magic-state factory
    magic_interval: int = 4  # factory emits one state every N beats (paper: 15 for Litinski MSF)
    magic_buffer: int = 4    # max states stored (also the initial stock)
    magic_unlimited: bool = False   # True -> T gates never wait (isolates memory effects)

    def __post_init__(self):
        for n in ("t_1q", "t_2q", "t_T", "t_meas", "line_shift", "port_move",
                  "magic_interval", "magic_buffer"):
            _req_int(n, getattr(self, n), 1)
        for n in ("xfer_base", "xfer_per_depth"):
            _req_int(n, getattr(self, n), 0)
        if self.xfer_base + self.xfer_per_depth < 1:
            raise ConfigError("generic transfers must take at least 1 beat")
        for n in ("t_h", "t_s"):
            v = getattr(self, n)
            if v is not None:
                _req_int(n, v, 1)
        if not isinstance(self.magic_unlimited, bool):
            raise ConfigError("magic_unlimited must be a bool")

    def op_duration(self, kind: str) -> int:
        if kind == "h":
            return self.t_h if self.t_h is not None else self.t_1q
        if kind == "s":
            return self.t_s if self.t_s is not None else self.t_1q
        return {"1q": self.t_1q, "2q": self.t_2q, "T": self.t_T, "meas": self.t_meas}[kind]

    @classmethod
    def lsqca_paper(cls, **over):
        """Timing preset using the numbers stated in the LSQCA paper where available:
        H=3 beats, S=2 beats, lattice surgery = 1 beat, one magic state per 15 beats
        (Litinski MSF), 2n magic buffer (n=1 factory). T = ZZ-measurement (1) + S correction
        (2) = 3 beats (paper always takes the longer conditional path). t_2q=2 (CX as two
        lattice-surgery measurements) and t_1q=1 (paper treats Paulis as ~0 beats; we need
        >=1) are OUR assumptions."""
        base = dict(t_1q=1, t_2q=2, t_T=3, t_meas=1, t_h=3, t_s=2,
                    magic_interval=15, magic_buffer=2, line_shift=1, port_move=1)
        base.update(over)
        return cls(**base)


@dataclass(frozen=True)
class ArchConfig:
    d: int = 11                    # code distance (fixed in all experiments)
    cr_slots: int = 8              # computational-register capacity (logical qubits)
    n_banks: int = 2               # number of SAM banks, each with ONE access port
    bank_capacity: int = 64        # cells per bank (logical qubits)
    bank_width: int = 8            # cells per row; row(cell) = cell // bank_width
    bus_lanes: int = 1             # simultaneous CR<->SAM transfers (shared routes)
    max_parallel_ops: int = 4      # concurrent lattice-surgery ops in CR (ancilla limit)
    timing: Timing = field(default_factory=Timing)
    sam_model: str = "line"        # "line" | "generic" (legacy placeholder)

    # --- area model (tiles). PLACEHOLDERS except where noted.
    cr_ancilla_ratio: float = 1.0  # routing/ancilla tiles per CR slot
    sam_overhead: float = 0.10     # generic model only: port/scan overhead per bank cell
    bus_tiles_per_lane: int = 8    # route + temporary buffer tiles per bus lane
    factory_tiles: int = 40        # magic-state factory footprint (paper cites 176 for 15-beat MSF)

    def __post_init__(self):
        _req_int("d", self.d, 3)
        if self.d % 2 == 0:
            raise ConfigError(f"code distance d must be odd, got {self.d}")
        for n in ("cr_slots", "n_banks", "bank_capacity", "bank_width", "bus_lanes",
                  "max_parallel_ops"):
            _req_int(n, getattr(self, n), 1)
        if self.cr_slots < 2:
            raise ConfigError("cr_slots must be >= 2 (two-qubit ops need both operands resident)")
        if not isinstance(self.timing, Timing):
            raise ConfigError("timing must be a Timing instance")
        if self.sam_model == "point":
            raise NotImplementedError("Point-SAM is not implemented (planned as a separate model)")
        if self.sam_model not in ("line", "generic"):
            raise ConfigError(f"unknown sam_model {self.sam_model!r}")
        for n in ("cr_ancilla_ratio", "sam_overhead"):
            if not isinstance(getattr(self, n), (int, float)) or getattr(self, n) < 0:
                raise ConfigError(f"{n} must be a non-negative number")
        _req_int("bus_tiles_per_lane", self.bus_tiles_per_lane, 0)
        _req_int("factory_tiles", self.factory_tiles, 0)

    # --- geometry
    def rows(self) -> int:
        return -(-self.bank_capacity // self.bank_width)

    def row_of(self, cell: int) -> int:
        return cell // self.bank_width

    def depth(self, cell: int) -> int:                 # legacy name
        return self.row_of(cell)

    def initial_scan_row(self) -> int:
        """Scan line starts at the middle of the bank (paper: worst case = half the height)."""
        return self.rows() // 2

    # --- latency models
    def xfer_time(self, cell: int) -> int:             # generic (legacy) model
        return self.timing.xfer_base + self.timing.xfer_per_depth * self.row_of(cell)

    def line_cost(self, scan_row: int, cell: int) -> int:
        """Line-SAM transfer latency for touching `cell` with the scan line at `scan_row`:
        |row - scan_row| scan-line shifts, plus the final port move into/out of CR."""
        return abs(self.row_of(cell) - scan_row) * self.timing.line_shift + self.timing.port_move

    # --- area
    def tiles(self) -> float:
        cr = self.cr_slots * (1 + self.cr_ancilla_ratio)
        if self.sam_model == "line":
            # full rows of data cells + one auxiliary scan line (bank_width cells) per bank
            sam = self.n_banks * (self.rows() * self.bank_width + self.bank_width)
        else:
            sam = self.n_banks * self.bank_capacity * (1 + self.sam_overhead)
        bus = self.bus_lanes * self.bus_tiles_per_lane
        return cr + sam + bus + self.factory_tiles

    def physical_qubits(self) -> int:
        return round(self.tiles() * (2 * self.d ** 2 - 1))

    def to_dict(self):
        return asdict(self)

    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True).encode()).hexdigest()[:12]


# ============================================================================
# 2. CIRCUIT
# ============================================================================
_KIND = {"cx": "2q", "cz": "2q", "cy": "2q", "cp": "2q", "swap": "2q",
         "x": "1q", "y": "1q", "z": "1q", "id": "1q",
         "h": "h", "s": "s", "sdg": "s",
         "t": "T", "tdg": "T", "measure": "meas"}


@dataclass(frozen=True)
class Op:
    id: int
    kind: str
    qubits: tuple
    name: str


class Circuit:
    """Dependency DAG. Edges come from shared qubits (program order per qubit) plus optional
    `extra_deps` {op_id: [pred ids]} (e.g. classical control). Validated on construction."""

    def __init__(self, n_qubits: int, ops: list, name: str = "circuit", extra_deps=None):
        if not _is_int(n_qubits) or n_qubits < 1:
            raise CircuitError(f"n_qubits must be an integer >= 1, got {n_qubits!r}")
        self.n_qubits, self.ops, self.name = n_qubits, list(ops), name
        n = len(self.ops)
        for i, op in enumerate(self.ops):
            if op.id != i:
                raise CircuitError(f"operation at position {i} has id {op.id}; ids must equal position")
            if op.kind not in OP_KINDS:
                raise CircuitError(f"op {i}: unknown kind {op.kind!r}")
            if len(op.qubits) != _ARITY[op.kind]:
                raise CircuitError(f"op {i} ({op.name}): kind {op.kind} needs {_ARITY[op.kind]} "
                                   f"operand(s), got {len(op.qubits)}; decompose 3+ qubit gates first")
            if any(not _is_int(q) or not 0 <= q < n_qubits for q in op.qubits):
                raise CircuitError(f"op {i} ({op.name}): qubit index out of range [0,{n_qubits}): {op.qubits}")
            if len(set(op.qubits)) != len(op.qubits):
                raise CircuitError(f"op {i} ({op.name}): duplicate operands {op.qubits}")
        extra = {}
        for k, ps in (extra_deps or {}).items():
            if not _is_int(k) or not 0 <= k < n:
                raise CircuitError(f"extra_deps: bad op id {k!r}")
            for p in ps:
                if not _is_int(p) or not 0 <= p < n:
                    raise CircuitError(f"extra_deps: op {k} depends on invalid op {p!r}")
                if p == k:
                    raise CircuitError(f"extra_deps: op {k} depends on itself")
            extra[k] = set(ps)
        last = {}
        self.preds = []
        self.uses = {q: [] for q in range(n_qubits)}
        for op in self.ops:
            ps = {last[q] for q in op.qubits if q in last} | extra.get(op.id, set())
            self.preds.append(sorted(ps))
            for q in op.qubits:
                last[q] = op.id
                self.uses[q].append(op.id)
        self.succs = [[] for _ in range(n)]
        for i, ps in enumerate(self.preds):
            for p in ps:
                self.succs[p].append(i)
        self._check_acyclic()
        self._crit = {}
        self.level = self._levels()

    def _check_acyclic(self):
        indeg = [len(p) for p in self.preds]
        stack = [i for i, d in enumerate(indeg) if d == 0]
        seen = 0
        while stack:
            i = stack.pop(); seen += 1
            for s in self.succs[i]:
                indeg[s] -= 1
                if indeg[s] == 0:
                    stack.append(s)
        if seen != len(self.ops):
            raise CircuitError("dependency graph contains a cycle")

    def _topo(self):
        indeg = [len(p) for p in self.preds]
        order, stack = [], [i for i, d in enumerate(indeg) if d == 0]
        while stack:
            i = stack.pop(); order.append(i)
            for s in self.succs[i]:
                indeg[s] -= 1
                if indeg[s] == 0:
                    stack.append(s)
        return order

    def _levels(self):
        """ASAP unit depth (0 for sources)."""
        lv = [0] * len(self.ops)
        for i in self._topo():
            for s in self.succs[i]:
                lv[s] = max(lv[s], lv[i] + 1)
        return lv

    def critical_path(self, timing: Timing):
        """crit[i] = duration(i) + longest duration-weighted path to any sink (memory ignored)."""
        key = timing
        if key not in self._crit:
            crit = [0] * len(self.ops)
            for i in reversed(self._topo()):
                best = max((crit[s] for s in self.succs[i]), default=0)
                crit[i] = timing.op_duration(self.ops[i].kind) + best
            self._crit[key] = crit
        return self._crit[key]

    @classmethod
    def from_gates(cls, n_qubits, gates, name="circuit", extra_deps=None):
        """gates: iterable of (gate_name, (q0, q1, ...))."""
        ops = []
        for i, (g, qs) in enumerate(gates):
            if g not in _KIND:
                raise CircuitError(f"unsupported gate {g!r} (op {i}); supported: {sorted(_KIND)}")
            ops.append(Op(i, _KIND[g], tuple(qs), g))
        return cls(n_qubits, ops, name, extra_deps)

    def stats(self):
        k = {}
        for o in self.ops:
            k[o.kind] = k.get(o.kind, 0) + 1
        return {"qubits": self.n_qubits, "ops": len(self.ops), **k}

    def fingerprint(self) -> str:
        h = hashlib.sha256()
        h.update(json.dumps([self.n_qubits, [(o.kind, o.qubits, o.name) for o in self.ops],
                             self.preds]).encode())
        return h.hexdigest()[:12]


# ---------------------------------------------------------------- synthetic benchmarks
def _toffoli(a, b, t):  # standard 7-T decomposition (controls a,b; target t); verified in tests
    return [("h", (t,)), ("cx", (b, t)), ("tdg", (t,)), ("cx", (a, t)), ("t", (t,)),
            ("cx", (b, t)), ("tdg", (t,)), ("cx", (a, t)), ("t", (b,)), ("t", (t,)),
            ("h", (t,)), ("cx", (a, b)), ("t", (a,)), ("tdg", (b,)), ("cx", (a, b))]


def adder(n_bits: int) -> Circuit:
    """Cuccaro-style ripple-carry adder (2n+2 qubits), Clifford+T. Functional correctness of the
    reversible form is checked in tests (qf_workloads.adder_rev)."""
    if not _is_int(n_bits) or n_bits < 1:
        raise CircuitError("adder needs n_bits >= 1")
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
    """Seeded synthetic workload. `seed` is REQUIRED to be an int for reproducibility."""
    if not _is_int(seed):
        raise CircuitError("random_circuit requires an integer seed")
    if n_qubits < 2:
        raise CircuitError("random_circuit needs >= 2 qubits")
    if not (0 <= p_t and 0 <= p_2q and p_t + p_2q <= 1):
        raise CircuitError("need 0 <= p_t, p_2q and p_t + p_2q <= 1")
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
    return Circuit.from_gates(n_qubits, g, f"random{n_qubits}x{n_ops}s{seed}")


# ---------------------------------------------------------------- Qiskit / MQT Bench
def rz_to_gates(angle: float, qs: tuple, rz_t_count: int = 4):
    """Map rz(angle) to Clifford+T gate names. APPROXIMATION: multiples of pi/2 -> S (Pauli/
    Clifford angles, Z-type Paulis and S/Sdg all cost the same 'S' kind); odd multiples of
    pi/4 -> one T; any other angle is charged `rz_t_count` T gates (crude stand-in for
    gridsynth-style synthesis, NOT a real decomposition and not unitary-equivalent)."""
    if abs(angle / (math.pi / 2) - round(angle / (math.pi / 2))) < 1e-9:
        return [("s", qs)]
    if abs(angle / (math.pi / 4) - round(angle / (math.pi / 4))) < 1e-9:
        return [("t", qs)]
    return [("t", qs)] * rz_t_count


_IMPORT_BASIS = ["cx", "h", "s", "sdg", "t", "tdg", "x", "z", "rz", "measure"]


def from_qiskit(qc, rz_t_count: int = 4, name=None) -> Circuit:
    """Transpile to {cx,h,s,sdg,t,tdg,x,z,rz,measure} (unrolls ccx etc.), then build the DAG.
    Limitations (see docs/ASSUMPTIONS.md): rz angles via rz_to_gates (approximate); global
    phase, barriers, delays are dropped; classically-conditioned gates and `reset` are
    rejected; timing of measurement is a single `t_meas` beat."""
    from qiskit import transpile
    qc = transpile(qc, basis_gates=_IMPORT_BASIS, optimization_level=0)
    gates = []
    for inst in qc.data:
        nm = inst.operation.name
        qs = tuple(qc.find_bit(q).index for q in inst.qubits)
        if nm in ("barrier", "delay"):
            continue
        if getattr(inst.operation, "condition", None) is not None:
            raise CircuitError(f"classically-conditioned gate {nm!r} is not supported")
        if nm == "rz":
            gates += rz_to_gates(float(inst.operation.params[0]), qs, rz_t_count)
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
    mem_wait_cycles: int       # cycles - exec_busy: nothing computing (memory/deps/T)
    physical_qubits: int
    cr_slots: int
    n_banks: int
    bank_capacity: int
    msf_stall_cycles: int = 0  # cycles in which >=1 op was blocked ONLY by an empty magic buffer
    msf_stalled_ops: int = 0   # number of T ops that had to wait for a magic state
    xfer_busy_cycles: int = 0  # sum of transfer durations (all lanes)
    shift_cycles: int = 0      # Line-SAM: sum of scan-line shift beats inside transfers
    sam_model: str = ""

    def asdict(self):
        return asdict(self)


class Simulator:
    def __init__(self, circuit: Circuit, cfg: ArchConfig, policy):
        if not isinstance(circuit, Circuit):
            raise TypeError("circuit must be a Circuit")
        if not isinstance(cfg, ArchConfig):
            raise TypeError("cfg must be an ArchConfig")
        B = cfg.n_banks
        if -(-circuit.n_qubits // B) > cfg.bank_capacity:
            raise ConfigError("banks too small to hold all logical qubits "
                              f"({circuit.n_qubits} qubits, {B}x{cfg.bank_capacity} cells)")
        self.circuit, self.cfg, self.policy = circuit, cfg, policy
        n = len(circuit.ops)
        self.t = 0
        self.loc = {}                                   # q -> ("CR",slot)|("SAM",bank,cell)|("XFER",)
        self.slot_owner = [None] * cfg.cr_slots
        self.cells = [[None] * cfg.bank_capacity for _ in range(B)]
        self.port_free = [0] * B
        self.lane_free = [0] * cfg.bus_lanes
        self.scan_row = [cfg.initial_scan_row()] * B    # Line-SAM state (one scan line per bank)
        self.busy = set()                               # qubits inside a running op
        self.last_used = [0] * circuit.n_qubits
        self.started, self.finished = set(), set()
        self._head = 0
        self.n_started_uses = [0] * circuit.n_qubits
        self._remaining = [len(p) for p in circuit.preds]
        self._ready = {i for i in range(n) if self._remaining[i] == 0}
        self._unstarted_preds = [len(p) for p in circuit.preds]
        self._soon = set(self._ready)                   # unstarted ops whose preds have all STARTED
        self.running = []                               # (end, op_id)
        self.xfers = []                                 # dicts: end, kind, q, ...
        self.xfer_of = {}                               # q -> in-flight transfer dict
        self.trace = []
        T = cfg.timing
        self.ms_stock = INF if T.magic_unlimited else T.magic_buffer
        self.ms_next = T.magic_interval
        self._stall_since = {}
        self._stall_iv = []
        self.initial_loc = {}
        nxt = [0] * B                                   # round-robin initial placement
        for q in range(circuit.n_qubits):
            b = q % B
            c = nxt[b]; nxt[b] += 1
            self.cells[b][c] = q
            self.loc[q] = self.initial_loc[q] = ("SAM", b, c)

    # ------------------------------------------------------------ queries (read-only)
    def state(self, q):
        return self.loc[q][0]

    def free_slots(self):
        return sum(o is None for o in self.slot_owner)

    def pending_stores(self):
        return sum(x["kind"] == "store" for x in self.xfers)

    def pending_loads(self):
        return sum(x["kind"] == "load" for x in self.xfers)

    def head(self):
        """Lowest-index op not yet started (meaningful for fixed-order policies)."""
        while self._head in self.started:
            self._head += 1
        return self._head

    def op_state(self, i):
        """'waiting' (preds unfinished) | 'ready' (preds done, not started) | 'running' | 'done'."""
        if i in self.finished:
            return "done"
        if i in self.started:
            return "running"
        return "ready" if self._remaining[i] == 0 else "waiting"

    def ready_ops(self):
        """Dependency-ready, not-yet-started ops (any order)."""
        return sorted(self._ready)

    def soon_ready_ops(self):
        """Unstarted ops whose predecessors are all finished or running (superset of ready_ops)."""
        return sorted(self._soon)

    def future_uses(self, q):
        """Unstarted ops using q, in dependency order. Ops on one qubit are chained, so the
        started ones always form a prefix of circuit.uses[q] -- valid for ANY schedule."""
        return self.circuit.uses[q][self.n_started_uses[q]:]

    def next_use(self, q):
        """Program index of the first not-yet-started op using q (INF if none)."""
        u = self.circuit.uses[q]
        k = self.n_started_uses[q]
        return u[k] if k < len(u) else INF

    def store_cost_estimate(self, q):
        """Cheapest duration to store q now, ignoring port/lane availability (INF if no space)."""
        best = INF
        for b in range(self.cfg.n_banks):
            p = self._plan_store_in_bank(b, None)
            if p is not None:
                best = min(best, p[0])
        return best

    # ------------------------------------------------------------ internals
    def _all_issuable_but_magic(self, i):
        c, cfg = self.circuit, self.cfg
        return (len(self.running) < cfg.max_parallel_ops and
                all(self.state(q) == "CR" and q not in self.busy for q in c.ops[i].qubits))

    def _update_stalls(self):
        if self.cfg.timing.magic_unlimited:
            return
        for i in self._ready:
            if (self.circuit.ops[i].kind == "T" and self.ms_stock <= 0
                    and self._all_issuable_but_magic(i)):
                self._stall_since.setdefault(i, self.t)

    # ------------------------------------------------------------ actions
    def try_exec(self, i):
        c, cfg = self.circuit, self.cfg
        op = c.ops[i]
        if i in self.started or self._remaining[i] != 0:
            return False
        if not self._all_issuable_but_magic(i):
            return False
        if op.kind == "T" and not cfg.timing.magic_unlimited:
            self._tick()
            if self.ms_stock <= 0:
                self._stall_since.setdefault(i, self.t)
                return False
            if self.ms_stock == cfg.timing.magic_buffer:   # factory was stalled; restart it
                self.ms_next = self.t + cfg.timing.magic_interval
            self.ms_stock -= 1
            self.trace.append(dict(kind="msf", t=self.t, delta=-1, stock=self.ms_stock, op=i))
        if i in self._stall_since:
            self._stall_iv.append((self._stall_since.pop(i), self.t))
        end = self.t + cfg.timing.op_duration(op.kind)
        self.started.add(i); self._ready.discard(i); self._soon.discard(i)
        for s_ in c.succs[i]:
            self._unstarted_preds[s_] -= 1
            if self._unstarted_preds[s_] == 0:
                self._soon.add(s_)
        self.running.append((end, i)); self.busy.update(op.qubits)
        for q in op.qubits:
            self.last_used[q] = self.t
            self.n_started_uses[q] += 1
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
        elif not 0 <= slot < self.cfg.cr_slots or self.slot_owner[slot] is not None:
            return False
        extra = {}
        if self.cfg.sam_model == "line":
            before = self.scan_row[b]
            dur = self.cfg.line_cost(before, cell)
            row = self.cfg.row_of(cell)
            self.scan_row[b] = row
            extra = dict(row=row, scan_before=before, shift=abs(row - before))
        else:
            dur = self.cfg.xfer_time(cell)
        end = self.t + dur
        self.slot_owner[slot] = q
        self.port_free[b] = self.lane_free[lane] = end
        self.loc[q] = ("XFER",)
        x = dict(end=end, kind="load", q=q, bank=b, cell=cell, slot=slot)
        self.xfers.append(x); self.xfer_of[q] = x
        self.trace.append(dict(kind="load", q=q, start=self.t, end=end, bank=b, cell=cell,
                               slot=slot, lane=lane, **extra))
        return True

    def _plan_store_in_bank(self, b, cell):
        """-> (duration, cell) cheapest/default destination in bank b, or None."""
        cfg = self.cfg
        if cell is not None:
            if not 0 <= cell < cfg.bank_capacity or self.cells[b][cell] is not None:
                return None
            cand = [cell]
        else:
            cand = [c for c in range(cfg.bank_capacity) if self.cells[b][c] is None]
            if not cand:
                return None
        if cfg.sam_model == "line":
            # locality-aware store: nearest row to the scan line (ties: lower row, lower col)
            s = self.scan_row[b]
            c = min(cand, key=lambda x: (abs(cfg.row_of(x) - s), x))
            return cfg.line_cost(s, c), c
        c = cand[0] if cell is None else cell          # generic: shallowest free cell
        return cfg.xfer_time(c), c

    def try_store(self, q, bank=None, cell=None):
        """CR -> SAM. Destination cell reserved now; CR slot freed on completion.
        Line-SAM default: the bank/cell minimising transfer time (locality-aware store).
        Generic default: shallowest free cell over banks whose port is free."""
        if self.state(q) != "CR" or q in self.busy:
            return False
        lane = self._lane()
        if lane is None:
            return False
        slot = self.loc[q][1]
        cands = []
        for b in ([bank] if bank is not None else range(self.cfg.n_banks)):
            if not 0 <= b < self.cfg.n_banks or self.port_free[b] > self.t:
                continue
            p = self._plan_store_in_bank(b, cell)
            if p is None:
                continue
            dur, c = p
            cands.append(((dur, b, c) if self.cfg.sam_model == "line" else (c, b), dur, b, c))
        if not cands:
            return False
        _, dur, b, c = min(cands)
        extra = {}
        if self.cfg.sam_model == "line":
            before = self.scan_row[b]
            row = self.cfg.row_of(c)
            self.scan_row[b] = row
            extra = dict(row=row, scan_before=before, shift=abs(row - before))
        end = self.t + dur
        self.cells[b][c] = q
        self.port_free[b] = self.lane_free[lane] = end
        self.loc[q] = ("XFER",)
        x = dict(end=end, kind="store", q=q, bank=b, cell=c, slot=slot)
        self.xfers.append(x); self.xfer_of[q] = x
        self.trace.append(dict(kind="store", q=q, start=self.t, end=end, bank=b, cell=c,
                               slot=slot, lane=lane, **extra))
        return True

    # ------------------------------------------------------------ engine
    def _tick(self):
        T = self.cfg.timing
        if T.magic_unlimited:
            return
        while self.ms_next <= self.t and self.ms_stock < T.magic_buffer:
            self.ms_stock += 1
            self.trace.append(dict(kind="msf", t=self.ms_next, delta=+1, stock=self.ms_stock))
            self.ms_next += T.magic_interval

    def _complete(self):
        for e, i in [r for r in self.running if r[0] <= self.t]:
            self.running.remove((e, i)); self.finished.add(i)
            self.busy.difference_update(self.circuit.ops[i].qubits)
            for s in self.circuit.succs[i]:
                self._remaining[s] -= 1
                if self._remaining[s] == 0:
                    self._ready.add(s)
        for x in [x for x in self.xfers if x["end"] <= self.t]:
            self.xfers.remove(x); q = x["q"]; self.xfer_of.pop(q, None)
            if x["kind"] == "load":
                self.cells[x["bank"]][x["cell"]] = None
                self.loc[q] = ("CR", x["slot"]); self.last_used[q] = self.t
            else:
                self.slot_owner[x["slot"]] = None
                self.loc[q] = ("SAM", x["bank"], x["cell"])

    def run(self) -> Result:
        n = len(self.circuit.ops)
        T = self.cfg.timing
        while len(self.finished) < n:
            self._tick()
            while self.policy.step(self):
                pass
            self._update_stalls()
            cands = [e for e, _ in self.running] + [x["end"] for x in self.xfers]
            if not T.magic_unlimited and self.ms_stock < T.magic_buffer and self.ms_next > self.t:
                cands.append(self.ms_next)
            if not cands:
                raise DeadlockError(f"no progress at t={self.t}, policy={self.policy.name}")
            self.t = min(cands)
            self._complete()
        makespan = max((e["end"] for e in self.trace if e["kind"] == "op"), default=0)
        # drain in-flight write-backs so the final resource state is consistent
        while self.xfers:
            self.t = min(x["end"] for x in self.xfers)
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
        stall, cs, ce = 0, None, None
        for s, e in sorted(self._stall_iv):               # union of MSF-stall intervals
            if ce is None or s > ce:
                if ce is not None:
                    stall += ce - cs
                cs, ce = s, e
            else:
                ce = max(ce, e)
        stall += (ce - cs) if ce is not None else 0
        xf = [e for e in self.trace if e["kind"] in ("load", "store")]
        L = sum(e["kind"] == "load" for e in xf)
        S = len(xf) - L
        return Result(self.circuit.name, self.policy.name, makespan, L, S, L + S, busy_cycles,
                      makespan - busy_cycles, self.cfg.physical_qubits(), self.cfg.cr_slots,
                      self.cfg.n_banks, self.cfg.bank_capacity,
                      msf_stall_cycles=stall, msf_stalled_ops=len(self._stall_iv),
                      xfer_busy_cycles=sum(e["end"] - e["start"] for e in xf),
                      shift_cycles=sum(e.get("shift", 0) for e in xf) * T.line_shift,
                      sam_model=self.cfg.sam_model)


def trace_hash(trace) -> str:
    """Canonical hash of a schedule, used to verify run-to-run determinism."""
    return hashlib.sha256(json.dumps(trace, sort_keys=True, default=str).encode()).hexdigest()[:16]


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
    """Independent trace checker (does not use Simulator state). Returns a list of error strings."""
    errs = []
    kinds = {"op", "load", "store", "msf"}
    for e in trace:
        if e.get("kind") not in kinds:
            errs.append(f"unknown trace event kind {e.get('kind')!r}")
    if errs:
        return errs
    T = cfg.timing
    ops = [e for e in trace if e["kind"] == "op"]
    xf = [e for e in trace if e["kind"] in ("load", "store")]
    msf = [e for e in trace if e["kind"] == "msf"]

    # ---- operations match the circuit, run exactly once, respect deps and durations
    seen = {}
    for e in ops:
        if e["id"] in seen:
            errs.append(f"op {e['id']} executed twice")
        seen[e["id"]] = e
    for i in seen:
        if not 0 <= i < len(circuit.ops):
            errs.append(f"trace executes unknown op {i}")
    missing = [i for i in range(len(circuit.ops)) if i not in seen]
    if missing:
        errs.append(f"{len(missing)} ops never executed (first: {missing[:5]})")
    for i, e in seen.items():
        if not 0 <= i < len(circuit.ops):
            continue
        op = circuit.ops[i]
        if tuple(e["qubits"]) != tuple(op.qubits):
            errs.append(f"op {i}: trace qubits {e['qubits']} differ from circuit {op.qubits}")
        for p in circuit.preds[i]:
            if p in seen and seen[p]["end"] > e["start"]:
                errs.append(f"dependency violation: op {i} starts {e['start']} before pred {p} ends {seen[p]['end']}")
        if e["end"] - e["start"] != T.op_duration(op.kind):
            errs.append(f"op {i} has wrong duration")

    # ---- resource ids / ranges, transfer durations
    R = cfg.rows()
    for e in xf:
        q = e["q"]
        if not 0 <= q < circuit.n_qubits:
            errs.append(f"transfer of unknown qubit {q}"); continue
        if not 0 <= e["bank"] < cfg.n_banks: errs.append(f"{e['kind']} q{q}: bank {e['bank']} out of range")
        if not 0 <= e["cell"] < cfg.bank_capacity: errs.append(f"{e['kind']} q{q}: cell {e['cell']} out of range")
        if not 0 <= e["slot"] < cfg.cr_slots: errs.append(f"{e['kind']} q{q}: CR slot {e['slot']} out of range")
        if not 0 <= e["lane"] < cfg.bus_lanes: errs.append(f"{e['kind']} q{q}: bus lane {e['lane']} out of range")
        if e["end"] <= e["start"]: errs.append(f"{e['kind']} q{q}: non-positive duration")
    if cfg.sam_model == "line":
        scan = {b: cfg.initial_scan_row() for b in range(cfg.n_banks)}
        for e in sorted(xf, key=lambda e: (e["start"], e["end"])):
            b = e["bank"]
            if not 0 <= b < cfg.n_banks or not 0 <= e["cell"] < cfg.bank_capacity:
                continue
            exp = cfg.line_cost(scan[b], e["cell"])
            if e["end"] - e["start"] != exp:
                errs.append(f"{e['kind']} q{e['q']} bank {b}: duration {e['end']-e['start']} != Line-SAM cost {exp} "
                            f"(scan row {scan[b]} -> row {cfg.row_of(e['cell'])})")
            scan[b] = cfg.row_of(e["cell"])
    else:
        for e in xf:
            if 0 <= e["cell"] < cfg.bank_capacity and e["end"] - e["start"] != cfg.xfer_time(e["cell"]):
                errs.append(f"{e['kind']} q{e['q']}: duration != generic transfer time")

    # ---- per-qubit state machine: CR residency, SAM source/destination consistency
    per_q = {q: [] for q in range(circuit.n_qubits)}
    for e in ops + xf:
        for q in (e["qubits"] if e["kind"] == "op" else (e["q"],)):
            if q in per_q:
                per_q[q].append(e)
    seen_cells = {}
    for q, loc in initial_loc.items():
        _, b0, c0 = loc
        if (b0, c0) in seen_cells:
            errs.append(f"initial placement: q{q} and q{seen_cells[(b0, c0)]} share cell {(b0, c0)}")
        seen_cells[(b0, c0)] = q
        if not (0 <= b0 < cfg.n_banks and 0 <= c0 < cfg.bank_capacity):
            errs.append(f"initial placement of q{q} out of range")
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
                if state != "CR":
                    errs.append(f"q{q}: op {e['id']} at t={e['start']} but qubit not resident in CR")
        if open_cell is not None:
            cell_iv.setdefault(open_cell[:2], []).append((open_cell[2], INF, q))
        if open_slot is not None:
            slot_iv.setdefault(open_slot[0], []).append((open_slot[1], INF, q))
    for s, iv in slot_iv.items():
        _no_overlap(iv, f"CR slot {s}", errs)
    for k, iv in cell_iv.items():
        _no_overlap(iv, f"SAM cell {k}", errs)

    # ---- bank ports, bus lanes (ids and counts), parallel-op limit
    for b in range(cfg.n_banks):
        _no_overlap([(e["start"], e["end"], e["q"]) for e in xf if e["bank"] == b], f"bank {b} port", errs)
    for ln in range(cfg.bus_lanes):
        _no_overlap([(e["start"], e["end"], e["q"]) for e in xf if e["lane"] == ln], f"bus lane {ln}", errs)
    if _max_concurrency([(e["start"], e["end"]) for e in xf]) > cfg.bus_lanes:
        errs.append("bus-lane limit exceeded")
    if _max_concurrency([(e["start"], e["end"]) for e in ops]) > cfg.max_parallel_ops:
        errs.append("parallel-op limit exceeded")

    # ---- magic-state inventory
    t_ops = sorted(e["start"] for e in ops if 0 <= e["id"] < len(circuit.ops) and circuit.ops[e["id"]].kind == "T")
    if T.magic_unlimited:
        if msf:
            errs.append("magic_unlimited run must not record factory events")
    else:
        stock = T.magic_buffer
        cons = []
        for e in msf:
            stock += e["delta"]
            if stock != e["stock"]: errs.append(f"msf event at t={e['t']}: stock bookkeeping mismatch")
            if stock < 0: errs.append(f"magic-state inventory negative at t={e['t']}")
            if stock > T.magic_buffer: errs.append(f"magic-state buffer overflow at t={e['t']}")
            if e["delta"] < 0: cons.append(e["t"])
        if sorted(cons) != t_ops:
            errs.append("magic-state consumptions do not match T-gate start times")
        for k, t in enumerate(t_ops):                       # independent upper bound on supply
            if k + 1 > T.magic_buffer + t // T.magic_interval:
                errs.append(f"T op #{k} at t={t}: more states consumed than the factory can have produced")
                break
    return errs
