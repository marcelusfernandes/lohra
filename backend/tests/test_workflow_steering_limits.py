"""TDD (ciclo vermelho): limites de steering por leaf e por run.

Especifica a API de ``lohra.workflow.steering``:
- constantes ``MAX_CORRECTIONS_PER_LEAF`` / ``MAX_EXTERNAL_STEERS_PER_LEAF`` /
  ``MAX_EXTERNAL_STEERS_PER_RUN``;
- ``SteeringLimits.reserve_external(sub_id)`` / ``reserve_internal(sub_id)``
  -> resultado com ``accepted``, ``reason``, ``leaf_used``, ``run_used``,
  ``corrections_used``;
- ``SteeringLimits.rollback_external(sub_id)`` libera os contadores da
  reserva externa do leaf.
"""

import threading

import pytest
from concurrent.futures import ThreadPoolExecutor

from lohra.workflow.steering import (
    MAX_CORRECTIONS_PER_LEAF,
    MAX_EXTERNAL_STEERS_PER_LEAF,
    MAX_EXTERNAL_STEERS_PER_RUN,
    SteeringLimits,
)


def test_limites_de_steering_externo_e_interno_com_rollback():
    assert MAX_CORRECTIONS_PER_LEAF == 2
    assert MAX_EXTERNAL_STEERS_PER_LEAF == 1
    assert MAX_EXTERNAL_STEERS_PER_RUN == 3

    limits = SteeringLimits()

    # 1o external no leaf-a: aceito
    r1 = limits.reserve_external("leaf-a")
    assert r1.accepted is True
    assert r1.leaf_used == 1
    assert r1.run_used == 1

    # 2o external no MESMO leaf: recusado pelo limite do leaf
    r2 = limits.reserve_external("leaf-a")
    assert r2.accepted is False
    assert r2.reason == "leaf_limit"

    # external em outros dois leaves: aceitos (run chega ao teto 3)
    r3 = limits.reserve_external("leaf-b")
    assert r3.accepted is True
    assert r3.run_used == 2
    r4 = limits.reserve_external("leaf-c")
    assert r4.accepted is True
    assert r4.run_used == 3

    # 4o leaf: recusado pelo limite do run
    r5 = limits.reserve_external("leaf-d")
    assert r5.accepted is False
    assert r5.reason == "run_limit"

    # external + 1 internal no mesmo leaf-a: corrections_used chega a 2
    ri1 = limits.reserve_internal("leaf-a")
    assert ri1.accepted is True
    assert ri1.corrections_used == 2

    # proximo internal no leaf-a: recusado pelo limite de correcoes
    ri2 = limits.reserve_internal("leaf-a")
    assert ri2.accepted is False
    assert ri2.reason == "correction_limit"

    # rollback libera os contadores da reserva externa do leaf-a
    limits.rollback_external("leaf-a")

    # leaf-a pode reservar de novo (contador do leaf liberado)...
    r6 = limits.reserve_external("leaf-a")
    assert r6.accepted is True
    assert r6.leaf_used == 1
    # ...e o run, que caiu a 2 com o rollback, volta ao teto 3
    assert r6.run_used == 3

    # run no teto de novo: leaf novo recusa
    r7 = limits.reserve_external("leaf-e")
    assert r7.accepted is False
    assert r7.reason == "run_limit"


def test_concorrencia_oito_reservas_externas_no_mesmo_leaf_aceita_exatamente_uma():
    """8 reserve_external('same') simultaneos: 1 aceito, 7 leaf_limit.

    Deterministico: threading.Barrier solta as 8 chamadas juntas e cada
    thread escreve o receipt na propria posicao do vetor de resultados.
    """
    limits = SteeringLimits()
    n = 8
    barrier = threading.Barrier(n)
    results: list = [None] * n

    def worker(i: int):
        barrier.wait()  # todas as chamadas simultaneas
        results[i] = limits.reserve_external("same")

    with ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(worker, range(n)))

    accepted = [r for r in results if r.accepted]
    rejected = [r for r in results if not r.accepted]

    assert len(accepted) == 1
    assert len(rejected) == 7
    assert all(r.reason == "leaf_limit" for r in rejected)

    winner = accepted[0]
    assert winner.run_used == 1
    assert winner.corrections_used == 1


# Settlement is a delivery receipt: read spends the slot; discarded refunds it.


class TestSettleExternalRead:
    def test_read_preserves_counters_and_new_reserve_refuses_leaf_limit(self) -> None:
        limits = SteeringLimits()

        assert limits.reserve_external("leaf-a").accepted is True

        # The steer landed: the slot is spent, every counter stands.
        assert limits.settle_external("leaf-a", "read") is True

        # The leaf stays at its external ceiling — a new reserve is refused.
        refused = limits.reserve_external("leaf-a")
        assert refused.accepted is False
        assert refused.reason == "leaf_limit"
        assert refused.leaf_used == 1
        assert refused.corrections_used == 1
        assert refused.run_used == 1


class TestSettleExternalDiscarded:
    def test_discarded_returns_slot_and_new_reserve_is_accepted(self) -> None:
        limits = SteeringLimits()

        assert limits.reserve_external("leaf-a").accepted is True

        # The steer never landed: counters fall back, slot returns.
        assert limits.settle_external("leaf-a", "discarded") is True

        # The freed slot can be reserved again (leaf AND run budgets).
        again = limits.reserve_external("leaf-a")
        assert again.accepted is True
        assert again.leaf_used == 1
        assert again.run_used == 1
        assert again.corrections_used == 1


class TestSettleExternalEdgeCases:
    def test_second_settlement_returns_false(self) -> None:
        limits = SteeringLimits()

        assert limits.reserve_external("leaf-a").accepted is True
        assert limits.settle_external("leaf-a", "read") is True
        # The leaf has no open slot anymore.
        assert limits.settle_external("leaf-a", "read") is False
        assert limits.settle_external("leaf-a", "discarded") is False

    def test_invalid_outcome_raises_valueerror(self) -> None:
        limits = SteeringLimits()
        limits.reserve_external("leaf-a")

        with pytest.raises(ValueError):
            limits.settle_external("leaf-a", "seen")

    def test_unknown_leaf_has_no_open_slot(self) -> None:
        limits = SteeringLimits()

        assert limits.settle_external("never-seen", "read") is False
