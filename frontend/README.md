# Overhand frontend

Design **1b — Bold editorial** (Archivo + Instrument Serif, 2px rules, electric blue
`#2b32ff`), implemented as three plain static files. No build step, no dependencies.

```
frontend/
  index.html      upload / processing / result screens
  styles.css      the 1b design system
  app.js          state + all backend calls
  assets/sample.png   "Try a sample" page (copy of tests/images/setnotes.png)
```

## Running

The backend serves this folder, so there is one process and one origin:

```bash
uvicorn main:app --reload --port 8000
```

Then open <http://localhost:8000>.

One origin is deliberate: the session cookie is `HttpOnly` + `SameSite=Lax`, which a
frontend on a different port could not send. Serving the files from `main.py` keeps the
cookie same-origin and avoids `SameSite=None; Secure` (which Safari rejects on
`http://localhost`).

You can still split them — set `ALLOWED_ORIGINS` on the backend and point the frontend at
it with `?api=https://api.example.com` or `window.OVERHAND_API` before `app.js` loads — but
then the cookie needs `APP_ENV=prod` and https on both sides.

## Signing in

Every endpoint requires a signed-in user. The auth screen renders a Google Identity
Services button; the ID token it returns goes to `POST /auth/google`, which verifies it and
sets the session cookie. The client id is not baked into the HTML — `app.js` fetches it from
`GET /auth/config`, so only the backend's `.env` needs it. See `.env.example` in the repo
root for the variables, and add `http://localhost:8000` as an authorized JavaScript origin
on the OAuth client in Google Cloud Console.

The header chip shows the signed-in email and the pages remaining against
`FREE_PAGE_LIMIT`. Past the limit, `POST /upload-photo/` returns 402 and the remaining
photos in the batch are marked failed.

## What is wired to the backend

| UI | Endpoint |
| --- | --- |
| Auth screen, header chip, sign out | `GET /auth/config`, `POST /auth/google`, `GET /auth/me`, `POST /auth/logout` |
| Drop / choose / paste photos, "Try a sample" | `POST /upload-photo/` — one request per photo, sequential, with a per-file progress list |
| Document preview + page rail | `POST /compile` → PDF blob in an iframe |
| Clarification cards in the sidebar | `POST /resolve-questions` |
| Chat box and the three suggestion chips | `POST /edit-latex` (full conversation is replayed each time) |
| Download PDF + format menu (DOCX / Markdown / PNG) | `POST /export/{fmt}` |
| Copy TeX, LaTeX source | client-side, from the LaTeX currently held in state |

Multi-page uploads: the backend returns one complete LaTeX document per photo, so the
frontend stitches them into a single document — shared preamble, bodies joined with
`\newpage` (`mergeDocs` in `app.js`). Questions from every page are merged into one
card and prefixed with their page number.

`v{n} · undo` in the sidebar header tracks document versions locally; undo restores the
previous LaTeX and recompiles. Page count comes from the compiled PDF's page tree, so
no PDF library is needed.

## Not wired (no backend for it yet)

- **Method / Pricing / Students** — inert; clicking says so.
- **Share** in the design is implemented as **Copy TeX**, since there is no share or
  document-persistence endpoint. The header also has **New**, which clears local state.
- **"40s" / "98.2%"** on the landing page are the design's marketing copy, not measured
  values. The third stat tile is real — it reads the page count from `/auth/me`.
- The document title is editable but lives only in the browser.

## Notes

- The PDF preview uses the browser's built-in viewer with its toolbar hidden; page
  navigation goes through the thumbnail rail (`#page=N`). Chrome and Edge honour it;
  Safari ignores the fragment and shows page 1.
- Export formats other than PDF need `pandoc` (docx, md) or ImageMagick `convert` (png)
  on the backend host; without them the API returns 503 and the sidebar shows the error.
