import pytest
from pydantic import ValidationError

from ticloud.config import Settings


def test_auth_mode_rejects_invalid_value_at_construction():
    with pytest.raises(ValidationError) as exc:
        Settings(auth_mode="optional")

    message = str(exc.value)
    assert "auth_mode" in message
    assert "off" in message
    assert "required" in message


@pytest.mark.parametrize("auth_mode", ["off", "required"])
def test_auth_mode_accepts_supported_values_at_construction(auth_mode):
    settings = Settings(auth_mode=auth_mode)

    assert settings.auth_mode == auth_mode
