import shutil
from pathlib import Path

import pytest
import yaml

from rd_core.config import load_config
from rd_core.errors import ConfigError
from rd_core.settings import Settings

CONFIG_DIR = Settings().config_dir


def _copy(tmp_path: Path) -> Path:
    dst = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, dst)
    return dst


def _edit(path: Path, mutate: object) -> None:
    data = yaml.safe_load(path.read_text())
    assert callable(mutate)
    mutate(data)
    path.write_text(yaml.safe_dump(data))


def test_repository_config_is_valid() -> None:
    cfg = load_config(CONFIG_DIR)
    assert cfg.product.identity.working is True
    assert cfg.product.identity.primary_domain == "example.com"
    assert cfg.product.commercial.trial_days == 7
    assert cfg.product.commercial.min_gross_margin == pytest.approx(0.70)
    assert cfg.models.model_for_role("research_director").id == "claude-opus-5-5"
    assert cfg.models.model_for_role("verifier").id == "claude-sonnet-5-5"
    assert cfg.models.model_for_role("news_triage").id == "claude-haiku-5-5"


def test_flags_and_overrides() -> None:
    cfg = load_config(CONFIG_DIR)
    assert cfg.flag("provider_fred") is False
    assert cfg.flag("provider_fred", {"provider_fred": True}) is True
    with pytest.raises(ConfigError):
        cfg.flag("does_not_exist")
    with pytest.raises(ConfigError):
        cfg.models.model_for_role("nobody")


def test_jurisdiction_rules() -> None:
    rules = load_config(CONFIG_DIR).jurisdictions
    assert not rules.sales_allowed("IR")
    assert not rules.sales_allowed("kp")
    assert not rules.sales_allowed("UA", region="UA-43")
    assert rules.sales_allowed("UA", region="UA-30")
    assert not rules.sales_allowed("PK")  # not yet cleared
    assert rules.sales_allowed("DE")


def test_missing_file(tmp_path: Path) -> None:
    d = _copy(tmp_path)
    (d / "models.yaml").unlink()
    with pytest.raises(ConfigError, match=r"missing config file"):
        load_config(d)


def test_invalid_yaml(tmp_path: Path) -> None:
    d = _copy(tmp_path)
    (d / "product.yaml").write_text("identity: [unclosed")
    with pytest.raises(ConfigError, match=r"invalid YAML"):
        load_config(d)


def test_unknown_role_alias_rejected(tmp_path: Path) -> None:
    d = _copy(tmp_path)
    _edit(d / "models.yaml", lambda m: m["roles"].__setitem__("verifier", "gpt"))
    with pytest.raises(ConfigError, match=r"unknown model aliases"):
        load_config(d)


def test_duplicate_tier_rejected(tmp_path: Path) -> None:
    d = _copy(tmp_path)
    _edit(d / "product.yaml", lambda p: p["commercial"]["tiers"].append(p["commercial"]["tiers"][0]))
    with pytest.raises(ConfigError, match=r"tier ids must be unique"):
        load_config(d)


def test_unknown_key_rejected(tmp_path: Path) -> None:
    d = _copy(tmp_path)
    _edit(d / "product.yaml", lambda p: p["identity"].__setitem__("surprise", 1))
    with pytest.raises(ConfigError):
        load_config(d)


def test_same_fallback_provider_rejected(tmp_path: Path) -> None:
    d = _copy(tmp_path)
    _edit(d / "product.yaml", lambda p: p["commercial"].__setitem__("billing_fallback_provider", "paddle"))
    with pytest.raises(ConfigError, match=r"fallback billing provider"):
        load_config(d)


def test_unknown_flag_override_rejected() -> None:
    cfg = load_config(CONFIG_DIR)
    cfg.validate_flag_overrides({"publishing_enabled": False})
    with pytest.raises(ConfigError, match=r"publishing_enabeld"):
        cfg.validate_flag_overrides({"publishing_enabeld": False})
