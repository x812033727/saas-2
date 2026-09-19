from urllib.parse import urlsplit


def validate_webhook_url(value: str | None) -> str | None:
    if value is None:
        return None
    parsed = urlsplit(value)
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("webhook_url must be an http(s) URL") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or any(c.isspace() for c in value)
    ):
        raise ValueError("webhook_url must be an http(s) URL")
    return value
