from rd_core.redaction import REDACTED, is_sensitive_key, redact, redact_text

# Secret-shaped values are assembled at runtime so this file never contains a literal that a scanner would flag.
ANTHROPIC = "sk-" + "ant-" + "api03-" + "A1b2C3d4E5f6G7h8I9j0"
GITHUB = "gh" + "p_" + "a" * 36
BEARER = "Bearer " + "x" * 40
TELEGRAM = "123456789:" + "A" * 35


def test_value_patterns_are_masked() -> None:
    for secret in (ANTHROPIC, GITHUB, TELEGRAM):
        out = redact_text(f"before {secret} after")
        assert secret not in out
        assert REDACTED in out
    assert "x" * 40 not in redact_text(f"Authorization: {BEARER}")


def test_url_credentials_and_query_params() -> None:
    dsn = "postgresql://rdesk:" + "hunter2pw" + "@db:5432/rdesk"
    assert "hunter2pw" not in redact_text(dsn)
    assert redact_text(dsn).startswith("postgresql://rdesk:")
    url = "https://api.eia.gov/v2/petroleum?api_key=" + "Z" * 32 + "&frequency=weekly"
    out = redact_text(url)
    assert "Z" * 32 not in out
    assert "frequency=weekly" in out


def test_discord_webhook_token_masked() -> None:
    url = "https://discord.com/api/webhooks/123456/" + "t" * 60
    assert "t" * 60 not in redact_text(url)


def test_private_key_block_masked() -> None:
    pem = "-----BEGIN PRIVATE KEY-----\nMIIBVgIBADANBgkq\n-----END PRIVATE KEY-----"
    assert "MIIBVgIBADANBgkq" not in redact_text(f"key={pem}")


def test_sensitive_keys_masked_recursively() -> None:
    payload = {
        "user": "alice",
        "api_key": "plain-value",
        "nested": {"Authorization": "abc", "list": [{"password": "p"}, "ok", ANTHROPIC]},
        "tuple": ("a", ANTHROPIC),
        "empty_token": "",
    }
    out = redact(payload)
    assert out["user"] == "alice"
    assert out["api_key"] == REDACTED
    assert out["nested"]["Authorization"] == REDACTED
    assert out["nested"]["list"][0]["password"] == REDACTED
    assert out["nested"]["list"][1] == "ok"
    assert ANTHROPIC not in str(out)
    assert isinstance(out["tuple"], tuple)
    assert out["empty_token"] == ""


def test_non_secret_text_untouched() -> None:
    text = "CL front-second spread as of 2026-10-20T18:30:00Z, evidence E17"
    assert redact_text(text) == text
    assert redact(42) == 42
    assert not is_sensitive_key("instrument")
    assert is_sensitive_key("X-Api-Key")


def test_depth_limit() -> None:
    deep: dict[str, object] = {}
    cur = deep
    for _ in range(40):
        nxt: dict[str, object] = {}
        cur["k"] = nxt
        cur = nxt
    assert REDACTED in str(redact(deep))
