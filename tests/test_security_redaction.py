from shared.redaction import REDACTED, redact_sensitive, redact_text


def test_recursive_redaction_masks_sensitive_keys() -> None:
    payload = {
        "account": "visible",
        "password": "example-password",
        "nested": {
            "Authorization": "Bearer example-token-value",
            "items": [{"cookie": "session=value"}],
        },
    }

    redacted = redact_sensitive(payload)

    assert redacted["account"] == "visible"
    assert redacted["password"] == REDACTED
    assert redacted["nested"]["Authorization"] == REDACTED
    assert redacted["nested"]["items"][0]["cookie"] == REDACTED


def test_text_redaction_masks_headers_assignments_and_query_values() -> None:
    text = (
        "Authorization: Bearer example-token password=example-password "
        "https://example.invalid/?access_token=example-token&safe=yes"
    )

    redacted = redact_text(text)

    assert "example-token" not in redacted
    assert "example-password" not in redacted
    assert redacted.count(REDACTED) >= 3


def test_bytes_are_never_decoded_into_logs() -> None:
    assert redact_sensitive({"payload": b"raw-body"}) == {"payload": "[REDACTED BYTES]"}


def test_authentication_keys_and_full_cookie_headers_are_masked() -> None:
    text = (
        'authenticationKey="private-value"\n'
        "Cookie: session=private-session; secondary=also-private\n"
        "safe=visible"
    )

    redacted = redact_text(text)

    assert "private-value" not in redacted
    assert "private-session" not in redacted
    assert "also-private" not in redacted
    assert "safe=visible" in redacted


def test_camel_case_tokens_and_raw_state_assignments_are_masked() -> None:
    text = (
        "{'tokenValue': 'private-token', 'accessToken': 'private-access', "
        "'storageState': 'private-state', 'requestBody': 'private-body'}"
    )

    redacted = redact_text(text)

    for secret in ("private-token", "private-access", "private-state", "private-body"):
        assert secret not in redacted
    assert redacted.count(REDACTED) == 4
