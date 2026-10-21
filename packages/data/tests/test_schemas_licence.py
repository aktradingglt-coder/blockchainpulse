from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from rd_core.settings import DeploymentMode
from rd_data.errors import LicenceBlocked
from rd_data.licence import (
    LicenceTag,
    LicenceTerms,
    byo_key_only,
    ensure_derivable,
    ensure_displayable,
    public_domain,
)
from rd_data.schemas import Bar, PositionCounts, PositioningReport, record_time


def test_records_are_utc_and_frozen() -> None:
    pkt = timezone(timedelta(hours=5))
    bar = Bar(symbol="X", interval="1d", ts=datetime(2026, 10, 20, 5, tzinfo=pkt), open=1, high=2, low=0.5, close=1.5)
    assert bar.ts == datetime(2026, 10, 20, 0, tzinfo=UTC)
    with pytest.raises(ValidationError):
        bar.close = 9  # type: ignore[misc]  # frozen model: assignment must fail
    with pytest.raises(ValidationError):
        Bar(symbol="X", interval="1d", ts=datetime(2026, 10, 20), open=1, high=2, low=0.5, close=1.5)  # noqa: DTZ001
    with pytest.raises(ValidationError):
        Bar(symbol="X", interval="2d", ts=datetime(2026, 10, 20, tzinfo=UTC), open=1, high=2, low=0.5, close=1.5)


def test_positioning_and_record_time() -> None:
    r = PositioningReport(
        market_code="099741",
        market_name="EURO FX",
        report_type="tff",
        futures_only=True,
        as_of=date(2026, 10, 13),
        open_interest=100,
        categories={"leveraged_funds": PositionCounts(long=60, short=20)},
    )
    assert r.categories["leveraged_funds"].net == 40
    assert record_time(r) == date(2026, 10, 13)


@pytest.mark.parametrize(
    ("terms", "hosted", "self_hosted"),
    [
        (public_domain("Source: CFTC"), LicenceTag.DISPLAY_OK, LicenceTag.DISPLAY_OK),
        (byo_key_only("Data: vendor"), LicenceTag.INTERNAL_ONLY, LicenceTag.DISPLAY_OK),
    ],
)
def test_licence_tags_by_deployment_mode(terms: LicenceTerms, hosted: LicenceTag, self_hosted: LicenceTag) -> None:
    assert terms.tag_for(DeploymentMode.HOSTED) is hosted
    assert terms.tag_for(DeploymentMode.SELF_HOSTED) is self_hosted


def test_derived_only_and_non_commercial() -> None:
    derived = LicenceTerms(
        display_allowed=frozenset(),
        derived_data_allowed=frozenset({DeploymentMode.HOSTED}),
        redistribution_allowed=False,
        commercial_use_allowed=True,
        delay_minutes=15,
        attribution_text="x",
        terms_verified=True,
    )
    assert derived.tag_for(DeploymentMode.HOSTED) is LicenceTag.DERIVED_ONLY
    personal = derived.model_copy(update={"commercial_use_allowed": False})
    assert personal.tag_for(DeploymentMode.HOSTED) is LicenceTag.INTERNAL_ONLY


def test_licence_gate_blocks_restricted_values() -> None:
    """PROMPT §5.1 / §17: no restricted provider value can render in the hosted edition."""
    tag = byo_key_only("Data: vendor").tag_for(DeploymentMode.HOSTED)
    with pytest.raises(LicenceBlocked):
        ensure_displayable(tag, provider="vendor")
    with pytest.raises(LicenceBlocked):
        ensure_derivable(tag, provider="vendor")
    ensure_derivable(LicenceTag.DERIVED_ONLY, provider="v")
    with pytest.raises(LicenceBlocked):
        ensure_displayable(LicenceTag.DERIVED_ONLY, provider="v")
    ensure_displayable(LicenceTag.DISPLAY_OK, provider="cftc")
