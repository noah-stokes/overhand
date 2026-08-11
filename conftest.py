"""
Shared test setup.

Two things every test needs now that the API is behind a login:

1. A database. Tests run against a throwaway SQLite file rather than Postgres, so
   the suite needs no server; the models use no Postgres-specific types.
2. A signed-in user. `signed_in` is autouse, so the suites written before auth
   existed keep testing what they were written to test. Tests that want to
   exercise the real dependency ask for the `anonymous` fixture.
"""

import os

import pytest

# cleaner.py reads this at import time; the tests below never call OpenAI.
os.environ.setdefault("OPENAI_API_KEY", "test-key-not-used")
os.environ.setdefault("SESSION_SECRET", "test-secret")
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id.apps.googleusercontent.com")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from auth import current_user
from db import Base, get_db
from main import app
from models import User


@pytest.fixture(scope="session")
def engine(tmp_path_factory):
    path = tmp_path_factory.mktemp("db") / "test.sqlite"
    eng = create_engine(f"sqlite:///{path}", future=True)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine):
    """A session on a connection wrapped in a transaction rolled back after each test."""
    connection = engine.connect()
    transaction = connection.begin()
    session = sessionmaker(bind=connection, autoflush=False, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture(autouse=True)
def override_db(db):
    app.dependency_overrides[get_db] = lambda: db
    yield
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def user(db):
    row = User(google_sub="sub-test-1", email="tester@example.com", name="Tester")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture(autouse=True)
def signed_in(user):
    app.dependency_overrides[current_user] = lambda: user
    yield user
    app.dependency_overrides.pop(current_user, None)


@pytest.fixture
def anonymous():
    """Drop the autouse sign-in so the real cookie dependency runs."""
    app.dependency_overrides.pop(current_user, None)
    yield
