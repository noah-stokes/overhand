"""Sign-in, session cookies, and the page limit."""

import pytest
from fastapi.testclient import TestClient

import main
from auth import COOKIE_NAME, _serializer, issue_session, upsert_user
from main import app
from models import UsageEvent, User

client = TestClient(app)

MINIMAL_LATEX = r"\documentclass{article}\begin{document}Hi\end{document}"

# Every request the gated routes accept, with a body valid enough that a 401 can
# only be coming from the auth dependency.
GATED_CALLS = [
    ("/upload-photo/", {"files": {"file": ("p.png", b"not-really-a-png", "image/png")}}),
    ("/compile", {"content": MINIMAL_LATEX, "headers": {"Content-Type": "text/plain"}}),
    ("/export/pdf", {"content": MINIMAL_LATEX, "headers": {"Content-Type": "text/plain"}}),
    ("/edit-latex", {"json": {"latex": MINIMAL_LATEX, "messages": []}}),
    ("/resolve-questions", {"json": {"latex": MINIMAL_LATEX, "questions": [], "answers": []}}),
]


@pytest.mark.parametrize("path,kwargs", GATED_CALLS, ids=[c[0] for c in GATED_CALLS])
def test_gated_routes_reject_anonymous(anonymous, path, kwargs):
    assert client.post(path, **kwargs).status_code == 401


def test_auth_me_rejects_anonymous(anonymous):
    assert client.get("/auth/me").status_code == 401


def test_auth_config_is_public(anonymous):
    res = client.get("/auth/config")
    assert res.status_code == 200
    assert res.json()["google_client_id"]


def test_sign_in_creates_the_user_and_sets_an_httponly_cookie(monkeypatch, anonymous, db):
    """The Google token check is stubbed; the endpoint around it is what is under test."""
    monkeypatch.setattr(
        main,
        "verify_google_token",
        lambda credential: {
            "sub": "sub-new-user",
            "email": "fresh@example.com",
            "name": "Fresh",
            "picture": "https://example.com/a.png",
            "email_verified": True,
        },
    )

    res = client.post("/auth/google", json={"credential": "pretend-google-id-token"})

    assert res.status_code == 200
    assert res.json()["email"] == "fresh@example.com"
    assert db.query(User).filter_by(google_sub="sub-new-user").count() == 1

    set_cookie = res.headers["set-cookie"]
    assert set_cookie.startswith(f"{COOKIE_NAME}=")
    assert "HttpOnly" in set_cookie      # JS must never be able to read it
    assert "SameSite=lax" in set_cookie

    # And the cookie it handed back actually authenticates.
    assert client.get("/auth/me", cookies=res.cookies).status_code == 200


def test_sign_in_with_a_bad_token_is_rejected(anonymous, db):
    """No stub: the real verifier sees a token that is not a JWT at all."""
    before = db.query(User).count()
    res = client.post("/auth/google", json={"credential": "not-a-token"})
    assert res.status_code == 401
    assert "set-cookie" not in res.headers
    assert db.query(User).count() == before  # no row conjured from a bad token


def _session_cookie(user) -> str:
    """The cookie /auth/google would have set for this user."""

    class _Sink:
        def __init__(self):
            self.value = None

        def set_cookie(self, name, value, **kwargs):
            self.value = value

    sink = _Sink()
    issue_session(sink, user)
    return sink.value


def test_valid_cookie_authenticates(anonymous, user):
    res = client.get("/auth/me", cookies={COOKIE_NAME: _session_cookie(user)})
    assert res.status_code == 200
    body = res.json()
    assert body["email"] == user.email
    assert body["page_limit"] == main.FREE_PAGE_LIMIT


def test_tampered_cookie_is_rejected(anonymous, user):
    token = _session_cookie(user)
    assert client.get("/auth/me", cookies={COOKIE_NAME: token[:-4] + "AAAA"}).status_code == 401


def test_cookie_signed_with_another_secret_is_rejected(anonymous, user):
    from itsdangerous import URLSafeTimedSerializer

    forged = URLSafeTimedSerializer("some-other-secret", salt="overhand-session")
    token = forged.dumps({"uid": user.id, "sub": user.google_sub})
    assert client.get("/auth/me", cookies={COOKIE_NAME: token}).status_code == 401


def test_cookie_for_a_deleted_user_is_rejected(anonymous, user, db):
    token = _session_cookie(user)
    db.delete(user)
    db.commit()
    assert client.get("/auth/me", cookies={COOKIE_NAME: token}).status_code == 401


def test_cookie_subject_must_match_the_row(anonymous, user):
    """A recycled row id must not resurrect someone else's session."""
    token = _serializer.dumps({"uid": user.id, "sub": "a-different-google-subject"})
    assert client.get("/auth/me", cookies={COOKIE_NAME: token}).status_code == 401


def test_upsert_is_idempotent_and_refreshes_email(db):
    claims = {"sub": "sub-9", "email": "old@example.com", "name": "Old", "email_verified": True}
    first = upsert_user(db, claims)

    claims["email"] = "new@example.com"
    claims["name"] = "New"
    second = upsert_user(db, claims)

    assert first.id == second.id
    assert db.query(User).filter_by(google_sub="sub-9").count() == 1
    assert second.email == "new@example.com"
    assert second.name == "New"


def test_same_email_under_a_new_subject_relinks_the_row(db):
    """A recreated Google account keeps the email but gets a new sub — no 500, no
    duplicate row, and the page count survives."""
    original = upsert_user(
        db, {"sub": "sub-old", "email": "moved@example.com", "email_verified": True}
    )
    original.pages_used = 7
    db.commit()

    relinked = upsert_user(
        db, {"sub": "sub-new", "email": "moved@example.com", "email_verified": True}
    )

    assert relinked.id == original.id
    assert relinked.google_sub == "sub-new"
    assert relinked.pages_used == 7
    assert db.query(User).filter_by(email="moved@example.com").count() == 1


def test_upload_past_the_limit_returns_402(monkeypatch, user, db):
    monkeypatch.setattr(main, "FREE_PAGE_LIMIT", 3)
    user.pages_used = 3
    db.commit()

    res = client.post(
        "/upload-photo/", files={"file": ("p.png", b"not-really-a-png", "image/png")}
    )

    assert res.status_code == 402
    assert "3" in res.json()["detail"]
    # Refused before any work, so nothing was metered.
    assert db.query(UsageEvent).filter_by(user_id=user.id).count() == 0
    assert user.pages_used == 3


def test_successful_upload_meters_one_page(monkeypatch, user, db):
    """The OCR call is stubbed; what is under test is the metering around it."""
    monkeypatch.setattr(main, "preprocess_image", lambda raw: b"clean")
    monkeypatch.setattr(
        main, "ocr_to_latex", lambda clean, media_type: {"latex": MINIMAL_LATEX, "questions": []}
    )
    before = user.pages_used

    res = client.post(
        "/upload-photo/", files={"file": ("p.png", b"not-really-a-png", "image/png")}
    )

    assert res.status_code == 200
    db.refresh(user)
    assert user.pages_used == before + 1
    events = db.query(UsageEvent).filter_by(user_id=user.id).all()
    assert len(events) == 1
    assert events[0].pages == 1


def test_failed_upload_meters_nothing(monkeypatch, user, db):
    """A page that could not be read must not be charged."""
    monkeypatch.setattr(main, "preprocess_image", lambda raw: b"clean")

    def boom(clean, media_type):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(main, "ocr_to_latex", boom)
    before = user.pages_used

    with pytest.raises(RuntimeError):
        client.post("/upload-photo/", files={"file": ("p.png", b"x", "image/png")})

    db.refresh(user)
    assert user.pages_used == before
    assert db.query(UsageEvent).filter_by(user_id=user.id).count() == 0


def test_non_image_upload_is_rejected_before_metering(user, db):
    res = client.post("/upload-photo/", files={"file": ("notes.txt", b"hello", "text/plain")})
    assert res.status_code == 400
    db.refresh(user)
    assert user.pages_used == 0


def test_logout_clears_the_cookie(user):
    res = client.post("/auth/logout")
    assert res.status_code == 200
    assert res.cookies.get(COOKIE_NAME) in (None, "")
