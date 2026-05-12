"""
Integration tests against the live OpenAI API (gpt-5) and local pdflatex.

Covers the full pipeline: upload photo, edit LaTeX, resolve clarification questions,
and compile to PDF. Skipped automatically when OPENAI_API_KEY is not set.
"""

import json
import os
import pathlib

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

load_dotenv()

# Skip all tests if no API key is present
pytestmark = pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set — skipping live API tests",
)

from main import app

client = TestClient(app)

SAMPLE_IMAGE = pathlib.Path(__file__).parent / "tests" / "images" / "proof1.jpg"


# /upload-photo/ — photo → LaTeX

def test_upload_photo_returns_latex():
    """POST a real handwritten-math image; expect a non-empty LaTeX document."""
    with SAMPLE_IMAGE.open("rb") as f:
        resp = client.post(
            "/upload-photo/",
            files={"file": ("proof1.jpg", f, "image/jpeg")},
        )

    assert resp.status_code == 200, f"Unexpected status: {resp.status_code}\n{resp.text}"
    body = resp.json()
    assert "latex" in body, f"Response missing 'latex' key: {body}"

    latex = body["latex"]
    assert isinstance(latex, str) and len(latex) > 0, "latex is empty"
    assert "\\documentclass" in latex, "Response does not look like a LaTeX document"
    assert "\\begin{document}" in latex
    assert "\\end{document}" in latex

    assert "questions" in body, f"Response missing 'questions' key: {body}"
    questions = body["questions"]
    assert isinstance(questions, list)
    for q in questions:
        assert "confidence" in q and "location" in q and "question" in q
        assert 0.0 <= q["confidence"] < 0.70, (
            f"Question confidence {q['confidence']} should be below the 0.70 threshold"
        )


@pytest.fixture(scope="module")
def photo_latex():
    """Return the LaTeX produced from the sample photo (cached for this module)."""
    with SAMPLE_IMAGE.open("rb") as f:
        resp = client.post(
            "/upload-photo/",
            files={"file": ("proof1.jpg", f, "image/jpeg")},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert "latex" in body and "questions" in body
    return body["latex"]


# /edit-latex — LaTeX + natural-language prompt → edited LaTeX

def test_edit_latex_applies_instruction(photo_latex):
    """Ask the editor to add a title; verify the returned document changed."""
    payload = {
        "latex": photo_latex,
        "messages": [
            {"role": "user", "content": "Add a title 'Math Homework' at the top of the document."}
        ],
    }
    resp = client.post("/edit-latex", json=payload)

    assert resp.status_code == 200, f"Unexpected status: {resp.status_code}\n{resp.text}"
    body = resp.json()

    assert "latex" in body, f"Response missing 'latex' key: {body}"
    assert "message" in body, f"Response missing 'message' key: {body}"

    edited = body["latex"]
    assert isinstance(edited, str) and len(edited) > 0
    assert "\\documentclass" in edited
    assert "\\end{document}" in edited
    # The title instruction should have changed something
    assert edited != photo_latex, "Edit returned the original document unchanged"


def test_edit_latex_multi_turn(photo_latex):
    """Two-turn conversation: add a title, then revert it."""
    first_edit_payload = {
        "latex": photo_latex,
        "messages": [
            {"role": "user", "content": "Add a \\title{Test} and \\maketitle to the document."}
        ],
    }
    r1 = client.post("/edit-latex", json=first_edit_payload)
    assert r1.status_code == 200
    after_first_edit = r1.json()["latex"]
    first_message = r1.json()["message"]

    revert_payload = {
        "latex": after_first_edit,
        "messages": [
            {"role": "user", "content": "Add a \\title{Test} and \\maketitle to the document."},
            {"role": "assistant", "content": json.dumps({"latex": after_first_edit, "message": first_message})},
            {"role": "user", "content": "Revert that last change."},
        ],
    }
    r2 = client.post("/edit-latex", json=revert_payload)
    assert r2.status_code == 200
    body = r2.json()
    assert "latex" in body
    assert "message" in body


# /resolve-questions — apply clarification answers to update LaTeX

def test_resolve_questions_updates_latex(photo_latex):
    """Simulate the resolve flow: inject a fake question and answer, verify LaTeX updates."""
    fake_questions = [
        {"confidence": 0.45, "location": "first equation, right-hand side exponent", "question": "Is the exponent 2 or Z?"}
    ]
    fake_answers = ["It's the number 2"]

    resp = client.post(
        "/resolve-questions",
        json={"latex": photo_latex, "questions": fake_questions, "answers": fake_answers},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "latex" in body
    assert "questions" in body
    assert isinstance(body["questions"], list)
    # Strong preference: model should not have generated follow-up questions for a clear answer
    assert len(body["questions"]) == 0, f"Unexpected follow-up questions: {body['questions']}"
    assert "\\documentclass" in body["latex"]
    assert "\\end{document}" in body["latex"]


# Full pipeline: photo → LaTeX → edit → compile to PDF

def test_full_pipeline_photo_to_pdf(photo_latex):
    """
    Given LaTeX from a real photo:
      1. Edit it to ensure it is minimal/compilable
      2. Compile the edited LaTeX to PDF
      3. Confirm a non-empty PDF is returned
    """
    # Step A — make a small deterministic edit (add a comment) so we verify
    #           the edited document still compiles
    edit_resp = client.post(
        "/edit-latex",
        json={
            "latex": photo_latex,
            "messages": [
                {"role": "user", "content": "Add a comment '% integration test' on the line after \\begin{document}."}
            ],
        },
    )
    assert edit_resp.status_code == 200
    edited_latex = edit_resp.json()["latex"]

    # Step B — compile
    compile_resp = client.post(
        "/compile",
        content=edited_latex.encode(),
        headers={"Content-Type": "text/plain"},
    )
    assert compile_resp.status_code == 200, (
        f"pdflatex failed:\n{compile_resp.text[:2000]}"
    )
    assert compile_resp.headers["content-type"] == "application/pdf"
    assert len(compile_resp.content) > 1000, "PDF seems too small — likely empty"
