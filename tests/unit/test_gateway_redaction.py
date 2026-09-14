"""Redaction: credentials never reach a model (INV-5)."""

from __future__ import annotations

import pytest

from slas_llm_gateway.redaction import (
    DEFAULT_REDACTION,
    RedactionError,
    default_redactor,
    rules_from_mapping,
)

REDACTOR = default_redactor()

SECRETS = {
    "bearer_token": "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abcDEF123456",
    "basic_auth": "Authorization: Basic dXNlcjpwYXNzd29yZDEyMzQ1Ng==",
    "url_credentials": (
        "git clone https://deploy:glpat-abcdefghijklmnopqrstuv@gitlab.internal/fw.git"
    ),
    "github_token": "token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
    "gitlab_token": "PRIVATE-TOKEN glpat-ABCDEFGHIJKLMNOPQRSTUV",
    "aws_access_key": "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE",
    "ipmitool_password": (
        "ipmitool -I lanplus -H 10.0.0.5 -U admin -P Sup3rSecret! chassis power status"
    ),
    "password_assignment": "POSTGRES_PASSWORD=hunter2hunter2",
    "private_key_block": (
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW\n"
        "-----END OPENSSH PRIVATE KEY-----"
    ),
}


@pytest.mark.parametrize(("rule", "text"), sorted(SECRETS.items()))
def test_each_default_rule_catches_its_secret(rule: str, text: str) -> None:
    result = REDACTOR.redact(text)
    assert rule in result.hits, result
    assert f"[redacted:{rule}]" in result.text or "[redacted:" in result.text
    # None of the secret material survives.
    for fragment in (
        "eyJhbGci",
        "dXNlcjpw",
        "glpat-abc",
        "ghp_ABC",
        "glpat-ABC",
        "AKIAIOSF",
        "Sup3rSecret",
        "hunter2",
        "b3BlbnNzaC",
    ):
        assert fragment not in result.text, (rule, result.text)


def test_non_secret_context_is_kept() -> None:
    result = REDACTOR.redact(SECRETS["ipmitool_password"])
    assert result.text == (
        "ipmitool -I lanplus -H 10.0.0.5 -U admin -P [redacted:ipmitool_password] "
        "chassis power status"
    )
    url = REDACTOR.redact(SECRETS["url_credentials"])
    assert url.text == "git clone https://[redacted:url_credentials]@gitlab.internal/fw.git"
    assignment = REDACTOR.redact("password: 'p@ss-word-1234' and token=abcd1234efgh")
    assert assignment.text == (
        "password: [redacted:password_assignment] and token=[redacted:password_assignment]"
    )
    assert assignment.hits == {"password_assignment": 2}


def test_plain_text_is_untouched_and_says_so() -> None:
    text = "PCIe link lost on GPU3 (0000:8a:00.0) during DC cycle 14; LnkSta width x8 speed 16GT/s"
    result = REDACTOR.redact(text)
    assert result.text == text
    assert not result.redacted
    assert result.sentence() == "Nothing was redacted."


def test_sentence_counts_per_rule() -> None:
    result = REDACTOR.redact(SECRETS["bearer_token"] + "\n" + SECRETS["password_assignment"])
    assert result.sentence() == (
        "2 secrets redacted before the model saw the text (1 bearer_token, 1 password_assignment)."
    )


def test_rules_validate() -> None:
    with pytest.raises(RedactionError) as raised:
        rules_from_mapping({"version": 1, "rules": []}, source="x.yaml")
    assert raised.value.message.what_happened == "The redaction rules in x.yaml could not be used."
    with pytest.raises(RedactionError, match="could not be used"):
        rules_from_mapping(
            {"version": 1, "rules": [{"name": "bad", "description": "d", "pattern": "("}]}
        )
    with pytest.raises(RedactionError):
        rules_from_mapping(
            {
                "version": 1,
                "rules": [
                    {"name": "dup", "description": "d", "pattern": "a"},
                    {"name": "dup", "description": "d", "pattern": "b"},
                ],
            }
        )
    rules = rules_from_mapping(DEFAULT_REDACTION)
    assert rules.rules[0].name == "private_key_block", "multi-line blocks must run first"
