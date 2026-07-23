from urllib.parse import urlsplit


def validate_webhook_url(value: str | None) -> str | None:
    if value is None:
        return None
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or any(c.isspace() for c in value)
    ):
        raise ValueError("webhook_url must be an http(s) URL")
    return value
