# Overhand

Photograph a page of handwritten mathematics, get back a clean, compilable LaTeX
document — and a PDF, DOCX, Markdown or PNG export of it.

Overhand is a FastAPI backend plus a dependency-free static frontend. A page goes
through image preprocessing, a GPT-5 vision pass that reconstructs it as LaTeX, an
optional round of clarification questions for anything the model read with low
confidence, and finally `pdflatex`. The transcription is deliberately faithful: the
model is instructed to reproduce the student's work exactly, mistakes and notation
quirks included, never to correct or improve the maths.

---

## How a page moves through the system

```
photo ──▶ processor.preprocess_image ──▶ cleaner.ocr_to_latex ──▶ LaTeX + questions
             greyscale, A4 @150dpi,          GPT-5 vision,             │
             sharpen, denoise, deskew,       JSON out                  │
             Otsu binarize                                             ▼
                                                       cleaner.resolve_questions
                                                       (user answers patch the LaTeX)
                                                                       │
                                                                       ▼
                                              pdflatex ──▶ PDF ──▶ pandoc / ImageMagick
                                                                  ──▶ DOCX · MD · PNG
```

Editing is conversational: `POST /edit-latex` replays the whole chat history against
the current document, so follow-ups like "revert that" work.

Every page that is successfully read increments `users.pages_used` and writes a
`usage_events` row, so the free-tier count is auditable rather than just a counter.

---

## Requirements

| | |
| --- | --- |
| Python | 3.11+ (developed on 3.14) |
| Postgres | any recent version — or point `DATABASE_URL` at anything SQLAlchemy speaks |
| `pdflatex` | **required** for `/compile` and PDF/PNG export (TeX Live, MacTeX, MiKTeX) |
| `pandoc` | optional — DOCX and Markdown export; without it those routes return 503 |
| ImageMagick `convert` | optional — PNG export; without it that route returns 503 |
| Google OAuth client | required — every endpoint is behind Google sign-in |
| OpenAI API key | required — the OCR, edit and resolve calls all use `gpt-5` |

On macOS: `brew install --cask mactex-no-gui` and `brew install pandoc imagemagick`.

---

## Setup

```bash
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

The four `/auth` routes below are how you get a session; every other route
requires the cookie and returns 401 without it.

| Method | Route | Body → Response |
| --- | --- | --- |
| `GET` | `/auth/config` | → the public Google client id, so the static frontend needs no build step |
| `POST` | `/auth/google` | Google ID token → session cookie + user |
| `GET` | `/auth/me` | → the signed-in user, or 401 |
| `POST` | `/auth/logout` | → clears the session cookie |
| `POST` | `/upload-photo/` | image file → `{latex, questions[]}`; 402 once `FREE_PAGE_LIMIT` is reached |
| `POST` | `/resolve-questions` | LaTeX + Q&A pairs → updated LaTeX |
| `POST` | `/edit-latex` | LaTeX + conversation → edited LaTeX + a confirmation message |
| `POST` | `/compile` | LaTeX (`text/plain`) → PDF bytes |
| `POST` | `/export/{fmt}` | LaTeX (`text/plain`) → `pdf` · `docx` · `md` · `png` |

Each `questions[]` entry is `{confidence, location, question}` — the model only asks
about regions it read at under 70% confidence.

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
pytest tests/ test_compile.py     # unit + API tests
pytest test_integration.py        # live OpenAI calls, skipped without OPENAI_API_KEY
```

`conftest.py` gives every test a throwaway SQLite database and an autouse signed-in
user, so suites written before authentication existed still test what they were
written to test; a test that wants the real cookie dependency asks for the
`anonymous` fixture. `test_compile.py` needs `pdflatex` on `PATH` and fails without
it — the rest of the suite has no external dependencies.

---

## License

[Mozilla Public License 2.0](LICENSE).
