"""Typed loader for ``config/*.yaml``. Startup fails fast on any inconsistency (one config surface, PROMPT §2)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from rd_core.errors import ConfigError

ISO2 = r"^[A-Z]{2}$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Identity(_Strict):
    product_name: str
    product_short_name: str
    working: bool
    legal_entity_name: str
    country_of_incorporation: str = Field(pattern=ISO2)
    primary_domain: str
    support_email: str
    logo_path: str


class Fonts(_Strict):
    sans: str
    mono: str


class Brand(_Strict):
    fonts: Fonts
    tokens_file: str


class Audience(_Strict):
    target_users: list[str]


class ExcludedCoverage(_Strict):
    id: str
    description: str
    until: str


class Markets(_Strict):
    sales_countries: Literal["worldwide"] | list[str]
    jurisdiction_rules_file: str
    excluded_coverage: list[ExcludedCoverage]


class Defaults(_Strict):
    display_currency: Literal["USD"]
    server_timezone: Literal["UTC"]
    user_timezone_source: str


class DeepDiveCredits(_Strict):
    quick: int = Field(gt=0)
    standard: int = Field(gt=0)
    extended: int = Field(gt=0)


class Credits(_Strict):
    deep_dive: DeepDiveCredits
    ask_the_desk_answer: int = Field(gt=0)
    screener_run: int = Field(gt=0)


class Tier(_Strict):
    id: str
    name: str
    price_usd_month: float = Field(ge=0)
    price_usd_year: float = Field(ge=0)
    credits_month: int = Field(ge=0)
    seats: int = Field(ge=1)
    house_report_delay_hours: int = Field(ge=0)
    features: list[str]
    alert_rules: int = 0
    credits_pooled: bool = False
    priority_queue: bool = False


class SelfHostedLicence(_Strict):
    price_usd_once: float = Field(gt=0)
    updates_months: int = Field(gt=0)
    renewal_price_usd_year: float = Field(gt=0)
    offline_grace_days: int = Field(gt=0)


class Commercial(_Strict):
    trial_days: int = Field(gt=0)
    min_gross_margin: float = Field(gt=0, lt=1)
    billing_provider: Literal["paddle", "lemonsqueezy"]
    billing_fallback_provider: Literal["paddle", "lemonsqueezy"]
    pricing_status: Literal["proposed", "confirmed"]
    credits: Credits
    tiers: list[Tier]
    self_hosted_licence: SelfHostedLicence

    @model_validator(mode="after")
    def _unique_tiers(self) -> Commercial:
        ids = [t.id for t in self.tiers]
        if len(ids) != len(set(ids)):
            raise ValueError("tier ids must be unique")
        if self.billing_provider == self.billing_fallback_provider:
            raise ValueError("fallback billing provider must differ from the primary")
        return self


class ProductConfig(_Strict):
    schema_version: Literal[1]
    identity: Identity
    brand: Brand
    audience: Audience
    markets: Markets
    defaults: Defaults
    commercial: Commercial


class ModelSpec(_Strict):
    id: str
    price_input: float = Field(ge=0)
    price_output: float = Field(ge=0)
    price_cache_read: float | None = Field(default=None, ge=0)
    long_context_threshold_tokens: int | None = None
    price_input_long: float | None = None
    price_output_long: float | None = None


class ModelsConfig(_Strict):
    schema_version: Literal[1]
    provider: Literal["anthropic"]
    checked_on: str
    models: dict[str, ModelSpec]
    roles: dict[str, str]
    batch_discount: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def _roles_resolve(self) -> ModelsConfig:
        missing = sorted({alias for alias in self.roles.values() if alias not in self.models})
        if missing:
            raise ValueError(f"roles reference unknown model aliases: {missing}")
        return self

    def model_for_role(self, role: str) -> ModelSpec:
        try:
            return self.models[self.roles[role]]
        except KeyError as exc:
            raise ConfigError(f"no model configured for role {role!r}") from exc


class SanctionedSet(_Strict):
    countries: list[str]
    regions: list[str]
    source_note: str


class CountryRule(_Strict):
    sales: Literal["allowed", "blocked", "not_yet_cleared"]
    reason: str
    blocked_asset_classes: list[str] = Field(default_factory=list)
    required_wording: list[str] = Field(default_factory=list)


class JurisdictionRules(_Strict):
    schema_version: Literal[1]
    default: dict[str, str]
    sanctioned: SanctionedSet
    countries: dict[str, CountryRule]

    def sales_allowed(self, country_iso2: str, region: str | None = None) -> bool:
        code = country_iso2.upper()
        if code in self.sanctioned.countries or (region is not None and region in self.sanctioned.regions):
            return False
        rule = self.countries.get(code)
        return rule is None or rule.sales == "allowed"


class FeatureFlagsConfig(_Strict):
    schema_version: Literal[1]
    flags: dict[str, bool]


class AppConfig(_Strict):
    product: ProductConfig
    models: ModelsConfig
    jurisdictions: JurisdictionRules
    feature_flags: FeatureFlagsConfig

    def flag(self, name: str, overrides: dict[str, bool] | None = None) -> bool:
        if overrides and name in overrides:
            return overrides[name]
        if name not in self.feature_flags.flags:
            raise ConfigError(f"unknown feature flag {name!r}")
        return self.feature_flags.flags[name]


def _read_yaml(path: Path) -> object:
    if not path.is_file():
        raise ConfigError(f"missing config file: {path}")
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc


def load_config(config_dir: Path) -> AppConfig:
    """Load and validate every config file. Raises ``ConfigError`` with the offending file on failure."""
    sources = {
        "product": config_dir / "product.yaml",
        "models": config_dir / "models.yaml",
        "jurisdictions": config_dir / "jurisdiction_rules.yaml",
        "feature_flags": config_dir / "feature_flags.yaml",
    }
    raw = {name: _read_yaml(path) for name, path in sources.items()}
    try:
        return AppConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"configuration invalid:\n{exc}") from exc
