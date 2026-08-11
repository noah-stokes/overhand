/* Overhand frontend — design 1b.
 *
 * Talks to the FastAPI backend in main.py:
 *   GET  /auth/config        -> { google_client_id }
 *   POST /auth/google        { credential }         -> user, sets the session cookie
 *   GET  /auth/me            -> user | 401
 *   POST /auth/logout        -> clears the session cookie
 *   POST /upload-photo/      multipart image        -> { latex, questions[] }
 *   POST /edit-latex         { latex, messages[] }  -> { latex, message }
 *   POST /resolve-questions  { latex, questions[], answers[] } -> { latex, questions[] }
 *   POST /compile            text/plain latex       -> application/pdf
 *   POST /export/{fmt}       text/plain latex       -> pdf | docx | md | png
 *
 * Everything but /auth/config and /auth/google needs the session cookie, which is
 * HttpOnly — JS never sees it, it just rides along on every request.
 *
 * Override the backend origin with  window.OVERHAND_API = 'http://host:port'
 * (or ?api=... in the query string) before this script runs.
 */

const API = (() => {
  const q = new URLSearchParams(location.search).get('api');
  if (q) return q.replace(/\/$/, '');
  if (window.OVERHAND_API) return String(window.OVERHAND_API).replace(/\/$/, '');
  // Served from the same origin as the API? Use it. Otherwise assume local uvicorn.
  if (location.protocol.startsWith('http') && location.port === '8000') return location.origin;
  return 'http://localhost:8000';
})();

const MAX_PAGES = 40;

const $ = (id) => document.getElementById(id);

const el = {
  screens: {
    auth: $('screen-auth'),
    upload: $('screen-upload'),
    processing: $('screen-processing'),
    result: $('screen-result'),
  },

  gSignin: $('g-signin'),
  authNote: $('auth-note'),
  userChip: $('user-chip'),
  userAvatar: $('user-avatar'),
  userEmail: $('user-email'),
  userUsage: $('user-usage'),
  signOut: $('sign-out'),
  statQuota: $('stat-quota'),
  statQuotaLabel: $('stat-quota-label'),

  fileInput: $('file-input'),
  dropzone: $('dropzone'),
  dropVeil: $('drop-veil'),
  toasts: $('toast-stack'),

  choosePhotos: $('choose-photos'),
  trySample: $('try-sample'),

  processingTitle: $('processing-title'),
  jobList: $('job-list'),
  cancelProcessing: $('cancel-processing'),

  docTitle: $('doc-title'),
  ppBadge: $('pp-badge'),
  newDoc: $('new-doc'),
  copyTex: $('copy-tex'),
  downloadPdf: $('download-pdf'),
  formatToggle: $('format-toggle'),
  formatMenu: $('format-menu'),

  frame: $('page-frame'),
  stageEmpty: $('stage-empty'),
  thumbRail: $('thumb-rail'),

  chatLog: $('chat-log'),
  chatChips: $('chat-chips'),
  chatForm: $('chat-form'),
  chatText: $('chat-text'),
  chatSend: $('chat-send'),
  versionLabel: $('version-label'),
  undoBtn: $('undo-btn'),
};

const state = {
  screen: 'auth',
  user: null,        // { email, name, picture, pages_used, page_limit }
  title: 'Untitled notes',
  latex: '',
  entries: [],        // rendered chat log
  history: [],        // [{ latex, entries, version }] for undo
  version: 1,
  pdfUrl: null,
  pdfBlob: null,
  pageCount: 0,
  currentPage: 1,
  busy: false,
  abort: null,
};

let entrySeq = 0;

/* ----------------------------------------------------------------- utils */

function show(screen) {
  state.screen = screen;
  for (const [name, node] of Object.entries(el.screens)) node.hidden = name !== screen;
}

function toast(message, opts = {}) {
  const node = document.createElement('div');
  node.className = 'toast' + (opts.blue ? ' is-blue' : '');
  const text = document.createElement('span');
  text.style.flex = '1';
  text.textContent = message;
  const close = document.createElement('button');
  close.textContent = '×';
  close.setAttribute('aria-label', 'Dismiss');
  close.addEventListener('click', () => node.remove());
  node.append(text, close);
  el.toasts.append(node);
  if (opts.sticky !== true) setTimeout(() => node.remove(), opts.ms || 4200);
  return node;
}

async function readError(res) {
  const raw = await res.text().catch(() => '');
  try {
    const parsed = JSON.parse(raw);
    if (typeof parsed?.detail === 'string') return parsed.detail;
    if (parsed?.detail) return JSON.stringify(parsed.detail, null, 2);
  } catch { /* not JSON — fall through */ }
  return raw || `${res.status} ${res.statusText}`;
}

class ApiError extends Error {
  constructor(message, detail, status) {
    super(message);
    this.detail = detail;
    this.status = status;
  }
}

// The session cookie is HttpOnly, so it has to ride on the request itself.
// 'include' rather than the same-origin default: ?api= can point elsewhere.
async function apiFetch(path, init = {}) {
  const res = await fetch(API + path, { credentials: 'include', ...init });
  if (res.status === 401) {
    onSessionLost();
    throw new ApiError('Signed out', 'Your session expired — sign in again.', 401);
  }
  return res;
}

async function apiJson(path, body, signal) {
  const res = await apiFetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok) throw new ApiError(`${path} failed (${res.status})`, await readError(res), res.status);
  return res.json();
}

// /compile and /export take the LaTeX as a raw text/plain body (see main.py).
async function apiLatexBody(path, latex, signal) {
  const res = await apiFetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'text/plain' },
    body: latex,
    signal,
  });
  if (!res.ok) throw new ApiError(`${path} failed (${res.status})`, await readError(res), res.status);
  return res.blob();
}

function slug(text) {
  return (text || 'overhand')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-|-$/g, '') || 'overhand';
}

function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

/* --------------------------------------------------------- latex plumbing */

function documentBody(doc) {
  const open = doc.indexOf('\\begin{document}');
  if (open === -1) return doc;
  const close = doc.lastIndexOf('\\end{document}');
  return doc.slice(open + '\\begin{document}'.length, close === -1 ? undefined : close);
}

/** One LaTeX document per photo comes back from the backend; stitch them into one. */
function mergeDocs(docs) {
  const usable = docs.filter(Boolean);
  if (usable.length <= 1) return usable[0] || '';

  let documentclass = '\\documentclass{article}';
  const packages = [];
  const bodies = [];

  for (const doc of usable) {
    const dc = doc.match(/\\documentclass(\[[^\]]*\])?\{[^}]*\}/);
    if (dc) documentclass = dc[0];
    const preamble = doc.split('\\begin{document}')[0] || '';
    for (const m of preamble.matchAll(/\\usepackage(\[[^\]]*\])?\{[^}]*\}/g)) {
      if (!packages.includes(m[0])) packages.push(m[0]);
    }
    bodies.push(documentBody(doc).trim());
  }

  return [
    documentclass,
    ...packages,
    '\\begin{document}',
    '',
    bodies.join('\n\n\\newpage\n\n'),
    '',
    '\\end{document}',
    '',
  ].join('\n');
}

/** Page count straight out of the PDF page tree — avoids pulling in pdf.js. */
async function countPdfPages(blob) {
  try {
    const bytes = new Uint8Array(await blob.arrayBuffer());
    let text = '';
    const step = 0x8000;
    for (let i = 0; i < bytes.length; i += step) {
      text += String.fromCharCode.apply(null, bytes.subarray(i, i + step));
    }
    const counts = [...text.matchAll(/\/Count\s+(\d+)/g)].map((m) => Number(m[1]));
    if (counts.length) return Math.max(...counts) || 1;
    const pages = text.match(/\/Type\s*\/Page[^s]/g);
    return pages ? pages.length : 1;
  } catch {
    return 1;
  }
}

/* -------------------------------------------------------------- chat log */

function addEntry(entry) {
  const withId = { id: ++entrySeq, ...entry };
  state.entries.push(withId);
  renderChat();
  return withId.id;
}

function removeEntry(id) {
  state.entries = state.entries.filter((e) => e.id !== id);
  renderChat();
}

function conversation() {
  // What /edit-latex wants: the plain user/assistant turns, in order.
  return state.entries
    .filter((e) => e.type === 'user' || e.type === 'bot')
    .map((e) => ({ role: e.type === 'user' ? 'user' : 'assistant', content: e.text }));
}

function renderChat() {
  el.chatLog.replaceChildren(...state.entries.map(renderEntry));
  el.chatLog.scrollTop = el.chatLog.scrollHeight;
}

function renderEntry(entry) {
  switch (entry.type) {
    case 'user': {
      const node = document.createElement('div');
      node.className = 'msg-user';
      node.textContent = entry.text;
      return node;
    }
    case 'bot': {
      const node = document.createElement('div');
      node.className = 'msg-bot';
      const who = document.createElement('span');
      who.className = 'who';
      who.textContent = 'Overhand';
      const p = document.createElement('p');
      p.textContent = entry.text;
      node.append(who, p);
      return node;
    }
    case 'status': {
      const node = document.createElement('div');
      node.className = 'msg-status';
      const pip = document.createElement('span');
      pip.className = 'pip';
      node.append(pip, document.createTextNode(entry.text));
      return node;
    }
    case 'error': {
      const node = document.createElement('div');
      node.className = 'msg-error';
      const who = document.createElement('span');
      who.className = 'who';
      who.textContent = 'Failed';
      const p = document.createElement('p');
      p.textContent = entry.text;
      node.append(who, p);
      if (entry.detail) {
        const details = document.createElement('details');
        const summary = document.createElement('summary');
        summary.textContent = 'Show log';
        const pre = document.createElement('pre');
        pre.textContent = entry.detail;
        details.append(summary, pre);
        node.append(details);
      }
      return node;
    }
    case 'questions':
      return renderQuestionCard(entry);
    default:
      return document.createComment('unknown entry');
  }
}

function renderQuestionCard(entry) {
  const card = document.createElement('form');
  card.className = 'qcard' + (entry.resolved ? ' is-resolved' : '');

  const head = document.createElement('div');
  head.className = 'qcard-head';
  const left = document.createElement('span');
  left.textContent = entry.resolved ? 'Answered' : 'Needs a check';
  const right = document.createElement('span');
  right.textContent = `${entry.questions.length} ${entry.questions.length === 1 ? 'spot' : 'spots'}`;
  head.append(left, right);
  card.append(head);

  const inputs = [];
  entry.questions.forEach((q, i) => {
    const item = document.createElement('div');
    item.className = 'qitem';

    const where = document.createElement('span');
    where.className = 'qwhere';
    const pct = Number.isFinite(q.confidence) ? ` · ${Math.round(q.confidence * 100)}%` : '';
    where.textContent = `${q.location || 'unknown location'}${pct}`;

    const text = document.createElement('span');
    text.className = 'qtext';
    text.textContent = q.question;

    const input = document.createElement('input');
    input.type = 'text';
    input.placeholder = 'Your answer…';
    input.value = entry.answers?.[i] || '';
    input.disabled = !!entry.resolved;
    inputs.push(input);

    item.append(where, text, input);
    card.append(item);
  });

  if (!entry.resolved) {
    const foot = document.createElement('div');
    foot.className = 'qcard-foot';
    const submit = document.createElement('button');
    submit.type = 'submit';
    submit.className = 'btn btn-blue btn-sm';
    submit.textContent = 'Apply answers';
    foot.append(submit);
    card.append(foot);

    card.addEventListener('submit', (event) => {
      event.preventDefault();
      const answers = inputs.map((input) => input.value.trim());
      if (answers.every((a) => !a)) {
        toast('Answer at least one question first.');
        return;
      }
      resolveQuestions(entry, answers);
    });
  }

  return card;
}

/* --------------------------------------------------------------- preview */

function pdfSrc(page) {
  return `${state.pdfUrl}#toolbar=0&navpanes=0&statusbar=0&view=Fit&page=${page}`;
}

function showPage(page) {
  if (!state.pdfUrl) return;
  state.currentPage = Math.min(Math.max(1, page), state.pageCount || 1);
  el.frame.src = 'about:blank';
  requestAnimationFrame(() => { el.frame.src = pdfSrc(state.currentPage); });
  renderRail();
}

function renderRail() {
  const nodes = [];
  const count = state.pageCount || 0;
  for (let i = 1; i <= count; i += 1) {
    const thumb = document.createElement('button');
    thumb.className = 'thumb' + (i === state.currentPage ? ' is-current' : '');
    thumb.type = 'button';
    thumb.textContent = String(i).padStart(2, '0');
    thumb.title = `Page ${i}`;
    thumb.addEventListener('click', () => showPage(i));
    nodes.push(thumb);
  }
  if (count) {
    const label = document.createElement('span');
    label.className = 'rail-count';
    label.textContent = `${String(state.currentPage).padStart(2, '0')} / ${String(count).padStart(2, '0')}`;
    nodes.push(label);
  }
  el.thumbRail.replaceChildren(...nodes);
  el.ppBadge.textContent = `${count} pp`;
}

async function compileAndShow({ statusText = 'Typesetting' } = {}) {
  const statusId = addEntry({ type: 'status', text: statusText });
  el.stageEmpty.hidden = false;
  el.stageEmpty.textContent = 'Compiling…';
  try {
    const blob = await apiLatexBody('/compile', state.latex);
    // Revoke the previous object URL only once the frame has moved on, or the
    // in-flight load of the old PDF aborts noisily.
    const stale = state.pdfUrl;
    if (stale) setTimeout(() => URL.revokeObjectURL(stale), 5_000);
    state.pdfBlob = blob;
    state.pdfUrl = URL.createObjectURL(blob);
    state.pageCount = await countPdfPages(blob);
    state.currentPage = Math.min(state.currentPage || 1, state.pageCount);
    el.stageEmpty.hidden = true;
    showPage(state.currentPage);
    return true;
  } catch (error) {
    if (error.name === 'AbortError') return false;
    el.stageEmpty.hidden = false;
    el.stageEmpty.textContent = 'Compile failed';
    addEntry({
      type: 'error',
      text: 'pdflatex could not compile the document. The LaTeX is still here — ask for a fix below.',
      detail: error.detail || error.message,
    });
    return false;
  } finally {
    removeEntry(statusId);
  }
}

/* ---------------------------------------------------------------- upload */

function jobRow(job) {
  const li = document.createElement('li');
  li.className = 'job' + (job.state === 'reading' ? ' is-active' : job.state === 'done' ? ' is-done' : job.state === 'failed' ? ' is-failed' : '');
  const name = document.createElement('span');
  name.className = 'job-name';
  name.textContent = job.name;
  const status = document.createElement('span');
  status.className = 'job-state';
  status.textContent = { queued: 'Queued', reading: 'Reading', done: 'Done', failed: 'Failed' }[job.state];
  li.append(name, status);
  return li;
}

async function intake(fileList) {
  const files = [...fileList].filter((f) => f.type.startsWith('image/'));
  if (!files.length) {
    toast('Those need to be image files — JPG, PNG or HEIC.');
    return;
  }
  if (files.length > MAX_PAGES) {
    toast(`That is ${files.length} photos; taking the first ${MAX_PAGES}.`);
    files.length = MAX_PAGES;
  }
  await runUpload(files);
}

async function runUpload(files) {
  const jobs = files.map((file) => ({ name: file.name || 'photo', state: 'queued' }));
  const drawJobs = () => el.jobList.replaceChildren(...jobs.map(jobRow));
  drawJobs();
  show('processing');

  state.abort = new AbortController();
  const { signal } = state.abort;

  const docs = [];
  const questions = [];

  for (let i = 0; i < files.length; i += 1) {
    jobs[i].state = 'reading';
    drawJobs();
    el.processingTitle.innerHTML = files.length > 1
      ? `Reading<br>page ${i + 1} of ${files.length}`
      : 'Reading<br>your page';

    const form = new FormData();
    form.append('file', files[i], files[i].name || `page-${i + 1}.png`);

    try {
      const res = await apiFetch('/upload-photo/', { method: 'POST', body: form, signal });
      if (!res.ok) throw new ApiError(`upload failed (${res.status})`, await readError(res), res.status);
      const result = await res.json();
      // The page is spent whether or not the rest of the job succeeds.
      if (state.user) { state.user.pages_used += 1; renderUser(); }
      docs.push(result.latex || '');
      for (const q of result.questions || []) {
        questions.push({
          confidence: q.confidence,
          location: files.length > 1 ? `Page ${i + 1} — ${q.location}` : q.location,
          question: q.question,
        });
      }
      jobs[i].state = 'done';
    } catch (error) {
      if (error.name === 'AbortError') return;
      jobs[i].state = 'failed';
      toast(`${jobs[i].name}: ${error.detail || error.message}`, { sticky: true });
      // Out of pages, or signed out: every remaining photo would fail the same way.
      if (error.status === 402 || error.status === 401) {
        for (let j = i + 1; j < files.length; j += 1) jobs[j].state = 'failed';
        drawJobs();
        break;
      }
    }
    drawJobs();
  }

  state.abort = null;

  if (state.screen === 'auth') return;  // signed out mid-run; onSessionLost has the screen

  if (!docs.length) {
    toast('None of those pages could be read. Try a sharper photo.', { blue: true, sticky: true });
    show('upload');
    return;
  }

  state.latex = mergeDocs(docs);
  state.title = files.length === 1
    ? (files[0].name || 'Untitled notes').replace(/\.[a-z0-9]+$/i, '')
    : `${files.length} pages — ${new Date().toLocaleDateString()}`;
  state.version = 1;
  state.history = [];
  state.entries = [];
  state.currentPage = 1;

  el.docTitle.textContent = state.title;
  updateVersion();
  show('result');

  const read = docs.length === 1 ? 'Page read.' : `All ${docs.length} pages read.`;
  addEntry({
    type: 'bot',
    text: questions.length
      ? `${read} ${questions.length} ${questions.length === 1 ? 'spot was' : 'spots were'} hard to make out — answer below and I will patch the transcription. Otherwise, tell me what to change.`
      : `${read} Nothing was ambiguous. Tell me what to change, or download it as it stands.`,
  });
  if (questions.length) addEntry({ type: 'questions', questions, answers: [] });

  await compileAndShow({ statusText: 'Typesetting' });
}

/* ------------------------------------------------------------ operations */

function setBusy(busy) {
  state.busy = busy;
  el.chatSend.disabled = busy;
  el.chatText.disabled = busy;
  for (const chip of el.chatChips.querySelectorAll('button')) chip.disabled = busy;
  el.undoBtn.disabled = busy || !state.history.length;
}

function snapshot() {
  state.history.push({
    latex: state.latex,
    entries: state.entries.map((e) => ({ ...e })),
    version: state.version,
  });
}

function updateVersion() {
  el.versionLabel.textContent = `v${state.version}`;
  el.undoBtn.disabled = state.busy || !state.history.length;
}

async function sendEdit(text) {
  if (!text || state.busy) return;
  setBusy(true);
  snapshot();
  addEntry({ type: 'user', text });
  const statusId = addEntry({ type: 'status', text: 'Rewriting' });

  try {
    const result = await apiJson('/edit-latex', { latex: state.latex, messages: conversation() });
    removeEntry(statusId);
    state.latex = result.latex || state.latex;
    state.version += 1;
    addEntry({ type: 'bot', text: result.message || 'Done.' });
    updateVersion();
    await compileAndShow({ statusText: 'Recompiling' });
  } catch (error) {
    removeEntry(statusId);
    state.history.pop();
    if (error.name !== 'AbortError') {
      addEntry({ type: 'error', text: 'That edit did not go through.', detail: error.detail || error.message });
    }
    updateVersion();
  } finally {
    setBusy(false);
  }
}

async function resolveQuestions(entry, answers) {
  if (state.busy) return;
  setBusy(true);
  snapshot();
  entry.answers = answers;
  entry.resolved = true;
  renderChat();
  const statusId = addEntry({ type: 'status', text: 'Applying answers' });

  try {
    const result = await apiJson('/resolve-questions', {
      latex: state.latex,
      questions: entry.questions,
      answers,
    });
    removeEntry(statusId);
    state.latex = result.latex || state.latex;
    state.version += 1;
    const followUps = result.questions || [];
    addEntry({
      type: 'bot',
      text: followUps.length
        ? 'Transcription patched. A couple of spots are still unclear:'
        : 'Transcription patched with your answers.',
    });
    if (followUps.length) addEntry({ type: 'questions', questions: followUps, answers: [] });
    updateVersion();
    await compileAndShow({ statusText: 'Recompiling' });
  } catch (error) {
    removeEntry(statusId);
    entry.resolved = false;
    state.history.pop();
    if (error.name !== 'AbortError') {
      addEntry({ type: 'error', text: 'Could not apply those answers.', detail: error.detail || error.message });
    }
    renderChat();
    updateVersion();
  } finally {
    setBusy(false);
  }
}

async function undo() {
  const previous = state.history.pop();
  if (!previous || state.busy) return;
  setBusy(true);
  state.latex = previous.latex;
  state.entries = previous.entries;
  state.version = previous.version;
  renderChat();
  updateVersion();
  await compileAndShow({ statusText: 'Reverting' });
  setBusy(false);
}

const EXPORT_EXT = { pdf: 'pdf', docx: 'docx', md: 'md', png: 'png' };

async function download(fmt) {
  if (!state.latex) return;
  const name = slug(el.docTitle.textContent || state.title);

  if (fmt === 'tex') {
    saveBlob(new Blob([state.latex], { type: 'application/x-tex' }), `${name}.tex`);
    return;
  }
  // The compiled PDF is already in hand — no need to make the server redo it.
  if (fmt === 'pdf' && state.pdfBlob) {
    saveBlob(state.pdfBlob, `${name}.pdf`);
    return;
  }

  const note = toast(`Exporting ${fmt.toUpperCase()}…`, { sticky: true, blue: true });
  try {
    const blob = await apiLatexBody(`/export/${fmt}`, state.latex);
    saveBlob(blob, `${name}.${EXPORT_EXT[fmt] || fmt}`);
  } catch (error) {
    addEntry({ type: 'error', text: `${fmt.toUpperCase()} export failed.`, detail: error.detail || error.message });
  } finally {
    note.remove();
  }
}

function reset() {
  if (state.pdfUrl) URL.revokeObjectURL(state.pdfUrl);
  Object.assign(state, {
    title: 'Untitled notes',
    latex: '',
    entries: [],
    history: [],
    version: 1,
    pdfUrl: null,
    pdfBlob: null,
    pageCount: 0,
    currentPage: 1,
    busy: false,
    abort: null,
  });
  el.frame.src = 'about:blank';
  el.fileInput.value = '';
  renderChat();
  renderRail();
  setBusy(false);
  show('upload');
}

/* ------------------------------------------------------------------- auth */

function renderUser() {
  const user = state.user;
  el.userChip.hidden = !user;
  if (!user) return;

  el.userEmail.textContent = user.email;
  // Google avatar URLs can 404 or be blocked; the email is the real identifier.
  el.userAvatar.hidden = !user.picture;
  if (user.picture) el.userAvatar.src = user.picture;
  el.userAvatar.onerror = () => { el.userAvatar.hidden = true; };

  const left = Math.max(0, user.page_limit - user.pages_used);
  el.userUsage.textContent = `${left} pp left`;
  el.userUsage.classList.toggle('is-spent', left === 0);

  el.statQuota.textContent = left
    ? `${left} ${left === 1 ? 'page' : 'pages'} left on your account.`
    : 'You have used every page on your account.';
  el.statQuotaLabel.textContent = `${user.pages_used} of ${user.page_limit} used`;
}

const LOADING_NOTE = el.authNote.textContent;  // the placeholder in index.html

function authNote(text, isError = false) {
  el.authNote.textContent = text;
  el.authNote.classList.toggle('is-error', isError);
}

// Called from apiFetch whenever the backend says 401.
function onSessionLost() {
  const wasSignedIn = state.user !== null;
  state.user = null;
  renderUser();
  show('auth');
  mountGoogleButton();  // no-op if it is already there
  if (wasSignedIn) authNote('Your session expired. Sign in again to pick up where you left off.');
}

async function signIn(credential) {
  authNote('Signing in…');
  try {
    const res = await fetch(API + '/auth/google', {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ credential }),
    });
    if (!res.ok) throw new ApiError('sign-in failed', await readError(res), res.status);
    state.user = await res.json();
    renderUser();
    authNote('');
    show('upload');
    toast(`Signed in as ${state.user.email}.`, { blue: true, ms: 2600 });
  } catch (error) {
    authNote(error.detail || error.message, true);
  }
}

async function signOut() {
  try {
    await fetch(API + '/auth/logout', { method: 'POST', credentials: 'include' });
  } catch { /* the cookie is the server's to clear; a failure here just means offline */ }
  reset();
  state.user = null;
  renderUser();
  show('auth');
  mountGoogleButton();
  authNote('Signed out.');
}

// Google Identity Services loads async, so poll briefly for the global.
function whenGoogleReady() {
  return new Promise((resolve, reject) => {
    if (window.google?.accounts?.id) return resolve();
    let waited = 0;
    const tick = setInterval(() => {
      if (window.google?.accounts?.id) {
        clearInterval(tick);
        resolve();
      } else if ((waited += 100) > 8000) {
        clearInterval(tick);
        reject(new Error('Google sign-in could not load. Check the network, then reload.'));
      }
    }, 100);
  });
}

let gsiMounted = false;

async function mountGoogleButton() {
  if (gsiMounted) return;
  gsiMounted = true;
  try {
    const res = await fetch(API + '/auth/config', { credentials: 'include' });
    if (!res.ok) throw new Error('the backend did not return a client id');
    const { google_client_id: clientId } = await res.json();
    if (!clientId) throw new Error('GOOGLE_CLIENT_ID is not set on the backend — see .env.example');

    await whenGoogleReady();
    google.accounts.id.initialize({
      client_id: clientId,
      callback: ({ credential }) => signIn(credential),
    });
    google.accounts.id.renderButton(el.gSignin, {
      theme: 'outline', size: 'large', text: 'continue_with', shape: 'rectangular', width: 280,
    });
    // Only retire the placeholder. Mounting is async, so a message set meanwhile
    // ("Signed out.", "Your session expired.") must not be clobbered.
    if (el.authNote.textContent === LOADING_NOTE) authNote('');
  } catch (error) {
    gsiMounted = false;  // let a later visit to the auth screen retry
    authNote(error.message, true);
  }
}

async function boot() {
  try {
    const res = await fetch(API + '/auth/me', { credentials: 'include' });
    if (res.ok) {
      state.user = await res.json();
      renderUser();
      show('upload');
      return;
    }
  } catch {
    authNote('Cannot reach the backend. Is uvicorn running on port 8000?', true);
  }
  show('auth');
  mountGoogleButton();
}

/* ----------------------------------------------------------------- wiring */

el.choosePhotos.addEventListener('click', () => el.fileInput.click());
el.dropzone.addEventListener('click', (event) => {
  if (event.target.closest('button')) return;
  el.fileInput.click();
});
el.fileInput.addEventListener('change', () => {
  if (el.fileInput.files.length) intake(el.fileInput.files);
});

el.trySample.addEventListener('click', async (event) => {
  event.stopPropagation();
  try {
    const res = await fetch('assets/sample.png');
    if (!res.ok) throw new Error(String(res.status));
    const blob = await res.blob();
    await intake([new File([blob], 'set-notes-sample.png', { type: 'image/png' })]);
  } catch {
    toast('Could not load the sample — serve this folder over http rather than opening the file directly.', { sticky: true });
  }
});

/* drag & drop anywhere, plus clipboard paste */
let dragDepth = 0;
window.addEventListener('dragenter', (event) => {
  if (![...(event.dataTransfer?.types || [])].includes('Files')) return;
  dragDepth += 1;
  el.dropVeil.hidden = false;
  el.dropzone.classList.add('is-hot');
});
window.addEventListener('dragover', (event) => event.preventDefault());
window.addEventListener('dragleave', () => {
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) {
    el.dropVeil.hidden = true;
    el.dropzone.classList.remove('is-hot');
  }
});
window.addEventListener('drop', (event) => {
  event.preventDefault();
  dragDepth = 0;
  el.dropVeil.hidden = true;
  el.dropzone.classList.remove('is-hot');
  if (state.screen === 'processing') return;
  if (event.dataTransfer?.files?.length) intake(event.dataTransfer.files);
});

window.addEventListener('paste', (event) => {
  if (state.screen !== 'upload') return;
  const files = [...(event.clipboardData?.files || [])];
  if (files.length) {
    event.preventDefault();
    intake(files);
  }
});

el.cancelProcessing.addEventListener('click', () => {
  state.abort?.abort();
  state.abort = null;
  show('upload');
});

/* chat */
el.chatForm.addEventListener('submit', (event) => {
  event.preventDefault();
  const text = el.chatText.value.trim();
  if (!text) return;
  el.chatText.value = '';
  el.chatText.style.height = 'auto';
  sendEdit(text);
});

el.chatText.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    el.chatForm.requestSubmit();
  }
});
el.chatText.addEventListener('input', () => {
  el.chatText.style.height = 'auto';
  el.chatText.style.height = `${Math.min(el.chatText.scrollHeight, 140)}px`;
});

el.chatChips.addEventListener('click', (event) => {
  const chip = event.target.closest('button[data-chip]');
  if (chip && !state.busy) sendEdit(chip.dataset.chip);
});

el.undoBtn.addEventListener('click', undo);

/* header actions */
el.newDoc.addEventListener('click', reset);

el.copyTex.addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(state.latex);
    toast('LaTeX source copied.', { blue: true, ms: 2200 });
  } catch {
    saveBlob(new Blob([state.latex], { type: 'application/x-tex' }), `${slug(state.title)}.tex`);
    toast('Clipboard blocked — downloaded the .tex instead.');
  }
});

el.downloadPdf.addEventListener('click', () => download('pdf'));

el.formatToggle.addEventListener('click', (event) => {
  event.stopPropagation();
  el.formatMenu.hidden = !el.formatMenu.hidden;
});
el.formatMenu.addEventListener('click', (event) => {
  const btn = event.target.closest('button[data-fmt]');
  if (!btn) return;
  el.formatMenu.hidden = true;
  download(btn.dataset.fmt);
});
document.addEventListener('click', () => { el.formatMenu.hidden = true; });

el.docTitle.addEventListener('blur', () => {
  state.title = el.docTitle.textContent.trim() || 'Untitled notes';
  el.docTitle.textContent = state.title;
});
el.docTitle.addEventListener('keydown', (event) => {
  if (event.key === 'Enter') {
    event.preventDefault();
    el.docTitle.blur();
  }
});

/* nav links have no backend behind them yet */
for (const link of document.querySelectorAll('a[data-inert]')) {
  link.addEventListener('click', (event) => {
    event.preventDefault();
    toast(`“${link.textContent.trim()}” is not wired up yet.`);
  });
}

el.signOut.addEventListener('click', signOut);

renderRail();
boot();
