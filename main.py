"""
FastAPI app for the proofy pipeline.

Endpoints:
- POST /upload-photo/       image file → LaTeX + clarification questions
- POST /edit-latex          LaTeX + conversation → edited LaTeX + confirmation message
- POST /resolve-questions   LaTeX + Q&A pairs → updated LaTeX
- POST /compile             LaTeX string → PDF bytes
"""

import os
import subprocess
import tempfile

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, File, UploadFile, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from processor import preprocess_image
from cleaner import ocr_to_latex, edit_latex, resolve_questions

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class Message(BaseModel):
    role: str
    content: str


class EditRequest(BaseModel):
    latex: str
    messages: list[Message]


class QuestionItem(BaseModel):
    confidence: float
    location: str
    question: str


class ResolveRequest(BaseModel):
    latex: str
    questions: list[QuestionItem]
    answers: list[str]

@app.post("/upload-photo/")
async def upload_photo(file: UploadFile = File(...)):
    if not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")

    raw_bytes = await file.read()
    clean_bytes = preprocess_image(raw_bytes)
    result = ocr_to_latex(clean_bytes, media_type=file.content_type)

    return JSONResponse(content=result)


def _run_pdflatex(latex: str, tmpdir: str) -> str:
    """Write latex to tmpdir, compile with pdflatex, return path to the PDF."""
    tex_path = os.path.join(tmpdir, "doc.tex")
    with open(tex_path, "w", encoding="utf-8") as f:
        f.write(latex)
    result = subprocess.run(
        ["pdflatex", "-interaction=nonstopmode", "-output-directory", tmpdir, tex_path],
        capture_output=True, timeout=30,
    )
    pdf_path = os.path.join(tmpdir, "doc.pdf")
    if not os.path.exists(pdf_path):
        log = result.stdout.decode(errors="replace") + result.stderr.decode(errors="replace")
        raise HTTPException(status_code=500, detail=f"pdflatex failed:\n{log}")
    return pdf_path


_EXPORT_FORMATS = {
    "pdf":  {"media_type": "application/pdf"},
    "docx": {"media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "pandoc_to": "docx"},
    "md":   {"media_type": "text/markdown", "pandoc_to": "markdown"},
    "png":  {"media_type": "image/png"},
}


@app.post("/compile")
async def compile_latex(latex: str = Body(..., media_type="text/plain")):
    with tempfile.TemporaryDirectory() as tmpdir:
        pdf_path = _run_pdflatex(latex, tmpdir)
        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()
    return Response(content=pdf_bytes, media_type="application/pdf")


@app.post("/export/{fmt}")
async def export_latex(fmt: str, latex: str = Body(..., media_type="text/plain")):
    if fmt not in _EXPORT_FORMATS:
        raise HTTPException(status_code=400, detail=f"Unsupported format '{fmt}'. Supported: {', '.join(_EXPORT_FORMATS)}")

    fmt_info = _EXPORT_FORMATS[fmt]
    with tempfile.TemporaryDirectory() as tmpdir:
        tex_path = os.path.join(tmpdir, "doc.tex")
        with open(tex_path, "w", encoding="utf-8") as f:
            f.write(latex)

        if fmt == "pdf":
            pdf_path = _run_pdflatex(latex, tmpdir)
            data = open(pdf_path, "rb").read()

        elif fmt == "png":
            pdf_path = _run_pdflatex(latex, tmpdir)
            png_path = os.path.join(tmpdir, "doc.png")
            try:
                r = subprocess.run(
                    ["convert", "-density", "150", pdf_path, png_path],
                    capture_output=True, timeout=30,
                )
            except FileNotFoundError:
                raise HTTPException(status_code=503, detail="ImageMagick is not installed")
            if not os.path.exists(png_path):
                raise HTTPException(status_code=500, detail="ImageMagick conversion failed:\n" + r.stderr.decode(errors="replace"))
            data = open(png_path, "rb").read()

        else:  # pandoc formats: docx, md
            ext = fmt_info["pandoc_to"] if fmt != "md" else "md"
            out_path = os.path.join(tmpdir, f"doc.{ext}")
            try:
                r = subprocess.run(
                    ["pandoc", tex_path, "--from=latex", f"--to={fmt_info['pandoc_to']}", f"--output={out_path}"],
                    capture_output=True, timeout=30,
                )
            except FileNotFoundError:
                raise HTTPException(status_code=503, detail="pandoc is not installed")
            if not os.path.exists(out_path):
                raise HTTPException(status_code=500, detail="pandoc failed:\n" + r.stderr.decode(errors="replace"))
            data = open(out_path, "rb").read()

    return Response(content=data, media_type=fmt_info["media_type"])


@app.post("/edit-latex")
async def edit_latex_endpoint(req: EditRequest):
    result = edit_latex(req.latex, [m.model_dump() for m in req.messages])
    return JSONResponse(content=result)


@app.post("/resolve-questions")
async def resolve_questions_endpoint(req: ResolveRequest):
    result = resolve_questions(
        req.latex,
        [q.model_dump() for q in req.questions],
        req.answers,
    )
    return JSONResponse(content=result)
