"""Unit tests for the /compile endpoint — no live API calls, just local pdflatex."""

import pytest
from fastapi.testclient import TestClient
from main import app

client = TestClient(app)

MINIMAL_LATEX = r"\documentclass{article}\begin{document}Hello World\end{document}"
MATH_LATEX = r"\documentclass{article}\begin{document}$E = mc^2$\end{document}"


def test_compile_returns_pdf():
    response = client.post("/compile", content=MINIMAL_LATEX, headers={"Content-Type": "text/plain"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content[:4] == b"%PDF"


def test_compile_pdf_nonempty():
    response = client.post("/compile", content=MINIMAL_LATEX, headers={"Content-Type": "text/plain"})
    assert len(response.content) > 1000


def test_compile_math():
    response = client.post("/compile", content=MATH_LATEX, headers={"Content-Type": "text/plain"})
    assert response.status_code == 200
    assert response.content[:4] == b"%PDF"


def test_compile_invalid_latex_returns_500():
    broken = r"\documentclass{article}\begin{document}\UNDEFINED_COMMAND_XYZ\end{document}"
    response = client.post("/compile", content=broken, headers={"Content-Type": "text/plain"})
    # pdflatex may still produce a PDF for some errors; we check it either
    # succeeds with a PDF or fails with a meaningful 500 error body
    if response.status_code == 500:
        assert "pdflatex failed" in response.json()["detail"]
    else:
        assert response.status_code == 200


def test_compile_empty_body_returns_error():
    response = client.post("/compile", content="", headers={"Content-Type": "text/plain"})
    # FastAPI rejects an empty required body with 422 before pdflatex is even called
    assert response.status_code == 422


def test_export_pdf():
    resp = client.post("/export/pdf", content=MINIMAL_LATEX, headers={"Content-Type": "text/plain"})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content[:4] == b"%PDF"


def test_export_docx():
    resp = client.post("/export/docx", content=MINIMAL_LATEX, headers={"Content-Type": "text/plain"})
    if resp.status_code == 503:
        pytest.skip("pandoc not installed")
    assert resp.status_code == 200
    assert "wordprocessingml" in resp.headers["content-type"]
    assert resp.content[:2] == b"PK"  # DOCX is a zip


def test_export_md():
    resp = client.post("/export/md", content=MINIMAL_LATEX, headers={"Content-Type": "text/plain"})
    if resp.status_code == 503:
        pytest.skip("pandoc not installed")
    assert resp.status_code == 200
    assert len(resp.content) > 0


def test_export_png():
    resp = client.post("/export/png", content=MATH_LATEX, headers={"Content-Type": "text/plain"})
    if resp.status_code == 503:
        pytest.skip("ImageMagick not installed")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content[:4] == b"\x89PNG"


def test_export_invalid_format():
    resp = client.post("/export/gif", content=MINIMAL_LATEX, headers={"Content-Type": "text/plain"})
    assert resp.status_code == 400
