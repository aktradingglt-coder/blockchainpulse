"""Licence terms per provider and the licence gate (PROMPT §5.1).

Whether a value may be shown depends on the deployment mode: in the hosted edition *we* hold the vendor relationship;
in the self-hosted edition the buyer brings their own keys and accepts the vendor's terms (PROMPT §5.2).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from rd_core.settings import DeploymentMode
from rd_data.errors import LicenceBlocked


class LicenceTag(StrEnum):
    """What the current deployment mode may show. Display and derived rights are independent (PROMPT §5.1)."""

    DISPLAY_OK = "display_ok"  # raw values and analytics derived from them may be shown
    DISPLAY_ONLY = "display_only"  # raw values may be shown; no analytics may be derived from them
    DERIVED_ONLY = "derived_only"  # only computed analytics may be shown, never the raw values
    INTERNAL_ONLY = "internal_only"  # nothing from it may be shown

    @property
    def raw_display_allowed(self) -> bool:
        return self in (LicenceTag.DISPLAY_OK, LicenceTag.DISPLAY_ONLY)

    @property
    def derived_allowed(self) -> bool:
        return self in (LicenceTag.DISPLAY_OK, LicenceTag.DERIVED_ONLY)


class LicenceTerms(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    display_allowed: frozenset[DeploymentMode]
    derived_data_allowed: frozenset[DeploymentMode]
    redistribution_allowed: bool
    commercial_use_allowed: bool
    delay_minutes: int = Field(ge=0)
    attribution_text: str = Field(min_length=1)
    terms_verified: bool = Field(description="False until a human confirmed the terms (docs/vendor-notes)")

    def tag_for(self, mode: DeploymentMode) -> LicenceTag:
        if not self.commercial_use_allowed:
            return LicenceTag.INTERNAL_ONLY
        display, derived = mode in self.display_allowed, mode in self.derived_data_allowed
        if display and derived:
            return LicenceTag.DISPLAY_OK
        if display:
            return LicenceTag.DISPLAY_ONLY
        if derived:
            return LicenceTag.DERIVED_ONLY
        return LicenceTag.INTERNAL_ONLY


ALL_MODES = frozenset(DeploymentMode)
NO_MODES: frozenset[DeploymentMode] = frozenset()


def public_domain(attribution: str) -> LicenceTerms:
    """US federal government works: display, derived use and redistribution allowed in every mode."""
    return LicenceTerms(
        display_allowed=ALL_MODES,
        derived_data_allowed=ALL_MODES,
        redistribution_allowed=True,
        commercial_use_allowed=True,
        delay_minutes=0,
        attribution_text=attribution,
        terms_verified=True,
    )


def byo_key_only(attribution: str, *, delay_minutes: int = 0) -> LicenceTerms:
    """Commercial or unverified terms: shown only where the buyer holds the contract (self-hosted, BYO key).

    In the hosted edition nothing is displayed or derived until the owner records a contract that allows it.
    """
    return LicenceTerms(
        display_allowed=frozenset({DeploymentMode.SELF_HOSTED}),
        derived_data_allowed=frozenset({DeploymentMode.SELF_HOSTED}),
        redistribution_allowed=False,
        commercial_use_allowed=True,
        delay_minutes=delay_minutes,
        attribution_text=attribution,
        terms_verified=False,
    )


def ensure_displayable(tag: LicenceTag, *, provider: str) -> None:
    """Renderer-side gate for raw values: ``display_ok`` or ``display_only`` pass."""
    if not tag.raw_display_allowed:
        raise LicenceBlocked(f"raw values from {provider} may not be displayed ({tag.value})", provider=provider)


def ensure_derivable(tag: LicenceTag, *, provider: str) -> None:
    """Gate for analytics computed from a provider's values: ``display_ok`` or ``derived_only`` pass."""
    if not tag.derived_allowed:
        raise LicenceBlocked(f"analytics derived from {provider} may not be shown ({tag.value})", provider=provider)
