import pytest
from pydantic import ValidationError

from ticloud.config import Settings


def test_auth_mode_rejects_invalid_value(monkeypatch):
    monkeypatch.setenv("TICLOUD_AUTH_MODE", "optional")

    with pytest.raises(ValidationError):
        Settings()


def test_auth_mode_normalizes_required_variants(monkeypatch):
    monkeypatch.setenv("TICLOUD_AUTH_MODE", "Required")
    assert Settings().auth_mode == "required"

    monkeypatch.setenv("TICLOUD_AUTH_MODE", " required ")
    assert Settings().auth_mode == "required"


def test_auth_mode_defaults_to_off(monkeypatch):
    monkeypatch.delenv("TICLOUD_AUTH_MODE", raising=False)

    assert Settings().auth_mode == "off"


def test_auth_mode_accepts_required(monkeypatch):
    monkeypatch.setenv("TICLOUD_AUTH_MODE", "required")

    assert Settings().auth_mode == "required"
