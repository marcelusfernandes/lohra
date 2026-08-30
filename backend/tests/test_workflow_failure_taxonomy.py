"""SUP-05 fatia 1 — taxonomia estrutural de failure observation (TDD).

Contrato fail-closed sob teste:

- a responsabilidade é decidida por **mechanism + signals de evidência**,
  NUNCA pelo status do run nem pelo tipo cru do fault — mesmo status/tipo
  com evidências diferentes têm responsabilidades opostas;
- só AGENCY com confiança >= ``AGENCY_CONFIDENCE_MIN`` é learnable;
- tudo que fica subdeterminado degrada para UNKNOWN e nunca é learnable —
  classificar sem evidência é falhar aberto, e isso é proibido aqui.
"""

from __future__ import annotations

import pytest

from lohra.workflow.failure_taxonomy import (
    AGENCY_CONFIDENCE_MIN,
    SIGNAL_HARNESS_INTERNAL,
    SIGNAL_PROVIDER_SIDE,
    SIGNAL_SPEC_SHAPE,
    Mechanism,
    Responsibility,
    classify_failure,
)


# --- mesmo status/tipo NÃO determina responsabilidade (contraexemplos) ------


def test_same_status_and_mechanism_split_between_agency_and_environment() -> None:
    """Dois runs 'failed' com mechanism validation: a evidência decide.

    O mesmo par (status, mechanism) tem de poder cair dos dois lados — se a
    taxonomia só olhasse status/tipo, um provider quebrado seria 'aprendido'
    como defeito de autoria (ou pior, o contrário)."""
    agency = classify_failure(
        status="failed",
        mechanism=Mechanism.VALIDATION,
        signals=(SIGNAL_SPEC_SHAPE,),
        confidence=0.9,
        summary="leaf output missing required field",
    )
    environment = classify_failure(
        status="failed",
        mechanism=Mechanism.VALIDATION,
        signals=(SIGNAL_PROVIDER_SIDE,),
        confidence=0.9,
        summary="leaf output missing required field",
    )
    assert agency.responsibility is Responsibility.AGENCY
    assert environment.responsibility is Responsibility.ENVIRONMENT
    assert agency.is_learnable
    assert not environment.is_learnable


def test_same_timeout_mechanism_splits_between_environment_and_infrastructure() -> None:
    provider = classify_failure(
        status="degraded",
        mechanism=Mechanism.TIMEOUT,
        signals=(SIGNAL_PROVIDER_SIDE,),
        confidence=0.9,
    )
    harness = classify_failure(
        status="degraded",
        mechanism=Mechanism.TIMEOUT,
        signals=(SIGNAL_HARNESS_INTERNAL,),
        confidence=0.9,
    )
    assert provider.responsibility is Responsibility.ENVIRONMENT
    assert harness.responsibility is Responsibility.INFRASTRUCTURE
    assert not provider.is_learnable and not harness.is_learnable


def test_cancelled_status_is_never_attributed() -> None:
    """'cancelled' não diz quem cancelou — a observação não carrega isso.

    Qualquer signal que exista, o status cancelled não autoriza atribuição:
    um cancelamento humano, um budget e um crash do orquestrador produzem a
    mesma linha aqui."""
    for signals in ((), (SIGNAL_SPEC_SHAPE,), (SIGNAL_HARNESS_INTERNAL,)):
        obs = classify_failure(
            status="cancelled",
            mechanism=Mechanism.CANCELLATION,
            signals=signals,
            confidence=1.0,
        )
        assert obs.responsibility is Responsibility.UNKNOWN
        assert not obs.is_learnable


def test_external_rejection_same_status_with_and_without_provider_evidence() -> None:
    rejected = classify_failure(
        status="failed",
        mechanism=Mechanism.EXTERNAL_REJECTION,
        signals=(SIGNAL_PROVIDER_SIDE,),
        confidence=0.9,
    )
    unattributed = classify_failure(
        status="failed",
        mechanism=Mechanism.EXTERNAL_REJECTION,
        signals=(),
        confidence=0.9,
    )
    assert rejected.responsibility is Responsibility.ENVIRONMENT
    assert unattributed.responsibility is Responsibility.UNKNOWN


# --- fail-closed: sem evidência / mechanism unknown -> UNKNOWN --------------


def test_unknown_mechanism_is_never_learnable_even_at_full_confidence() -> None:
    obs = classify_failure(
        status="failed",
        mechanism=Mechanism.UNKNOWN,
        signals=(SIGNAL_SPEC_SHAPE,),
        confidence=1.0,
    )
    assert obs.responsibility is Responsibility.UNKNOWN
    assert not obs.is_learnable


def test_garbage_mechanism_string_degrades_to_unknown() -> None:
    obs = classify_failure(
        status="failed",
        mechanism="quantum_interference",
        signals=(SIGNAL_SPEC_SHAPE,),
        confidence=1.0,
    )
    assert obs.mechanism is Mechanism.UNKNOWN
    assert obs.responsibility is Responsibility.UNKNOWN
    assert not obs.is_learnable


def test_no_signals_is_unknown_even_at_full_confidence() -> None:
    obs = classify_failure(
        status="failed",
        mechanism=Mechanism.VALIDATION,
        signals=(),
        confidence=1.0,
        summary="leaf returned null",
    )
    assert obs.responsibility is Responsibility.UNKNOWN
    assert not obs.is_learnable


def test_agency_below_confidence_floor_degrades_to_unknown_not_agency() -> None:
    """Confiança baixa NÃO vira 'agency com ressalva': vira UNKNOWN.

    Fail-closed significa que o degrau entre 'sabemos' e 'não sabemos' é o
    floor — nunca um meio-termo learnable."""
    low = classify_failure(
        status="failed",
        mechanism=Mechanism.VALIDATION,
        signals=(SIGNAL_SPEC_SHAPE,),
        confidence=AGENCY_CONFIDENCE_MIN - 0.01,
    )
    assert low.responsibility is Responsibility.UNKNOWN
    assert not low.is_learnable


def test_agency_exactly_at_floor_is_learnable() -> None:
    obs = classify_failure(
        status="failed",
        mechanism=Mechanism.VALIDATION,
        signals=(SIGNAL_SPEC_SHAPE,),
        confidence=AGENCY_CONFIDENCE_MIN,
    )
    assert obs.responsibility is Responsibility.AGENCY
    assert obs.is_learnable


def test_only_agency_is_ever_learnable() -> None:
    for mechanism, signal in (
        (Mechanism.TRANSPORT, SIGNAL_HARNESS_INTERNAL),
        (Mechanism.TIMEOUT, SIGNAL_PROVIDER_SIDE),
        (Mechanism.EXTERNAL_REJECTION, SIGNAL_PROVIDER_SIDE),
    ):
        obs = classify_failure(
            status="failed", mechanism=mechanism, signals=(signal,), confidence=1.0
        )
        assert not obs.is_learnable


# --- normalização de entrada -------------------------------------------------


def test_mechanism_accepts_plain_string() -> None:
    obs = classify_failure(
        status="failed",
        mechanism="validation",
        signals=(SIGNAL_SPEC_SHAPE,),
        confidence=0.9,
    )
    assert obs.mechanism is Mechanism.VALIDATION
    assert obs.responsibility is Responsibility.AGENCY


def test_confidence_is_clamped_into_unit_interval() -> None:
    over = classify_failure(
        status="failed",
        mechanism=Mechanism.VALIDATION,
        signals=(SIGNAL_SPEC_SHAPE,),
        confidence=7.0,
    )
    under = classify_failure(
        status="failed",
        mechanism=Mechanism.VALIDATION,
        signals=(SIGNAL_SPEC_SHAPE,),
        confidence=-1.0,
    )
    assert over.confidence == 1.0
    assert under.confidence == 0.0
    assert not under.is_learnable


def test_summary_and_signals_are_bounded() -> None:
    obs = classify_failure(
        status="failed",
        mechanism=Mechanism.VALIDATION,
        signals=(SIGNAL_SPEC_SHAPE, "x" * 500, "y" * 500, "z" * 500),
        confidence=0.9,
        summary="boom " * 4000,
    )
    assert len(obs.summary) <= 500
    assert len(obs.signals) <= 8
    assert all(len(signal) <= 128 for signal in obs.signals)


def test_observation_is_immutable() -> None:
    obs = classify_failure(
        status="failed",
        mechanism=Mechanism.VALIDATION,
        signals=(SIGNAL_SPEC_SHAPE,),
        confidence=0.9,
    )
    with pytest.raises(Exception):
        obs.responsibility = Responsibility.INFRASTRUCTURE  # type: ignore[misc]


def test_status_is_carried_honestly_but_never_decides() -> None:
    """O status entra na observação (telemetria honesta), mas a decisão de
    responsabilidade é idêntica para statuses diferentes quando mechanism e
    signals são iguais."""
    failed = classify_failure(
        status="failed",
        mechanism=Mechanism.VALIDATION,
        signals=(SIGNAL_SPEC_SHAPE,),
        confidence=0.9,
    )
    degraded = classify_failure(
        status="degraded",
        mechanism=Mechanism.VALIDATION,
        signals=(SIGNAL_SPEC_SHAPE,),
        confidence=0.9,
    )
    assert failed.status == "failed"
    assert degraded.status == "degraded"
    assert failed.responsibility is degraded.responsibility
    assert failed.is_learnable == degraded.is_learnable


def test_resource_with_harness_evidence_is_infrastructure() -> None:
    obs = classify_failure(
        status="failed",
        mechanism=Mechanism.RESOURCE,
        signals=(SIGNAL_HARNESS_INTERNAL,),
        confidence=0.9,
    )
    assert obs.responsibility is Responsibility.INFRASTRUCTURE
    assert not obs.is_learnable


def test_resource_without_evidence_stays_unknown() -> None:
    obs = classify_failure(
        status="failed",
        mechanism=Mechanism.RESOURCE,
        signals=(),
        confidence=0.9,
    )
    assert obs.responsibility is Responsibility.UNKNOWN
