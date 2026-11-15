"""Pure unit tests: ids, config guards, redaction."""

from __future__ import annotations

import base64
import json
import logging

import pytest
import structlog

from app.config import Settings
from app.ids import uuid7
from app.logging import configure_logging
from app.providers.errors import AuthError, RateLimitError
from app.security.redaction import REDACTED, redact, registered_secret

GEMINI_KEY = "AIzaSyD" + "x" * 32
OPENAI_KEY = "sk-proj-" + "a1B2" * 10
ANTHROPIC_KEY = "sk-ant-api03-" + "Z" * 40


# --------------------------------------------------------------------- ids


def test_uuid7_version_and_variant() -> None:
    u = uuid7()
    assert u.version == 7
    assert u.variant == "specified in RFC 4122"


def test_uuid7_monotonic_within_process() -> None:
    ids = [uuid7() for _ in range(5000)]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)


# --------------------------------------------------------------------- config


def _key() -> str:
    return base64.b64encode(b"k" * 32).decode()


def test_dev_login_forbidden_in_production() -> None:
    with pytest.raises(ValueError, match="DEV_LOGIN"):
        Settings(
            env="production",
            dev_login=True,
            session_secret="s" * 40,
            key_encryption_keys=json.dumps({"k1": _key()}),
            key_encryption_active_id="k1",
        )


def test_production_requires_encryption_keys() -> None:
    with pytest.raises(ValueError, match="KEY_ENCRYPTION_KEYS"):
        Settings(env="production", session_secret="s" * 40)


def test_encryption_keys_must_be_32_bytes() -> None:
    s = Settings(
        key_encryption_keys=json.dumps({"k1": base64.b64encode(b"short").decode()}),
        key_encryption_active_id="k1",
    )
    with pytest.raises(ValueError, match="32 bytes"):
        s.encryption_keys()


def test_active_key_id_must_exist() -> None:
    s = Settings(key_encryption_keys=json.dumps({"k1": _key()}), key_encryption_active_id="k2")
    with pytest.raises(ValueError, match="ACTIVE_ID"):
        s.encryption_keys()


def test_allowed_origins_from_env_csv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOWED_ORIGINS", "http://a.test,http://b.test")
    assert Settings().allowed_origins == ["http://a.test", "http://b.test"]


def test_allowed_origins_csv() -> None:
    s = Settings(allowed_origins="http://a.test, http://b.test")
    assert s.allowed_origins == ["http://a.test", "http://b.test"]


# --------------------------------------------------------------------- redaction


@pytest.mark.parametrize("key", [GEMINI_KEY, OPENAI_KEY, ANTHROPIC_KEY, "sk-or-v1-" + "f" * 40])
def test_redacts_key_shaped_strings(key: str) -> None:
    out = redact(f"failed calling provider with {key} oops")
    assert key not in out
    assert REDACTED in out


def test_redacts_query_param_and_headers() -> None:
    url = "https://generativelanguage.googleapis.com/v1beta/models/x:stream?alt=sse&key=weird-shape-123"
    assert "weird-shape-123" not in redact(url)
    assert "secret-tok" not in redact("Authorization: Bearer secret-tok")
    assert "abc123" not in redact("x-api-key: abc123")


def test_registered_literal_secret_is_scrubbed() -> None:
    odd = "totally-unusual-key-format-42"
    assert redact(f"echo {odd}") == f"echo {odd}"
    with registered_secret(odd):
        assert odd not in redact(f"echo {odd}")
    assert redact(f"echo {odd}") == f"echo {odd}"  # scope ends


def test_provider_errors_are_redacted_at_construction() -> None:
    odd = "my-custom-gateway-key-99"
    e = AuthError(f"401 for key {odd} and {GEMINI_KEY}", secret=odd)
    assert odd not in str(e)
    assert GEMINI_KEY not in str(e)
    r = RateLimitError("slow down", retry_after=2.5)
    assert r.retry_after == 2.5
    assert r.retryable


def test_structlog_and_stdlib_logs_are_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", json=True)
    structlog.get_logger("t").info("calling", detail=f"key={GEMINI_KEY}", nested={"k": OPENAI_KEY})
    logging.getLogger("thirdparty").warning("echo %s", ANTHROPIC_KEY)
    out = capsys.readouterr().out
    assert out  # something was logged
    for k in (GEMINI_KEY, OPENAI_KEY, ANTHROPIC_KEY):
        assert k not in out
