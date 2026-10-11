# Overhand

**Turn handwritten mathematics into editable LaTeX and downloadable documents.**

Overhand is a full-stack web app for students who want to digitize handwritten
notes without retyping equations. Upload photos, clarify ambiguous symbols, edit
the result in natural language, and export a PDF, Word document, Markdown file, or
PNG. The transcription prompt preserves the original work, including mathematical
mistakes and notation choices.

Built with **Python · FastAPI · GPT-5 vision · OpenCV · PostgreSQL · vanilla JavaScript**.

[Features](#features) · [Architecture](#architecture) · [Setup](#setup) · [Tests](#tests) · [API](#api)

## Features

- **Photo-to-LaTeX workflow:** upload, drag and drop, or paste handwritten pages;
  preview the compiled document alongside its source.
- **Multi-page documents:** process photos sequentially and merge their LaTeX into
  one document with a shared preamble and page breaks.
- **Clarification before export:** answer targeted questions about symbols the
  model reports as uncertain, then apply those answers to the transcription.
- **Conversational editing:** request changes in plain language, use follow-up
  instructions, and undo document versions in the browser.
- **Multiple export formats:** download PDF, DOCX, Markdown, or PNG, or copy the
  LaTeX source directly.
- **Accounts and usage tracking:** Google sign-in, signed session cookies, a
  configurable page allowance, and a database record for each successful upload.

## Architecture

```mermaid
flowchart TD
    A[Browser: upload handwritten pages] --> B[FastAPI: authenticate and check page allowance]
    B --> C[OpenCV + Pillow: preprocess image]
    C --> D[GPT-5 vision: LaTeX + clarification questions]
    D --> E[Browser: review, clarify, and edit]
    E --> F[FastAPI: resolve questions or apply conversational edits]
    F --> E
    E --> G[pdflatex: PDF preview and export]
    G --> H[ImageMagick: PNG]
    E --> I[Pandoc: DOCX or Markdown]
    D --> J[Database: user page count + usage event]
```

| Layer | Technology | Role |
| --- | --- | --- |
| Frontend | HTML, CSS, vanilla JavaScript | Upload flow, PDF preview, clarification cards, chat, and local undo |
| API | Python, FastAPI, Pydantic | Authenticated endpoints and document processing |
| Image processing | OpenCV, Pillow, NumPy | Grayscale conversion, A4 sizing, sharpening, denoising, deskewing, and Otsu binarization |
| Transcription and editing | OpenAI GPT-5 | Vision-based reconstruction and JSON responses for edits and clarifications |
| Persistence | PostgreSQL, SQLAlchemy, Alembic | User accounts, usage events, and versioned schema migrations |
| Authentication | Google Identity Services, google-auth, itsdangerous | Verified Google ID tokens and signed session cookies |
| Document output | pdflatex, Pandoc, ImageMagick | Compilation and format conversion |

### Engineering decisions

- **Preserve the student's work.** The reconstruction prompt distinguishes
  structural LaTeX repairs from mathematical corrections. It asks for clarification
  when the model estimates confidence below 70%; this is model-reported confidence,
  not a measured accuracy score.
- **Keep deployment simple.** FastAPI serves the static frontend from the same
  origin as the API. The UI has no build step or JavaScript package dependencies,
  and authenticated requests use an `HttpOnly`, `SameSite=Lax` cookie.
- **Make usage inspectable.** A successful transcription updates `users.pages_used`
  and inserts a `usage_events` row in one database commit. Failed transcriptions
  do not consume a page.
- **Retain editing context.** Each edit sends the current document and conversation
  history to the model, allowing follow-ups such as “revert that.” The frontend
  separately keeps document versions for undo.
- **Explore finer-grained OCR independently.** `detector.py` implements line
  segmentation and flags suspicious crops. It has dedicated tests; the current
  upload pipeline sends the entire preprocessed page to GPT-5.

## Requirements

| | |
| --- | --- |
| Python | 3.11+ (developed on 3.14) |
| Postgres | any recent version — or point `DATABASE_URL` at anything SQLAlchemy speaks |
| `pdflatex` | **required** for `/compile` and PDF/PNG export (TeX Live, MacTeX, MiKTeX) |
| `pandoc` | optional — DOCX and Markdown export; without it those routes return 503 |
| ImageMagick `convert` | optional — PNG export; without it that route returns 503 |
| Google OAuth client | required — document endpoints require Google sign-in |
| OpenAI API key | required — the OCR, edit and resolve calls all use `gpt-5` |

On macOS: `brew install --cask mactex-no-gui` and `brew install pandoc imagemagick`.

---

## Setup

```bash
git clone https://github.com/noah-stokes/overhand.git
cd overhand
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env               # then fill it in — see below
```

### Environment

`.env.example` documents every variable. The ones you must set:

- `OPENAI_API_KEY` — the pipeline calls `gpt-5` for OCR, edits and question resolution.
- `DATABASE_URL` — SQLAlchemy URL, e.g.
  `postgresql+psycopg://postgres:overhand@localhost:5432/postgres`.
- `GOOGLE_CLIENT_ID` — an OAuth 2.0 **Web** client from the
  [Google Cloud console](https://console.cloud.google.com/apis/credentials), with
  `http://localhost:8000` as an authorized JavaScript origin.
- `SESSION_SECRET` — signs the session cookie. Generate one with
  `python -c "import secrets; print(secrets.token_urlsafe(32))"`.

The rest have working defaults: `APP_ENV=dev` (drops the `Secure` flag so the cookie
works over plain `http://localhost`), `FREE_PAGE_LIMIT=100`, and `ALLOWED_ORIGINS`
(only needed if you serve the frontend from a different origin).

`auth.py` refuses to start with the built-in development secret whenever `APP_ENV`
is anything other than `dev` — a known secret would let anyone mint a session for
any account.

### Database

Alembic owns the schema. `alembic.ini` deliberately has no `sqlalchemy.url`;
`migrations/env.py` reads `DATABASE_URL` from the environment so there is one source
of truth shared with `db.py`.

```bash
alembic upgrade head
```

---

## Running

```bash
uvicorn main:app --reload --port 8000
```

Open <http://localhost:8000>. One process, one origin: `main.py` mounts `frontend/`
at `/`, which keeps the `HttpOnly` + `SameSite=Lax` session cookie same-origin. See
[`frontend/README.md`](frontend/README.md) for the UI, what is wired to which
endpoint, and how to split the two apart if you want to.

---

## API

Document endpoints require a session cookie and return HTTP 401 without one.
`/auth/config`, `/auth/google`, and `/auth/logout` are public; `/auth/me` requires
an authenticated session.

| Method | Route | Body → Response |
| --- | --- | --- |
| `GET` | `/auth/config` | → the public Google client id, so the static frontend needs no build step |
| `POST` | `/auth/google` | Google ID token → session cookie + user |
| `GET` | `/auth/me` | → the signed-in user, or 401 |
| `POST` | `/auth/logout` | → clears the session cookie |
| `POST` | `/upload-photo/` | image file → `{latex, questions[]}`; 402 once `FREE_PAGE_LIMIT` is reached |
| `POST` | `/resolve-questions` | LaTeX + Q&A pairs → updated LaTeX + questions |
| `POST` | `/edit-latex` | LaTeX + conversation → edited LaTeX + a confirmation message |
| `POST` | `/compile` | LaTeX (`text/plain`) → PDF bytes |
| `POST` | `/export/{fmt}` | LaTeX (`text/plain`) → `pdf` · `docx` · `md` · `png` |

Each `questions[]` entry is `{confidence, location, question}`. The prompt asks for
questions about regions the model estimates at under 70% confidence.

---

## Layout

```
main.py         FastAPI app: auth routes, pipeline routes, static mount
auth.py         Google ID-token verification, signed session cookies, current_user
db.py           engine + per-request session dependency
models.py       users, usage_events
processor.py    photo → clean binarized PNG
cleaner.py      the three GPT-5 calls, and the prompts that shape them
detector.py     standalone line segmentation (see below)
migrations/     alembic; 0001 creates users + usage_events
frontend/       index.html, styles.css, app.js — no build step
tests/          auth, processor, detector
test_compile.py       /compile and /export against local pdflatex
test_integration.py   full pipeline against the live OpenAI API
```

`detector.py` splits a preprocessed page into one crop per line — ink blobs are
clustered into lines, tall symbols like integrals are shared across the lines they
span, orphan dots are absorbed into their nearest line, and each crop comes back
flagged if it looks suspicious (`TALL`, `NARROW`, `LOW_INK`, `SINGLE_COMP`). It is
covered by its own tests but is **not** currently in the request path: `ocr_to_latex`
sends the whole page to the model in one call. It is there for a per-line OCR
strategy.

---

## Tests

```bash
pytest tests/                    # authentication, usage tracking, preprocessing, segmentation
pytest test_compile.py           # compilation and export API tests; requires pdflatex
pytest test_integration.py       # live GPT-5 calls; requires a real OPENAI_API_KEY
```

`conftest.py` gives every test a throwaway SQLite database and an autouse signed-in
user, so suites written before authentication existed still test what they were
written to test; a test that wants the real cookie dependency asks for the
`anonymous` fixture. `test_compile.py` needs `pdflatex` on `PATH` and fails without
it. DOCX/Markdown and PNG export tests skip when their conversion tool is missing.
The live integration suite uses `tests/images/proof1.jpg`, makes billable API calls,
and requires `pdflatex` for the complete photo-to-PDF test.

## Current scope

User accounts and usage records persist in the database. Documents, titles,
conversation history, and undo versions live in browser memory; saved documents
and shareable document links are not implemented. The landing page's speed and
accuracy figures are illustrative copy, not benchmark results.

---

## License

[Mozilla Public License 2.0](LICENSE).
