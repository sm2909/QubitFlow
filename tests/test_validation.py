"""Input / configuration validation (item 2): invalid inputs are rejected with clear errors."""
import pytest
import qf_core as core
from qf_core import ArchConfig, Timing, Circuit, Op, ConfigError, CircuitError
from qf_policies import make_policy, FixedOrderPolicy, DagPolicy


@pytest.mark.parametrize("field,val", [
    ("cr_slots", 0), ("cr_slots", 1), ("cr_slots", -3), ("n_banks", 0), ("bank_capacity", 0),
    ("bank_width", 0), ("bus_lanes", 0), ("max_parallel_ops", 0), ("d", 2), ("d", 10), ("d", 1),
    ("d", -5), ("cr_slots", 2.5), ("n_banks", True), ("sam_model", "bogus"),
    ("cr_ancilla_ratio", -1), ("bus_tiles_per_lane", -1), ("factory_tiles", -1)])
def test_arch_rejects_bad_values(field, val):
    with pytest.raises(ConfigError):
        ArchConfig(**{field: val})


@pytest.mark.parametrize("field,val", [
    ("t_1q", 0), ("t_2q", -1), ("t_T", 0), ("t_meas", 0), ("t_h", 0), ("t_s", -2),
    ("magic_interval", 0), ("magic_buffer", 0), ("magic_buffer", -1), ("line_shift", 0),
    ("port_move", 0), ("xfer_base", -1), ("magic_unlimited", 1), ("magic_interval", 1.5)])
def test_timing_rejects_bad_values(field, val):
    with pytest.raises(ConfigError):
        Timing(**{field: val})


def test_generic_transfer_must_take_time():
    with pytest.raises(ConfigError):
        Timing(xfer_base=0, xfer_per_depth=0)


def test_point_sam_not_implemented():
    with pytest.raises(NotImplementedError):
        ArchConfig(sam_model="point")


def test_error_messages_name_the_field():
    with pytest.raises(ConfigError, match="bank_width"):
        ArchConfig(bank_width=0)
    with pytest.raises(ConfigError, match="magic_interval"):
        Timing(magic_interval=0)


def test_defaults_are_valid():
    ArchConfig(); Timing(); Timing.lsqca_paper()


def test_banks_too_small_for_circuit():
    c = Circuit.from_gates(10, [("h", (0,))])
    with pytest.raises(ConfigError, match="too small"):
        core.Simulator(c, ArchConfig(n_banks=2, bank_capacity=4), make_policy("demand_lru"))


def test_simulator_type_checks():
    with pytest.raises(TypeError):
        core.Simulator("nope", ArchConfig(), make_policy("demand_lru"))


# ---------------------------------------------------------------- circuits
def test_circuit_rejects_out_of_range_qubit():
    with pytest.raises(CircuitError, match="out of range"):
        Circuit.from_gates(2, [("h", (2,))])
    with pytest.raises(CircuitError, match="out of range"):
        Circuit.from_gates(2, [("cx", (0, -1))])


def test_circuit_rejects_duplicate_operands():
    with pytest.raises(CircuitError, match="duplicate"):
        Circuit.from_gates(3, [("cx", (1, 1))])


@pytest.mark.parametrize("gate", [("h", (0, 1)), ("cx", (0,)), ("t", ()), ("cx", (0, 1, 2))])
def test_circuit_rejects_wrong_arity(gate):
    with pytest.raises(CircuitError):
        Circuit.from_gates(3, [gate])


def test_circuit_rejects_unknown_gate():
    with pytest.raises(CircuitError, match="unsupported gate"):
        Circuit.from_gates(2, [("ccx", (0, 1, 1))])
    with pytest.raises(CircuitError, match="unsupported gate"):
        Circuit.from_gates(2, [("reset", (0,))])


def test_circuit_rejects_bad_op_ids_and_kinds():
    with pytest.raises(CircuitError, match="ids must equal position"):
        Circuit(2, [Op(1, "h", (0,), "h")])
    with pytest.raises(CircuitError, match="unknown kind"):
        Circuit(2, [Op(0, "weird", (0,), "w")])


def test_circuit_rejects_bad_qubit_count():
    for n in (0, -1, 2.0, True):
        with pytest.raises(CircuitError):
            Circuit(n, [])


def test_dependency_validation_and_cycles():
    g = [("h", (0,)), ("h", (1,)), ("h", (2,))]
    with pytest.raises(CircuitError, match="invalid op"):
        Circuit.from_gates(3, g, extra_deps={2: [7]})
    with pytest.raises(CircuitError, match="itself"):
        Circuit.from_gates(3, g, extra_deps={1: [1]})
    with pytest.raises(CircuitError, match="cycle"):
        Circuit.from_gates(3, g, extra_deps={0: [1], 1: [2], 2: [0]})
    c = Circuit.from_gates(3, g, extra_deps={2: [0, 1]})
    assert c.preds[2] == [0, 1] and c.level == [0, 0, 1]


def test_extra_deps_are_respected_by_scheduler():
    from helpers import cfg, run, op_ev
    c = Circuit.from_gates(3, [("h", (0,)), ("h", (1,)), ("h", (2,))], extra_deps={0: [2]})
    sim, _ = run(c, cfg(), "dag")                       # DAG policy: op 2 runs before op 0
    assert op_ev(sim, 2)["end"] <= op_ev(sim, 0)["start"]
    for pol in ("demand_lru", "prefetch_belady"):       # fixed-order policies must refuse, not deadlock
        with pytest.raises(ValueError, match="topological"):
            run(c, cfg(), pol, validate=False)


def test_random_circuit_requires_integer_seed_and_is_reproducible():
    with pytest.raises(CircuitError, match="seed"):
        core.random_circuit(5, 10, seed="a")
    a, b = core.random_circuit(8, 50, seed=3), core.random_circuit(8, 50, seed=3)
    c = core.random_circuit(8, 50, seed=4)
    assert a.fingerprint() == b.fingerprint() != c.fingerprint()


def test_policy_parameter_validation():
    with pytest.raises(ValueError):
        FixedOrderPolicy(0, "fifo")
    with pytest.raises(ValueError):
        FixedOrderPolicy(-1)
    with pytest.raises(ValueError):
        DagPolicy(prefetch_window=-1)
    with pytest.raises(KeyError, match="unknown policy"):
        make_policy("nope")


def test_critical_path_lengths():
    c = Circuit.from_gates(2, [("h", (0,)), ("cx", (0, 1)), ("t", (1,)), ("h", (0,))])
    T = Timing(t_1q=1, t_2q=2, t_T=5)
    cp = c.critical_path(T)
    assert cp[2] == 5 and cp[3] == 1 and cp[1] == 2 + 5 and cp[0] == 1 + 7
