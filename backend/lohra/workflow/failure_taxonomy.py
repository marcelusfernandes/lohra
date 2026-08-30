"""Structural taxonomy for failure observation (SUP-05, slice 1).

Separates the two questions every workflow failure observation must answer
independently:

- **mechanism** — what mechanically went wrong (validation, transport,
  timeout, external rejection, resource, cancellation);
- **responsibility** — who owns it (infrastructure / environment / agency /
  unknown).

The classification is FAIL-CLOSED along two axes:

- responsibility is decided by mechanism + evidence signals, NEVER by the
  run status or the raw fault type — the same (status, mechanism) pair can
  fall on opposite sides with different evidence (the anti-pattern this
  exists to kill: learning from a provider outage as if it were an
  authoring defect, or the reverse);
- only AGENCY at confidence >= ``AGENCY_CONFIDENCE_MIN`` is learnable.
  Anything underdetermined degrades to UNKNOWN and is never learnable:
  classifying without evidence would be failing open.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

# Evidence signals a caller may attach. They are opaque strings by design:
# the taxonomy reasons over the three below and tolerates arbitrary extras
# (bounded) so callers can carry provenance without re-versioning this file.
SIGNAL_SPEC_SHAPE = "spec_shape"
SIGNAL_PROVIDER_SIDE = "provider_side"
SIGNAL_HARNESS_INTERNAL = "harness_internal"

# The confidence floor under which an otherwise-agency observation degrades
# to UNKNOWN. There is no "agency with reservations": below the floor the
# observation is not learnable, full stop.
AGENCY_CONFIDENCE_MIN = 0.8

MAX_SIGNALS = 8
MAX_SIGNAL_CHARS = 128
MAX_SUMMARY_CHARS = 500
MAX_STATUS_CHARS = 32


class Mechanism(Enum):
    """What mechanically went wrong — independent of who owns it."""

    VALIDATION = "validation"
    TRANSPORT = "transport"
    TIMEOUT = "timeout"
    EXTERNAL_REJECTION = "external_rejection"
    RESOURCE = "resource"
    CANCELLATION = "cancellation"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, value: object) -> "Mechanism":
        # A mechanism we do not recognise is UNKNOWN — never a guess.
        return cls.UNKNOWN


class Responsibility(Enum):
    """Who owns the failure — the only axis learnability keys on."""

    INFRASTRUCTURE = "infrastructure"
    ENVIRONMENT = "environment"
    AGENCY = "agency"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class FailureObservation:
    """One bounded, immutable failure observation."""

    status: str
    mechanism: Mechanism
    responsibility: Responsibility
    confidence: float
    signals: tuple[str, ...] = ()
    summary: str = ""

    @property
    def is_learnable(self) -> bool:
        """Only high-confidence AGENCY observations may feed learning."""
        return self.responsibility is Responsibility.AGENCY


def _coerce_enum(value: Any, enum_type: type[Enum]) -> Enum:
    if isinstance(value, enum_type):
        return value
    return enum_type(str(value)) if value is not None else enum_type("unknown")


def classify_failure(
    *,
    status: str,
    mechanism: Mechanism | str,
    signals: tuple[str, ...] | list[str] = (),
    confidence: float,
    summary: str = "",
) -> FailureObservation:
    """Classify one failure observation. Never raises on odd input.

    Normalisation is bounded (signals capped in count and length, summary and
    status clipped, confidence clamped into the unit interval) so a hostile
    fault string cannot inflate the observation that outlives it."""
    mech = _coerce_enum(mechanism, Mechanism)
    clean_signals = tuple(str(signal)[:MAX_SIGNAL_CHARS] for signal in tuple(signals)[:MAX_SIGNALS])
    try:
        conf = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        conf = 0.0
    responsibility = _resolve(mech, clean_signals, conf)
    return FailureObservation(
        status=str(status or "")[:MAX_STATUS_CHARS],
        mechanism=mech,  # type: ignore[arg-type]
        responsibility=responsibility,  # type: ignore[arg-type]
        confidence=conf,
        signals=clean_signals,
        summary=str(summary or "")[:MAX_SUMMARY_CHARS],
    )


def _resolve(mech: Mechanism, signals: tuple[str, ...], conf: float) -> Responsibility:
    """Mechanism + evidence -> responsibility. Status never participates.

    The mapping is deliberately narrow: each branch demands a SPECIFIC signal
    before it attributes, and every branch that cannot decide returns UNKNOWN."""
    if mech is Mechanism.CANCELLATION:
        # A cancellation does not say who cancelled (human, budget, crash);
        # attributing it from the status alone is exactly the open failure
        # this taxonomy forbids.
        return Responsibility.UNKNOWN
    if mech is Mechanism.UNKNOWN:
        return Responsibility.UNKNOWN
    if mech is Mechanism.VALIDATION:
        # Authoring evidence dominates: a schema/shape mismatch in the spec
        # is the author's to fix, even if the provider also misbehaved.
        if SIGNAL_SPEC_SHAPE in signals:
            if conf >= AGENCY_CONFIDENCE_MIN:
                return Responsibility.AGENCY
            return Responsibility.UNKNOWN  # low confidence is not half-agency
        if SIGNAL_PROVIDER_SIDE in signals:
            return Responsibility.ENVIRONMENT
        return Responsibility.UNKNOWN
    if mech in (Mechanism.TRANSPORT, Mechanism.TIMEOUT):
        # Same mechanism, opposite owners — the discriminator is WHERE the
        # failure was observed, not the mechanism itself.
        if SIGNAL_HARNESS_INTERNAL in signals:
            return Responsibility.INFRASTRUCTURE
        if SIGNAL_PROVIDER_SIDE in signals:
            return Responsibility.ENVIRONMENT
        return Responsibility.UNKNOWN
    if mech is Mechanism.EXTERNAL_REJECTION:
        return (
            Responsibility.ENVIRONMENT
            if SIGNAL_PROVIDER_SIDE in signals
            else Responsibility.UNKNOWN
        )
    if mech is Mechanism.RESOURCE:
        return (
            Responsibility.INFRASTRUCTURE
            if SIGNAL_HARNESS_INTERNAL in signals
            else Responsibility.UNKNOWN
        )
    return Responsibility.UNKNOWN
