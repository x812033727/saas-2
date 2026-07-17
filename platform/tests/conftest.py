import os
import tempfile

# Point the platform at a throwaway SQLite DB before any ticloud import.
_tmpdir = tempfile.mkdtemp(prefix="ticloud-test-")
os.environ["TICLOUD_DATABASE_URL"] = f"sqlite:///{_tmpdir}/test.db"
os.environ["TICLOUD_TICK_INTERVAL"] = "0.1"
os.environ["TICLOUD_POLL_INTERVAL"] = "0.1"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from ticloud.api.main import app  # noqa: E402
from ticloud.db import engine, get_session  # noqa: E402
from ticloud.models import Base  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


@pytest.fixture
def session():
    s = get_session()
    yield s
    s.close()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c
