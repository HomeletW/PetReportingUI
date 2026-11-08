import os
import shutil

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from petreport.config import settings
from petreport import db
from petreport.main import app


@pytest.fixture
def client(monkeypatch, tmp_path):
    use_database = os.getenv("TEST_DATABASE") == "1"
    if not use_database:
        config = tmp_path / "config.yaml"
        config.write_text(
            f"internal_api_token: this-is-an-obviously-fake-test-token-only\nmedia_root: {tmp_path / 'media'}\n"
        )
        monkeypatch.setenv("PETREPORT_CONFIG", str(config))
        monkeypatch.setenv("PETREPORT_SECRETS", "/nonexistent")
    settings.cache_clear()
    if use_database:
        # Dedicated ephemeral test database only. Never point this at a deployed DB.
        assert db.engine().url.database == "petreport_test"
        db.metadata.drop_all(db.engine())
        from alembic.config import Config
        from alembic import command

        with db.engine().begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
        command.upgrade(Config("alembic.ini"), "head")
        shutil.rmtree(settings().media_root, ignore_errors=True)
    with TestClient(app) as c:
        c.headers["authorization"] = "Bearer this-is-an-obviously-fake-test-token-only"
        yield c


@pytest.fixture
def pgclient(client):
    if os.getenv("TEST_DATABASE") != "1":
        pytest.skip(
            "Requires isolated PostgreSQL: docker compose -f compose.test.yaml up --build --abort-on-container-exit"
        )
    return client


@pytest.fixture
def anyio_backend():
    return "asyncio"
