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


def test_empty_username_url_credentials_masked() -> None:
    pw = "Xq3vT9pLmZ2kR8wY" + "4nB6cD1eF5gH7jK0"
    for url in (f"redis://:{pw}@valkey:6379/0", f"connecting to rediss://:{pw}@cache.internal:6380"):
        out = redact_text(url)
        assert pw not in out
        assert REDACTED in out


def test_common_key_formats_masked() -> None:
    samples = [
        "AKIA" + "IOSFODNN7EXAMPLE",
        "sk_" + "live_" + "4eC39HqLyjWDarjtT1zdp7dc",
        "re_" + "123456789_" + "abcdefghijklmnopqrst",
        "eyJ"
        + "hbGciOiJIUzI1NiJ9"
        + ".eyJ"
        + "zdWIiOiIxMjM0NTY3ODkwIn0"
        + ".dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    ]
    for secret in samples:
        assert secret not in redact_text(f"value={secret} end"), secret
    dsn = "https://" + "0123456789abcdef" * 2 + "@o4500000.ingest.example.io/42"
    assert "0123456789abcdef" * 2 not in redact_text(dsn)


def test_token_counts_and_lookalike_params_are_not_redacted() -> None:
    usage = {"input_tokens": 1234, "output_tokens": 56, "max_tokens": 4096, "cache_read_input_tokens": 7}
    assert redact(usage) == usage
    assert redact({"access_token": "abc", "bot-token": "x", "token": "y"}) == {
        "access_token": REDACTED,
        "bot-token": REDACTED,
        "token": REDACTED,
    }
    assert redact_text("https://example.com/?monkey=banana&donkey=1") == "https://example.com/?monkey=banana&donkey=1"
    assert "s3cr3tvalue" not in redact_text("https://example.com/?x_api_key=s3cr3tvalue")


def test_key_names_are_matched_word_wise() -> None:
    for key in (
        "refreshToken",
        "accessToken",
        "idToken",
        "authToken",
        "X-Api-Key",
        "db_password",
        "set-cookie",
        "SENTRY_DSN",
        "AWS_SECRET_ACCESS_KEY",
        "client_secret",
        "session_id",
    ):
        assert is_sensitive_key(key), key
    for key in ("passenger", "compass", "secretary", "input_tokens", "tokens", "instrument", "keystone"):
        assert not is_sensitive_key(key), key


def test_free_text_credentials_masked() -> None:
    tg = "123456789:" + "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawQ"
    assert tg not in redact_text(f"POST https://api.telegram.org/bot{tg}/sendMessage")
    assert "dXNlcjpwYXNz" not in redact_text("Authorization: Basic dXNlcjpwYXNz")
    assert redact_text("Authorization: Basic dXNlcjpwYXNz").startswith("Authorization: Basic ")
    hdr_value = "abcdef" + "123456"
    assert hdr_value not in redact_text("x-api-key: " + hdr_value)
    aws = "wJalrXUtnFEMIK7MDENG" + "bPxRfiCYEXAMPLEKEY"
    assert aws not in redact_text(f"AWS_SECRET_ACCESS_KEY: {aws}")
    assert "hunter2xyz" not in redact_text('{"password": "hunter2xyz", "user": "a"}')
    assert '"user": "a"' in redact_text('{"password": "hunter2xyz", "user": "a"}')
    assert "abc/defghij" not in redact_text("redis://default:" + "abc/defghij" + "@host:6379")
    assert redact_text("see https://example.com:8080/path for docs") == "see https://example.com:8080/path for docs"


def test_round3_redaction_cases() -> None:
    pw = "p@ss@" + "word123"
    assert pw not in redact_text(f"postgresql://u:{pw}@host:5432/db more")
    assert "@ss@" not in redact_text(f"postgresql://u:{pw}@host:5432/db more")
    phrase = "multi word secret " + "pass phrase"
    assert "pass phrase" not in redact_text('{"password": "' + phrase + '", "user": "a"}')
    for text, secret in (
        ("PGPASSWORD=abc123xyz psql", "abc123xyz"),
        ("REDISCLI_AUTH=zz99yy valkey-cli", "zz99yy"),
        ("pass=hunter2", "hunter2"),
        ("token: abcdef", "abcdef"),
        ("Cookie: sessionid=abc123; csrftoken=x", "abc123"),
        ("Set-Cookie: sessionid=q1w2e3", "q1w2e3"),
    ):
        assert secret not in redact_text(text), text
    assert redact_text("contact a@b.com or https://x.io/a") == "contact a@b.com or https://x.io/a"
    assert redact_text("max_tokens=5 tokens: 7") == "max_tokens=5 tokens: 7"


def test_numbers_and_booleans_under_sensitive_keys_are_kept() -> None:
    data = {
        "token_count": 5,
        "pass_rate": 0.9,
        "compliance_pass": True,
        "password_reset_requested": False,
        "secret_count": 3,
        "auth": "Basic abc",
        "password": "x",
    }
    out = redact(data)
    assert out["token_count"] == 5
    assert out["pass_rate"] == 0.9
    assert out["compliance_pass"] is True
    assert out["password_reset_requested"] is False
    assert out["secret_count"] == 3
    assert out["auth"] == REDACTED
    assert out["password"] == REDACTED
