import pytest
from pydantic import ValidationError

from ticloud.config import Settings


def test_auth_mode_rejects_illegal_value():
    with pytest.raises(ValidationError):
        Settings(auth_mode="definitely-not-valid")


def test_auth_mode_normalizes_case_and_whitespace():
    assert Settings(auth_mode=" Required ").auth_mode == "required"


def test_auth_mode_defaults_to_off():
    assert Settings().auth_mode == "off"


def test_auth_mode_accepts_required():
    assert Settings(auth_mode="required").auth_mode == "required"
