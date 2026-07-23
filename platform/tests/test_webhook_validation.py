import pytest
from pydantic import ValidationError

from ticloud.api.schemas import JobCreate, JobUpdate, TenantUpdate
from ticloud.config import Settings
from ticloud.validation import validate_webhook_url


@pytest.mark.parametrize(
    "url",
    [
        "https://hooks.example/alert",
        "http://localhost:8080/webhook",
    ],
)
def test_validate_webhook_url_accepts_http_urls(url):
    assert validate_webhook_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "ftp://hooks.example/alert",
        "not-a-url",
        "https://bad host.example/alert",
        "https:///alert",
        "https://:443/alert",
        "https://@/alert",
    ],
)
def test_validate_webhook_url_rejects_invalid_or_empty_host(url):
    with pytest.raises(ValueError):
        validate_webhook_url(url)


def test_settings_rejects_webhook_url_with_empty_host(monkeypatch):
    monkeypatch.setenv("TICLOUD_WEBHOOK_URL", "https://:443/alert")

    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize(
    "schema_factory",
    [
        lambda webhook_url: JobCreate(name="qa-webhook", webhook_url=webhook_url),
        lambda webhook_url: JobUpdate(webhook_url=webhook_url),
        lambda webhook_url: TenantUpdate(webhook_url=webhook_url),
    ],
)
def test_api_webhook_schemas_reject_empty_host(schema_factory):
    with pytest.raises(ValidationError):
        schema_factory("https://:443/alert")
