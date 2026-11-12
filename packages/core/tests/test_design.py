import pytest
import yaml

from rd_core.design import contrast_ratio, relative_luminance
from rd_core.settings import Settings


def test_contrast_reference_values() -> None:
    assert contrast_ratio("#000000", "#FFFFFF") == pytest.approx(21.0)
    assert contrast_ratio("#FFFFFF", "#FFFFFF") == pytest.approx(1.0)
    assert contrast_ratio("#767676", "#FFFFFF") == pytest.approx(4.54, abs=0.01)
    with pytest.raises(ValueError, match="RRGGBB"):
        relative_luminance("red")


def test_every_design_token_text_pair_meets_wcag_aa() -> None:
    tokens = yaml.safe_load((Settings().config_dir / "design_tokens.yaml").read_text())
    failures = []
    for theme, colours in tokens["themes"].items():
        for fg, bg in tokens["text_pairs"]:
            ratio = contrast_ratio(colours[fg], colours[bg])
            if ratio < 4.5:
                failures.append(f"{theme}: {fg} on {bg} = {ratio:.2f}")
    assert not failures, failures
