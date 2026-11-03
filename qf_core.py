"""QubitFlow discrete-event model for LSQCA-inspired scheduling experiments.

Sections:
  1. CONFIG    Architecture and logical timing parameters.
  2. CIRCUIT   Dependency DAG, synthetic workloads, and strict QASM import.
  3. SIM       Event-driven residency, transfer, operation, and MSF model.
  4. VALIDATE  Independent trace/resource checker.

Time is measured in abstract code beats. H/S/Pauli/parity-measurement costs and
the MSF 15-beat production interval follow LSQCA's reported assumptions.
CNOT/T macro costs, bank geometry, CR size, parallelism, and scan-line initial
position remain explicit model parameters. Line-SAM uses row-distance timing;
this is not a physical surface-code or Point-SAM simulator. Space metrics are
logical-cell accounting only, not physical-qubit estimates.

A qubit is in exactly one of ("CR", slot), ("SAM", bank, cell), or ("XFER",).
Transfers reserve their destination and bank/lane resources at start; source
resources are released at completion. Operations require completed dependencies,
resident idle operands, a free operation slot, and a buffered magic state for T.
"""
from __future__ import annotations
import ast, bisect, math, random, re
from dataclasses import dataclass, field, asdict

INF = float("inf")


# ============================================================================
# 1. CONFIG
# ============================================================================



@dataclass(frozen=True)
class Timing:
    # Logical primitive timings from LSQCA Table I.
    t_h: int = 3
    t_s: int = 2
    t_pauli: int = 0
    t_measure: int = 0
    t_parity_measure: int = 1
    t_t: int = 3  # Assumed MZZ (1) + conditional S (2); PM/SK extra latency omitted.
    # The optimized CNOT in LSQCA has variable latency. This is an explicit
    # experiment parameter, not a value asserted by the paper.
    t_2q: int = 1
    # Legacy depth-only model; not used by default Line-SAM.
    xfer_base: int = 2
    xfer_per_depth: int = 1
    magic_interval: int = 15
    magic_factories: int = 1
    magic_buffer: int | None = None

    def __post_init__(self):
        for key in ("t_h", "t_s", "t_pauli", "t_measure", "t_parity_measure",
                    "t_t", "t_2q", "xfer_base", "xfer_per_depth",
                    "magic_interval", "magic_factories"):
            value = getattr(self, key)
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"{key} must be an integer")
            if key not in ("magic_interval", "magic_factories") and value < 0:
                raise ValueError(f"{key} must be non-negative")
        if self.magic_interval <= 0 or self.magic_factories <= 0:
            raise ValueError("magic_interval and magic_factories must be positive")
        if self.magic_buffer is None:
            object.__setattr__(self, "magic_buffer", 2 * self.magic_factories)
        if not isinstance(self.magic_buffer, int) or isinstance(self.magic_buffer, bool):
            raise ValueError("magic_buffer must be an integer")
        if self.magic_buffer <= 0:
            raise ValueError("magic_buffer must be positive")

    def op_duration(self, kind: str, name: str | None = None) -> int:
        name = (name or "").lower()
        if name == "h":
            return self.t_h
        if name in {"s", "sdg"}:
            return self.t_s
        if name in {"x", "y", "z", "id"}:
            return self.t_pauli
        if name in {"measure", "mx", "mz"}:
            return self.t_measure
        if name in {"mxx", "mzz"}:
            return self.t_parity_measure
        if kind == "T" or name in {"t", "tdg"}:
            return self.t_t
        if kind == "2q":
            return self.t_2q
        raise ValueError(f"no timing defined for operation {name!r} ({kind})")


@dataclass(frozen=True)
class ArchConfig:
    d: int = 11
    cr_slots: int = 2
    n_banks: int = 2
    bank_capacity: int = 64
    bank_width: int = 8
    # None means one transfer lane per bank. A smaller explicit value models
    # a shared interconnect sensitivity, not the default multi-bank behavior.
    bus_lanes: int | None = None
    max_parallel_ops: int = 4
    timing: Timing = field(default_factory=Timing)
    sam_model: str = "line"
    # Explicit local access/port overhead; zero is the movement-only lower bound.
    line_access_cost: int = 0
    line_move_per_row: int = 1
    line_initial_row: int | None = None

    def __post_init__(self):
        for key in ("d", "cr_slots", "n_banks", "bank_capacity",
                    "bank_width", "max_parallel_ops"):
            value = getattr(self, key)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{key} must be a positive integer")
        if self.cr_slots < 2:
            raise ValueError("cr_slots must be >= 2")
        if self.bus_lanes is None:
            object.__setattr__(self, "bus_lanes", self.n_banks)
        if not isinstance(self.bus_lanes, int) or isinstance(self.bus_lanes, bool) or self.bus_lanes <= 0:
            raise ValueError("bus_lanes must be a positive integer")
        if self.sam_model not in ("line", "depth"):
            raise ValueError("sam_model must be 'line' or 'depth'")
        if not isinstance(self.line_access_cost, int) or isinstance(self.line_access_cost, bool) or self.line_access_cost < 0:
            raise ValueError("line_access_cost must be a non-negative integer")
        if not isinstance(self.line_move_per_row, int) or isinstance(self.line_move_per_row, bool) or self.line_move_per_row < 0:
            raise ValueError("line_move_per_row must be a non-negative integer")
        rows = math.ceil(self.bank_capacity / self.bank_width)
        if self.line_initial_row is None:
            object.__setattr__(self, "line_initial_row", 0)
        if not isinstance(self.line_initial_row, int) or isinstance(self.line_initial_row, bool):
            raise ValueError("line_initial_row must be an integer")
        if not 0 <= self.line_initial_row < rows:
            raise ValueError("line_initial_row is outside the SAM bank")

    def depth(self, cell: int) -> int:
        if not isinstance(cell, int) or not 0 <= cell < self.bank_capacity:
            raise ValueError(f"SAM cell out of range: {cell}")
        return cell // self.bank_width

    def xfer_time(self, cell: int, scan_row: int | None = None) -> int:
        """Line-SAM latency is vertical row distance H.

        This models scan-line travel only. It assumes one scan line per bank,
        and abstracts away physical patch transport within the LSQCA port.
        """
        row = self.depth(cell)
        if self.sam_model == "depth":
            return self.timing.xfer_base + self.timing.xfer_per_depth * row
        if scan_row is None:
            scan_row = self.line_initial_row
        return self.line_access_cost + self.line_move_per_row * abs(row - scan_row)

    def logical_cell_count(self) -> int:
        """SAM data cells + scan-line auxiliary cells + CR cells; excludes MSFs.

        The CR model uses four auxiliary/port cells plus its register cells,
        matching the compact six-cell CR shown for two registers in LSQCA.
        """
        return (self.n_banks * self.bank_capacity
                + self.n_banks * self.bank_width
                + self.cr_slots + 4)


_KIND = {
    "cx": "2q", "cz": "2q", "cy": "2q", "cp": "2q", "swap": "2q",
    "mxx": "2q", "mzz": "2q",
    "t": "T", "tdg": "T",
    "measure": "meas", "mx": "meas", "mz": "meas",
}
_SUPPORTED_GATES = set(_KIND) | {"h", "s", "sdg", "x", "y", "z", "id"}


@dataclass(frozen=True)
class Op:
    id: int
    kind: str
    qubits: tuple
    name: str


class Circuit:
    def __init__(self, n_qubits: int, ops: list, name: str = "circuit"):
        if not isinstance(n_qubits, int) or isinstance(n_qubits, bool) or n_qubits < 0:
            raise ValueError("n_qubits must be a non-negative integer")
        self.n_qubits, self.ops, self.name = n_qubits, list(ops), name
        last, self.preds = {}, []
        self.uses = {q: [] for q in range(n_qubits)}
        for i, op in enumerate(self.ops):
            if not isinstance(op, Op):
                raise TypeError(f"ops[{i}] must be an Op")
            if not isinstance(op.id, int) or isinstance(op.id, bool) or op.id != i:
                raise ValueError("operation IDs must be contiguous and match list positions")
            if op.kind not in {"1q", "2q", "T", "meas"} or op.name not in _SUPPORTED_GATES:
                raise ValueError(f"unsupported operation at op {i}: {op.name!r} ({op.kind})")
            expected_kind = _KIND.get(op.name, "1q")
            if op.kind != expected_kind:
                raise ValueError(
                    f"operation kind mismatch at op {i}: gate {op.name!r} "
                    f"must have kind {expected_kind!r}, not {op.kind!r}"
                )
            arity = 2 if op.kind == "2q" else 1
            if len(op.qubits) != arity or len(set(op.qubits)) != arity:
                raise ValueError(f"op {i} must have {arity} distinct operand(s)")
            if any(not isinstance(q, int) or isinstance(q, bool) or q < 0 or q >= n_qubits for q in op.qubits):
                raise ValueError(f"op {i} contains an out-of-range qubit index")
            self.preds.append(sorted({last[q] for q in op.qubits if q in last}))
            for q in op.qubits:
                last[q] = op.id
                self.uses[q].append(op.id)

    @classmethod
    def from_gates(cls, n_qubits, gates, name="circuit"):
        ops = []
        for i, (gate, qubits) in enumerate(gates):
            if not isinstance(gate, str):
                raise TypeError("gate names must be strings")
            gate, qubits = gate.lower(), tuple(qubits)
            if len(qubits) not in (1, 2):
                raise ValueError(f"gate {gate!r} has unsupported arity {len(qubits)}")
            if gate not in _SUPPORTED_GATES:
                raise ValueError(f"unsupported gate {gate!r}; transpile/decompose it explicitly")
            kind = _KIND.get(gate, "1q")
            expected_arity = 2 if kind == "2q" else 1
            if len(qubits) != expected_arity:
                raise ValueError(f"gate {gate!r} needs {expected_arity} operand(s)")
            ops.append(Op(i, kind, qubits, gate))
        return cls(n_qubits, ops, name)

    def stats(self):
        counts = {}
        for op in self.ops:
            counts[op.kind] = counts.get(op.kind, 0) + 1
        return {"qubits": self.n_qubits, "ops": len(self.ops), **counts}


# ---------------------------------------------------------------- synthetic benchmarks
def _toffoli(a, b, t):  # standard 7-T decomposition (controls a,b; target t)
    return [("h", (t,)), ("cx", (b, t)), ("tdg", (t,)), ("cx", (a, t)), ("t", (t,)),
            ("cx", (b, t)), ("tdg", (t,)), ("cx", (a, t)), ("t", (b,)), ("t", (t,)),
            ("h", (t,)), ("cx", (a, b)), ("t", (a,)), ("tdg", (b,)), ("cx", (a, b))]


def adder(n_bits: int) -> Circuit:
    """Cuccaro-style ripple-carry adder (2n+2 qubits), Clifford+T."""
    if not isinstance(n_bits, int) or n_bits < 1:
        raise ValueError("n_bits must be a positive integer")
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



def qft(n_qubits: int) -> Circuit:
    """Return a QFT interaction-topology workload.

    Controlled-phase angles and final bit-reversal swaps are represented as
    generic two-qubit operations: this is for scheduling/topology studies,
    not a gate-equivalent decomposition or statevector implementation.
    """
    if not isinstance(n_qubits, int) or n_qubits < 1:
        raise ValueError("n_qubits must be a positive integer")
    gates = []
    for target in range(n_qubits):
        gates.append(("h", (target,)))
        for control in range(target + 1, n_qubits):
            gates.append(("cp", (control, target)))
    for left in range(n_qubits // 2):
        right = n_qubits - 1 - left
        gates.append(("swap", (left, right)))
    return Circuit.from_gates(n_qubits, gates, f"qft{n_qubits}")


def random_circuit(n_qubits: int, n_ops: int, p_t=0.3, p_2q=0.4, seed=0) -> Circuit:
    if n_qubits < 1 or n_ops < 0:
        raise ValueError("n_qubits must be positive and n_ops non-negative")
    if not 0 <= p_t <= 1 or not 0 <= p_2q <= 1 or p_t + p_2q > 1:
        raise ValueError("p_t and p_2q must be in [0, 1] and sum to at most 1")
    if n_qubits < 2 and p_2q > 0:
        raise ValueError("two-qubit gate probability requires at least two qubits")
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
def from_qiskit(qc, rz_t_count: int | None = None, name=None) -> Circuit:
    """Transpile to a supported Clifford+T gate vocabulary.

    RZ rotations exactly on the pi/4 grid map to Clifford+T up to global
    phase. Off-grid rotations are rejected unless rz_t_count is supplied;
    that is a gate-cost approximation, not semantics-preserving synthesis.
    """
    from qiskit import transpile
    if rz_t_count is not None and rz_t_count < 0:
        raise ValueError("rz_t_count must be non-negative")
    if any(getattr(inst.operation, "condition", None) is not None for inst in qc.data):
        raise ValueError("classically conditioned Qiskit operations are unsupported")
    basis = ["cx", "h", "s", "sdg", "t", "tdg", "x", "y", "z", "rz", "measure"]
    qc = transpile(qc, basis_gates=basis, optimization_level=0)
    gates = []
    measured_qubits = set()
    for inst in qc.data:
        gate = inst.operation.name.lower()
        qubits = tuple(qc.find_bit(q).index for q in inst.qubits)
        if gate in ("barrier", "delay"):
            continue
        if gate == "measure":
            measured_qubits.update(qubits)
        elif any(q in measured_qubits for q in qubits):
            raise ValueError("Qiskit operations after measurement are unsupported")
        if gate == "rz":
            angle = float(inst.operation.params[0])
            units = angle / (math.pi / 4)
            nearest = round(units)
            if abs(units - nearest) < 1e-9:
                k = nearest % 8
                exact = {
                    0: [], 1: [("t", qubits)], 2: [("s", qubits)],
                    3: [("s", qubits), ("t", qubits)], 4: [("z", qubits)],
                    5: [("sdg", qubits), ("tdg", qubits)],
                    6: [("sdg", qubits)], 7: [("tdg", qubits)],
                }
                gates.extend(exact[k])
            elif rz_t_count is not None:
                gates.extend([("t", qubits)] * rz_t_count)
            else:
                raise ValueError(
                    f"RZ angle {angle} is off the pi/4 grid; pass rz_t_count explicitly "
                    "only for a coarse cost approximation"
                )
        else:
            gates.append((gate, qubits))
    return Circuit.from_gates(qc.num_qubits, gates, name or getattr(qc, "name", "qiskit"))


def _angle_value(expression: str) -> float:
    """Evaluate the arithmetic subset used for OpenQASM rotation angles."""
    tree = ast.parse(expression.replace("pi", "PI"), mode="eval")
    def visit(node):
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.Name) and node.id == "PI":
            return math.pi
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = visit(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp):
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Add): return left + right
            if isinstance(node.op, ast.Sub): return left - right
            if isinstance(node.op, ast.Mult): return left * right
            if isinstance(node.op, ast.Div): return left / right
        raise ValueError(f"unsupported QASM angle expression: {expression!r}")
    return visit(tree)


def _append_rz(gates, qubit, angle, source):
    units = angle / (math.pi / 4)
    nearest = round(units)
    if abs(units - nearest) > 1e-9:
        raise ValueError(
            f"{source}: RZ angle {angle} is not Clifford+T exact; use a documented "
            "approximate synthesis before importing"
        )
    mapping = {
        0: [], 1: [("t", (qubit,))], 2: [("s", (qubit,))],
        3: [("s", (qubit,)), ("t", (qubit,))],
        4: [("z", (qubit,))],
        5: [("sdg", (qubit,)), ("tdg", (qubit,))],
        6: [("sdg", (qubit,))], 7: [("tdg", (qubit,))],
    }
    gates.extend(mapping[nearest % 8])


def from_qasm(path: str, **kw) -> Circuit:
    """Import the supported OpenQASM 2 Clifford+T subset without optional deps.

    Supports qreg/creg declarations, barriers, terminal measurements, CX,
    Clifford/Pauli gates, SX/SXdg, T/Tdg, and exact pi/4-grid RZ rotations.
    Classical control, reset, arbitrary rotations, and custom gate definitions
    are intentionally rejected rather than silently approximated.
    """
    if kw:
        raise TypeError(f"unsupported from_qasm options: {', '.join(kw)}")
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    source = re.sub(r"//[^\n]*", "", source)
    qregs, cregs, gates = {}, {}, []
    measured_qubits = set()
    statements = [x.strip() for x in source.split(";") if x.strip()]
    qtotal = 0
    for statement in statements:
        if statement.startswith("OPENQASM ") or statement.startswith("include "):
            continue
        decl = re.fullmatch(r"qreg\s+(\w+)\s*\[\s*(\d+)\s*\]", statement)
        if decl:
            name, size = decl.group(1), int(decl.group(2))
            if name in qregs or size <= 0:
                raise ValueError(f"invalid/duplicate QASM quantum register: {statement}")
            qregs[name] = (qtotal, size)
            qtotal += size
            continue
        decl = re.fullmatch(r"creg\s+(\w+)\s*\[\s*(\d+)\s*\]", statement)
        if decl:
            cregs[decl.group(1)] = int(decl.group(2))
            continue
        if statement.startswith("barrier"):
            continue
        if statement.startswith("measure "):
            m = re.fullmatch(r"measure\s+(\w+)\[(\d+)\]\s*->\s*(\w+)\[(\d+)\]", statement)
            if not m:
                raise ValueError(f"unsupported measurement statement: {statement}")
            reg, idx, creg, cidx = m.groups()
            if reg not in qregs or creg not in cregs:
                raise ValueError(f"unknown register in measurement: {statement}")
            base, size = qregs[reg]
            if not 0 <= int(idx) < size or not 0 <= int(cidx) < cregs[creg]:
                raise ValueError(f"measurement index out of range: {statement}")
            qubit = base + int(idx)
            if qubit in measured_qubits:
                raise ValueError(f"duplicate measurement of qubit: {statement}")
            gates.append(("measure", (qubit,)))
            measured_qubits.add(qubit)
            continue
        m = re.fullmatch(r"([a-zA-Z][a-zA-Z0-9_]*)\s*(?:\(([^()]*)\))?\s+(.+)", statement)
        if not m:
            raise ValueError(f"unsupported OpenQASM statement: {statement}")
        gate, angle, operand_text = m.groups()
        gate = gate.lower()
        operands = []
        for operand in operand_text.split(","):
            q = re.fullmatch(r"\s*(\w+)\[(\d+)\]\s*", operand)
            if not q:
                raise ValueError(f"unsupported QASM operand: {operand}")
            reg, idx = q.groups()
            if reg not in qregs:
                raise ValueError(f"unknown quantum register {reg!r}")
            base, size = qregs[reg]
            if not 0 <= int(idx) < size:
                raise ValueError(f"qubit index out of range: {operand}")
            operands.append(base + int(idx))
        if any(q in measured_qubits for q in operands):
            raise ValueError(f"operation after measurement is unsupported: {statement}")
        if gate == "rz":
            if angle is None or len(operands) != 1:
                raise ValueError(f"malformed RZ statement: {statement}")
            _append_rz(gates, operands[0], _angle_value(angle), statement)
        elif gate in {"sx", "sxdg"}:
            if angle is not None or len(operands) != 1:
                raise ValueError(f"malformed {gate} statement: {statement}")
            phase = "s" if gate == "sx" else "sdg"
            gates.extend([("h", operands), (phase, operands), ("h", operands)])
        elif gate == "ccx":
            if angle is not None or len(operands) != 3:
                raise ValueError(f"malformed CCX statement: {statement}")
            gates.extend(_toffoli(*operands))
        else:
            if angle is not None:
                raise ValueError(f"unexpected parameters for {gate}: {statement}")
            gates.append((gate, tuple(operands)))
    if qtotal <= 0:
        raise ValueError("QASM file contains no quantum registers")
    return Circuit.from_gates(qtotal, gates, name=path.rsplit("/", 1)[-1].removesuffix(".qasm"))


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
    exec_busy_cycles: int
    non_exec_cycles: int
    logical_cells: int
    memory_density: float
    cr_slots: int
    n_banks: int
    bank_capacity: int
    msf_stall_cycles: int
    total_transfer_cycles: int

    def asdict(self):
        return asdict(self)




class Simulator:
    def __init__(self, circuit: Circuit, cfg: ArchConfig, policy,
                 initially_resident: bool = False):
        B = cfg.n_banks
        if cfg.cr_slots < 2:
            raise ValueError("cr_slots must be >= 2 (two-qubit ops need both operands resident)")
        if not initially_resident and -(-circuit.n_qubits // B) > cfg.bank_capacity:
            raise ValueError("banks too small to hold all logical qubits")
        self.circuit, self.cfg, self.policy = circuit, cfg, policy
        self.t = 0
        self.loc = {}                                   # q -> ("CR",slot)|("SAM",bank,cell)|("XFER",)
        self.slot_owner = [None] * cfg.cr_slots
        self.cells = [[None] * cfg.bank_capacity for _ in range(B)]
        self.port_free = [0] * B
        self.lane_free = [0] * cfg.bus_lanes
        self.scan_row = [cfg.line_initial_row] * B
        self.busy = set()                               # qubits inside a running op
        self.last_used = [0] * circuit.n_qubits
        self.started, self.finished = set(), set()
        # Incremental DAG-ready frontier avoids scanning the full circuit at
        # every scheduling decision on large workloads.
        self.successors = [[] for _ in circuit.ops]
        self.pending_predecessors = [len(parents) for parents in circuit.preds]
        for node, parents in enumerate(circuit.preds):
            for parent in parents:
                self.successors[parent].append(node)
        self._ready_ops = {
            node for node, pending in enumerate(self.pending_predecessors)
            if pending == 0
        }
        self._next_use_cursor = [0] * circuit.n_qubits
        self._head = 0
        self.running = []                               # (end, op_id)
        self.xfers = []                                 # dicts: end, kind, q, ...
        self.trace = []
        self.ms_stock = cfg.timing.magic_buffer
        self.ms_next = cfg.timing.magic_interval
        self.initial_loc = {}
        if initially_resident:
            if circuit.n_qubits > cfg.cr_slots:
                raise ValueError("initially_resident requires cr_slots >= circuit.n_qubits")
            for q in range(circuit.n_qubits):
                self.slot_owner[q] = q
                self.loc[q] = self.initial_loc[q] = ("CR", q)
        else:
            nxt = [0] * B
            for q in range(circuit.n_qubits):
                b = q % B
                c = nxt[b]
                nxt[b] += 1
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
        """Lowest-ID operation not yet started (fixed-order baseline helper)."""
        while self._head < len(self.circuit.ops) and self._head in self.started:
            self._head += 1
        return self._head

    def next_use(self, q):
        """Next unstarted use in this qubit's dependency-preserved order."""
        uses = self.circuit.uses[q]
        pos = self._next_use_cursor[q]
        while pos < len(uses) and uses[pos] in self.started:
            pos += 1
        self._next_use_cursor[q] = pos
        return uses[pos] if pos < len(uses) else INF

    def ready_ops(self):
        """Current dependency-ready frontier, maintained incrementally."""
        return list(self._ready_ops)

    # ------------------------------------------------------------ actions
    def try_exec(self, i):
        c, cfg = self.circuit, self.cfg
        if not isinstance(i, int) or not 0 <= i < len(c.ops):
            return False
        op = c.ops[i]
        if i not in self._ready_ops:
            return False
        if len(self.running) >= cfg.max_parallel_ops:
            return False
        if any(self.state(q) != "CR" or q in self.busy for q in op.qubits):
            return False
        if op.kind == "T":
            self._tick()
            if self.ms_stock <= 0:
                return False
            self.ms_stock -= 1
            self.trace.append(dict(kind="magic_consume", time=self.t, op_id=i))
        end = self.t + cfg.timing.op_duration(op.kind, op.name)
        self.started.add(i)
        self._ready_ops.discard(i)
        self.running.append((end, i))
        self.busy.update(op.qubits)
        for q in op.qubits:
            self.last_used[q] = self.t
            self.next_use(q)
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
        else:
            if not isinstance(slot, int) or not 0 <= slot < self.cfg.cr_slots:
                return False
            if self.slot_owner[slot] is not None:
                return False
        end = self.t + self.cfg.xfer_time(cell, self.scan_row[b])
        self.slot_owner[slot] = q
        self.port_free[b] = self.lane_free[lane] = end
        self.loc[q] = ("XFER",)
        self.xfers.append(dict(end=end, kind="load", q=q, bank=b, cell=cell, slot=slot))
        self.trace.append(dict(kind="load", q=q, start=self.t, end=end, bank=b, cell=cell,
                               slot=slot, lane=lane, scan_row_before=self.scan_row[b],
                               target_row=self.cfg.depth(cell)))
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
        if bank is not None and not 0 <= bank < self.cfg.n_banks:
            return False
        if cell is not None and not 0 <= cell < self.cfg.bank_capacity:
            return False
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
        end = self.t + self.cfg.xfer_time(cell, self.scan_row[b])
        self.cells[b][cell] = q
        self.port_free[b] = self.lane_free[lane] = end
        self.loc[q] = ("XFER",)
        self.xfers.append(dict(end=end, kind="store", q=q, bank=b, cell=cell, slot=slot))
        self.trace.append(dict(kind="store", q=q, start=self.t, end=end, bank=b, cell=cell,
                               slot=slot, lane=lane, scan_row_before=self.scan_row[b],
                               target_row=self.cfg.depth(cell)))
        return True

    # ------------------------------------------------------------ engine
    def _tick(self):
        timing = self.cfg.timing
        while self.ms_next <= self.t:
            available = timing.magic_buffer - self.ms_stock
            produced = min(timing.magic_factories, max(0, available))
            if produced:
                self.ms_stock += produced
                self.trace.append(dict(kind="magic_produce", time=self.ms_next,
                                       count=produced))
            # Factory periods continue even when the buffer is full; excess output
            # is discarded instead of becoming catch-up production.
            self.ms_next += timing.magic_interval


    def _complete(self):
        for e, i in [r for r in self.running if r[0] <= self.t]:
            self.running.remove((e, i))
            self.finished.add(i)
            self.busy.difference_update(self.circuit.ops[i].qubits)
            for successor in self.successors[i]:
                self.pending_predecessors[successor] -= 1
                if self.pending_predecessors[successor] == 0:
                    self._ready_ops.add(successor)
        for x in [x for x in self.xfers if x["end"] <= self.t]:
            self.xfers.remove(x); q = x["q"]
            self.scan_row[x["bank"]] = self.cfg.depth(x["cell"])
            if x["kind"] == "load":
                self.cells[x["bank"]][x["cell"]] = None
                self.loc[q] = ("CR", x["slot"]); self.last_used[q] = self.t
            else:
                self.slot_owner[x["slot"]] = None
                self.loc[q] = ("SAM", x["bank"], x["cell"])

    def run(self) -> Result:
        n = len(self.circuit.ops)
        msf_stall_cycles = 0
        event_iterations = 0
        max_event_iterations = max(1000, 100 * (n + self.circuit.n_qubits + 1))
        while len(self.finished) < n:
            event_iterations += 1
            if event_iterations > max_event_iterations:
                raise DeadlockError(
                    f"event-loop iteration limit exceeded at t={self.t}; "
                    f"finished={len(self.finished)}/{n}, policy={self.policy.name}"
                )
            self._tick()
            policy_iterations = 0
            seen_policy_states = set()
            while self.policy.step(self):
                policy_iterations += 1
                # Complete zero-beat gates/transfers before asking the policy again.
                self._complete()
                # Detect genuine same-timestamp residency/transfer cycles rather
                # than relying on an arbitrary low action-count threshold.
                signature = (
                    tuple(sorted(self.loc.items())),
                    tuple(self.slot_owner),
                    tuple(tuple(bank) for bank in self.cells),
                    tuple(sorted(self.running)),
                    tuple(sorted((x["kind"], x["q"], x["bank"], x["cell"],
                                  x["slot"], x["end"]) for x in self.xfers)),
                    frozenset(self.started), frozenset(self.finished),
                    tuple(self.port_free), tuple(self.lane_free), tuple(self.scan_row),
                    tuple(self.last_used), self.ms_stock, self.ms_next,
                )
                if signature in seen_policy_states:
                    raise DeadlockError(
                        f"policy entered a same-timestamp scheduling cycle at t={self.t}; "
                        f"policy={self.policy.name}"
                    )
                seen_policy_states.add(signature)
                if policy_iterations > max(10000, 32 * (n + self.circuit.n_qubits + 1)):
                    raise DeadlockError(
                        f"policy exceeded the action safety limit at t={self.t}; "
                        f"policy={self.policy.name}"
                    )
            if len(self.finished) == n:
                break
            candidates = [end for end, _ in self.running] + [x["end"] for x in self.xfers]
            if self.ms_stock < self.cfg.timing.magic_buffer:
                candidates.append(self.ms_next)
            if not candidates:
                raise DeadlockError(f"no progress at t={self.t}, policy={self.policy.name}")
            next_time = min(candidates)
            t_ready = (
                len(self.running) < self.cfg.max_parallel_ops
                and any(
                    self.circuit.ops[i].kind == "T"
                    and all(self.state(q) == "CR" and q not in self.busy
                            for q in self.circuit.ops[i].qubits)
                    for i in self.ready_ops()
                )
            )
            if self.ms_stock <= 0 and t_ready:
                msf_stall_cycles += next_time - self.t
            self.t = next_time
            self._complete()

        intervals = sorted((e["start"], e["end"]) for e in self.trace if e["kind"] == "op")
        busy, start, end = 0, None, None
        for a, b in intervals:
            if end is None or a > end:
                if end is not None:
                    busy += end - start
                start, end = a, b
            else:
                end = max(end, b)
        if end is not None:
            busy += end - start
        loads = sum(e["kind"] == "load" for e in self.trace)
        stores = sum(e["kind"] == "store" for e in self.trace)
        transfer_cycles = sum(e["end"] - e["start"] for e in self.trace
                              if e["kind"] in ("load", "store"))
        logical_cells = self.cfg.logical_cell_count()
        sam_data_cells = self.cfg.n_banks * self.cfg.bank_capacity
        memory_density = sam_data_cells / logical_cells
        return Result(self.circuit.name, self.policy.name, self.t, loads, stores,
                      loads + stores, busy, self.t - busy, logical_cells,
                      memory_density, self.cfg.cr_slots, self.cfg.n_banks,
                      self.cfg.bank_capacity, msf_stall_cycles, transfer_cycles)

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
    """Independently validate operation, placement, transfer, and resource constraints."""
    errs = []
    known = {"op", "load", "store", "magic_consume", "magic_produce"}
    for index, event in enumerate(trace):
        if event.get("kind") not in known:
            errs.append(f"event {index}: unknown kind {event.get('kind')!r}")
        if event.get("kind") in ("op", "load", "store"):
            if not isinstance(event.get("start"), (int, float)) or not isinstance(event.get("end"), (int, float)):
                errs.append(f"event {index}: missing/non-numeric interval")
            elif event["start"] < 0 or event["end"] < event["start"]:
                errs.append(f"event {index}: invalid interval [{event['start']}, {event['end']})")

    op_events = [e for e in trace if e.get("kind") == "op"]
    seen = {}
    for e in op_events:
        op_id = e.get("id")
        if not isinstance(op_id, int) or not 0 <= op_id < len(circuit.ops):
            errs.append(f"operation has invalid ID {op_id!r}")
            continue
        if op_id in seen:
            errs.append(f"op {op_id} executed twice")
            continue
        seen[op_id] = e
        op = circuit.ops[op_id]
        if tuple(e.get("qubits", ())) != tuple(op.qubits):
            errs.append(f"op {op_id}: operands differ from circuit")
        if e.get("end", 0) - e.get("start", 0) != cfg.timing.op_duration(op.kind, op.name):
            errs.append(f"op {op_id}: wrong duration")
        for pred in circuit.preds[op_id]:
            pe = next((x for x in op_events if x.get("id") == pred), None)
            if pe is None or pe.get("end", INF) > e.get("start", -INF):
                errs.append(f"op {op_id}: dependency on op {pred} violated")
    missing = sorted(set(range(len(circuit.ops))) - set(seen))
    if missing:
        errs.append(f"operations missing from trace: {missing[:10]}")

    # Reconstruct each qubit's residency and reserve source/destination resources
    # for the complete duration of each transfer.
    per_q = {q: [] for q in range(circuit.n_qubits)}
    for e in trace:
        kind = e.get("kind")
        if kind == "op":
            qubits = e.get("qubits", ())
        elif kind in ("load", "store"):
            qubits = (e.get("q"),)
        else:
            continue
        for q in qubits:
            if not isinstance(q, int) or q not in per_q:
                errs.append(f"event references invalid qubit {q!r}")
            else:
                per_q[q].append(e)

    slot_intervals, cell_intervals = {}, {}
    for q, events in per_q.items():
        initial = initial_loc.get(q)
        if not isinstance(initial, tuple) or not initial:
            errs.append(f"q{q}: invalid/missing initial location")
            continue
        if initial[0] == "SAM":
            if len(initial) != 3:
                errs.append(f"q{q}: malformed initial SAM location")
                continue
            _, bank, cell = initial
            if not (isinstance(bank, int) and 0 <= bank < cfg.n_banks
                    and isinstance(cell, int) and 0 <= cell < cfg.bank_capacity):
                errs.append(f"q{q}: initial SAM location out of range")
                continue
            state, current_cell, current_slot = "SAM", (bank, cell), None
            cell_start, slot_start = 0, None
        elif initial[0] == "CR":
            if len(initial) != 2 or not isinstance(initial[1], int) or not 0 <= initial[1] < cfg.cr_slots:
                errs.append(f"q{q}: invalid initial CR slot")
                continue
            state, current_cell, current_slot = "CR", None, initial[1]
            cell_start, slot_start = None, 0
        else:
            errs.append(f"q{q}: unsupported initial location")
            continue
        previous_end = 0
        events.sort(key=lambda x: (x["start"], x["end"]))
        for e in events:
            if e["start"] < previous_end:
                errs.append(f"q{q}: events overlap at t={e['start']}")
            previous_end = max(previous_end, e["end"])
            kind = e.get("kind")
            if kind == "load":
                b, c, slot = e.get("bank"), e.get("cell"), e.get("slot")
                if state != "SAM" or current_cell != (b, c):
                    errs.append(f"q{q}: load from invalid SAM location/state")
                if not (isinstance(b, int) and 0 <= b < cfg.n_banks
                        and isinstance(c, int) and 0 <= c < cfg.bank_capacity):
                    errs.append(f"q{q}: invalid load cell")
                if not (isinstance(slot, int) and 0 <= slot < cfg.cr_slots):
                    errs.append(f"q{q}: invalid CR slot")
                if current_cell is not None:
                    cell_intervals.setdefault(current_cell, []).append((cell_start, e["end"], q))
                state, current_cell, current_slot = "CR", None, slot
                slot_start = e["start"]
            elif kind == "store":
                b, c, slot = e.get("bank"), e.get("cell"), e.get("slot")
                if state != "CR" or current_slot != slot:
                    errs.append(f"q{q}: store from invalid CR slot/state")
                if not (isinstance(b, int) and 0 <= b < cfg.n_banks
                        and isinstance(c, int) and 0 <= c < cfg.bank_capacity):
                    errs.append(f"q{q}: invalid store destination")
                if current_slot is not None and slot_start is not None:
                    slot_intervals.setdefault(current_slot, []).append((slot_start, e["end"], q))
                state, current_cell, current_slot = "SAM", (b, c), None
                cell_start, slot_start = e["start"], None
            elif kind == "op" and state != "CR":
                errs.append(f"q{q}: op {e.get('id')} starts while qubit is not in CR")
        if current_cell is not None:
            cell_intervals.setdefault(current_cell, []).append((cell_start, INF, q))
        if current_slot is not None and slot_start is not None:
            slot_intervals.setdefault(current_slot, []).append((slot_start, INF, q))

    initial_cells = [(initial_loc[q][1], initial_loc[q][2]) for q in initial_loc
                     if isinstance(initial_loc[q], tuple) and len(initial_loc[q]) == 3
                     and initial_loc[q][0] == "SAM"]
    initial_slots = [initial_loc[q][1] for q in initial_loc
                     if isinstance(initial_loc[q], tuple) and len(initial_loc[q]) == 2
                     and initial_loc[q][0] == "CR"]
    if len(initial_cells) != len(set(initial_cells)):
        errs.append("initial placement assigns multiple qubits to one cell")
    if len(initial_slots) != len(set(initial_slots)):
        errs.append("initial placement assigns multiple qubits to one CR slot")
    if set(initial_loc) != set(range(circuit.n_qubits)):
        errs.append("initial placement does not cover exactly all circuit qubits")

    def no_overlap(intervals, label):
        ordered = sorted(intervals, key=lambda x: (x[0], x[1]))
        for left, right in zip(ordered, ordered[1:]):
            if right[0] < left[1]:
                errs.append(f"{label}: overlap {left} vs {right}")

    for slot, intervals in slot_intervals.items():
        no_overlap(intervals, f"CR slot {slot}")
    for cell_key, intervals in cell_intervals.items():
        no_overlap(intervals, f"SAM cell {cell_key}")

    transfers = [e for e in trace if e.get("kind") in ("load", "store")]
    for bank in range(cfg.n_banks):
        bank_events = sorted((e for e in transfers if e.get("bank") == bank),
                             key=lambda e: (e["start"], e["end"]))
        no_overlap([(e["start"], e["end"], e.get("q")) for e in bank_events],
                   f"bank {bank} port")
        row = cfg.line_initial_row
        for e in bank_events:
            cell = e.get("cell")
            if not isinstance(cell, int) or not 0 <= cell < cfg.bank_capacity:
                continue
            if e["end"] - e["start"] != cfg.xfer_time(cell, row):
                errs.append(f"{e['kind']} q{e.get('q')}: wrong SAM transfer duration")
            if e.get("scan_row_before") != row or e.get("target_row") != cfg.depth(cell):
                errs.append(f"{e['kind']} q{e.get('q')}: inconsistent scan-line metadata")
            row = cfg.depth(cell)

    lane_intervals = {}
    for e in transfers:
        lane = e.get("lane")
        if not isinstance(lane, int) or not 0 <= lane < cfg.bus_lanes:
            errs.append(f"transfer q{e.get('q')}: invalid bus lane")
        else:
            lane_intervals.setdefault(lane, []).append((e["start"], e["end"], e.get("q")))
    for lane, intervals in lane_intervals.items():
        no_overlap(intervals, f"bus lane {lane}")
    if _max_concurrency([(e["start"], e["end"]) for e in transfers]) > cfg.bus_lanes:
        errs.append("bus-lane limit exceeded")
    if _max_concurrency([(e["start"], e["end"]) for e in op_events]) > cfg.max_parallel_ops:
        errs.append("parallel-operation limit exceeded")

    # Reconstruct the exact aggregate factory inventory. Every factory tick
    # emits min(factory_count, available_buffer_slots); no catch-up production
    # is allowed after a full buffer. Production is processed before consumption
    # at the same timestamp, matching Simulator._tick().
    magic_events = [e for e in trace if e.get("kind") in ("magic_produce", "magic_consume")]
    production_by_time = {}
    consumption_by_time = {}
    # Factory ticks occur even while the buffer is full. Use the complete trace
    # makespan to enumerate every tick; absent production events are valid only
    # when the reconstructed buffer is full at that tick.
    # _tick() runs at the start of each scheduler iteration and immediately
    # before a T-gate start. The last processed factory tick is therefore bounded
    # by the latest scheduled event start, not the makespan/end timestamp.
    processed_through = max(
        [0]
        + [e.get("start", 0) for e in trace
           if e.get("kind") in ("op", "load", "store")
           and isinstance(e.get("start"), (int, float))]
        + [e.get("time", 0) for e in magic_events
           if isinstance(e.get("time"), (int, float))]
    )
    for event in magic_events:
        time = event.get("time")
        if (not isinstance(time, (int, float)) or isinstance(time, bool)
                or not math.isfinite(time) or time < 0):
            errs.append("magic-state event has invalid timestamp")
            continue
        if event["kind"] == "magic_produce":
            count = event.get("count")
            if not isinstance(count, int) or isinstance(count, bool) or not 0 < count <= cfg.timing.magic_factories:
                errs.append("invalid magic-state production count")
                continue
            production_by_time[time] = production_by_time.get(time, 0) + count
        else:
            consumption_by_time.setdefault(time, []).append(event)

    expected_ticks = set(range(cfg.timing.magic_interval,
                               int(processed_through) + 1,
                               cfg.timing.magic_interval))
    for time in production_by_time:
        if time not in expected_ticks:
            errs.append(f"magic-state production is off factory cadence at t={time}")

    stock = cfg.timing.magic_buffer
    all_times = sorted(expected_ticks | set(consumption_by_time) | set(production_by_time))
    consumed_ids = []
    for time in all_times:
        expected = (
            min(cfg.timing.magic_factories,
                max(0, cfg.timing.magic_buffer - stock))
            if time in expected_ticks else 0
        )
        actual = production_by_time.get(time, 0)
        if actual != expected:
            errs.append(
                f"magic-state production mismatch at t={time}: expected {expected}, got {actual}"
            )
        stock += actual
        if stock > cfg.timing.magic_buffer:
            errs.append(f"magic-state buffer overflow at t={time}")

        for event in consumption_by_time.get(time, []):
            op_id = event.get("op_id")
            op_event = seen.get(op_id)
            if (op_event is None or op_event.get("start") != time
                    or not isinstance(op_id, int) or isinstance(op_id, bool)
                    or not 0 <= op_id < len(circuit.ops)
                    or circuit.ops[op_id].kind != "T"):
                errs.append("magic-state consumption does not match a T-gate start")
            consumed_ids.append(op_id)
            stock -= 1
            if stock < 0:
                errs.append(f"magic-state inventory became negative at t={time}")

    t_ops = {e["id"] for e in op_events if 0 <= e.get("id", -1) < len(circuit.ops)
             and circuit.ops[e["id"]].kind == "T"}
    if len(consumed_ids) != len(t_ops) or set(consumed_ids) != t_ops:
        errs.append("magic-state consumption events do not match T-gate executions")
    return errs
