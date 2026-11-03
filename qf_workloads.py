"""Benchmark workloads for the controlled experiment suite.

Arithmetic circuits are first built at the *reversible* level (x/cx/ccx) so their function can be
verified by classical bit-simulation (tests), then lowered to Clifford+T with the standard 7-T
Toffoli (itself verified against the exact CCX unitary in tests).

  adder(n)             Cuccaro ripple-carry adder           (2n+2 qubits)
  multiplier(n)        schoolbook shift-and-add multiplier  (5n+1 qubits, 2n-bit product)
  qft(n, max_k)        textbook QFT, cp -> cx + rz; (approximate-QFT cutoff via max_k)
  parallel_adders(k,n) k disjoint adders, gates interleaved round-robin (high parallelism)
  random(n, ops, seed) seeded random Clifford+T mix

QFT / rz synthesis is APPROXIMATE (see qf_core.rz_to_gates).
"""
import math
import qf_core as core
from qf_core import Circuit, CircuitError, _toffoli, rz_to_gates


# ---------------------------------------------------------------- reversible building blocks
def cuccaro_add_gates(cin, A, B, z, ctrl_free=True):
    """B += A (mod 2^n), carry-out XORed into z. cin must be |0> and is restored."""
    n = len(A)
    C = lambda i: cin if i == 0 else A[i - 1]
    g = []
    for i in range(n):                                   # MAJ
        g += [("cx", (A[i], B[i])), ("cx", (A[i], C(i))), ("ccx", (C(i), B[i], A[i]))]
    g.append(("cx", (A[n - 1], z)))
    for i in reversed(range(n)):                         # UMA
        g += [("ccx", (C(i), B[i], A[i])), ("cx", (A[i], C(i))), ("cx", (C(i), B[i]))]
    return g


def adder_rev(n):
    cin, z = 0, 2 * n + 1
    A = [1 + 2 * i for i in range(n)]
    B = [2 + 2 * i for i in range(n)]
    return 2 * n + 2, cuccaro_add_gates(cin, A, B, z), dict(cin=cin, A=A, B=B, z=z)


def multiplier_rev(n):
    """a (n bits), b (n bits), tmp (n), acc (2n), cin (1). acc += a*b."""
    a = list(range(0, n)); b = list(range(n, 2 * n)); tmp = list(range(2 * n, 3 * n))
    acc = list(range(3 * n, 5 * n)); cin = 5 * n
    g = []
    for j in range(n):
        g += [("ccx", (a[i], b[j], tmp[i])) for i in range(n)]             # tmp = a & b_j
        g += cuccaro_add_gates(cin, tmp, acc[j:j + n], acc[j + n])         # acc[j..j+n] += tmp
        g += [("ccx", (a[i], b[j], tmp[i])) for i in range(n)]             # uncompute tmp
    return 5 * n + 1, g, dict(a=a, b=b, tmp=tmp, acc=acc, cin=cin)


def simulate_reversible(n_qubits, gates, bits):
    """Classical simulation of x/cx/ccx on a list/dict of bits (returns a new list)."""
    s = list(bits)
    for g, qs in gates:
        if g == "x": s[qs[0]] ^= 1
        elif g == "cx": s[qs[1]] ^= s[qs[0]]
        elif g == "ccx": s[qs[2]] ^= s[qs[0]] & s[qs[1]]
        else: raise ValueError(g)
    return s


def lower_reversible(gates):
    out = []
    for g, qs in gates:
        out += _toffoli(*qs) if g == "ccx" else [(g, qs)]
    return out


# ---------------------------------------------------------------- circuits
def adder(n):
    nq, g, _ = adder_rev(n)
    return Circuit.from_gates(nq, lower_reversible(g), f"adder{n}")


def multiplier(n):
    nq, g, _ = multiplier_rev(n)
    return Circuit.from_gates(nq, lower_reversible(g), f"multiplier{n}")


def qft(n, max_k=None, rz_t_count=4):
    """Textbook QFT without the final bit-reversal swaps (a relabelling). cp(theta) with
    theta = pi/2^m is lowered as rz(theta/2)(c), cx, rz(-theta/2)(t), cx, rz(theta/2)(t).
    max_k: approximate QFT -- drop rotations with m > max_k."""
    if n < 2:
        raise CircuitError("qft needs n >= 2")
    g = []
    for j in range(n):
        g.append(("h", (j,)))
        for k in range(j + 1, n):
            m = k - j
            if max_k is not None and m > max_k:
                continue
            th = math.pi / 2 ** m
            g += rz_to_gates(th / 2, (k,), rz_t_count)
            g.append(("cx", (k, j)))
            g += rz_to_gates(-th / 2, (j,), rz_t_count)
            g.append(("cx", (k, j)))
            g += rz_to_gates(th / 2, (j,), rz_t_count)
    return Circuit.from_gates(n, g, f"qft{n}" + (f"k{max_k}" if max_k else ""))


def parallel_adders(k, n_bits, interleave=True):
    if k < 1:
        raise CircuitError("need k >= 1")
    nq, g, _ = adder_rev(n_bits)
    low = lower_reversible(g)
    per = [[(nm, tuple(q + b * nq for q in qs)) for nm, qs in low] for b in range(k)]
    if interleave:
        gates = [blk[i] for i in range(len(per[0])) for blk in per]
    else:
        gates = [x for blk in per for x in blk]
    return Circuit.from_gates(k * nq, gates, f"padder{k}x{n_bits}")


def random_workload(n, ops, seed, **kw):
    return core.random_circuit(n, ops, seed=seed, **kw)


def build_workload(spec: dict) -> Circuit:
    """spec['kind'] in adder|multiplier|qft|parallel_adders|random|qasm|mqt (+ params)."""
    k = spec["kind"]
    if k == "adder": return adder(spec["n"])
    if k == "multiplier": return multiplier(spec["n"])
    if k == "qft": return qft(spec["n"], spec.get("max_k"), spec.get("rz_t_count", 4))
    if k == "parallel_adders": return parallel_adders(spec["k"], spec["n"], spec.get("interleave", True))
    if k == "random":
        if "seed" not in spec:
            raise CircuitError("random workloads require an explicit 'seed'")
        return core.random_circuit(spec["n"], spec["ops"], seed=spec["seed"])
    if k == "qasm": return core.from_qasm(spec["path"])
    if k == "mqt": return core.from_mqt_bench(spec["name"], spec["n"])
    raise CircuitError(f"unknown workload kind {k!r}")
