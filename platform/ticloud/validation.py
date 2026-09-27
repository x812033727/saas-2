from urllib.parse import urlsplit


def validate_http_url(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    parsed = urlsplit(value)
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an http(s) URL") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or any(c.isspace() for c in value)
    ):
        raise ValueError(f"{field_name} must be an http(s) URL")
    return value


def validate_webhook_url(value: str | None) -> str | None:
    return validate_http_url(value, "webhook_url")
