# StudyMate

**Read anything, understand everything.** Drop in a PDF, a screenshot, a Word file or a photo of your notes, then ask questions about it — or select any part of the page and have it explained.

Works with **any** AI provider: your own laptop via [Ollama](https://ollama.com) (free, unlimited, private) or a cloud key you already have (Gemini, Groq, OpenAI, Anthropic, Mistral, and more). There is no account, no sign-up and no billing in this app.

---

## What it does

| | |
|---|---|
| **Reads** | PDF, DOCX, PPTX, XLSX, TXT/MD, and images (PNG/JPG) |
| **Understands pictures** | Built-in OCR reads screenshots, phone photos of notes, scanned pages and diagram labels — no text layer required |
| **Explains a selection** | Drag a box around any part of a page, or select text, then ask |
| **Keeps materials apart** | One workspace per material; notes Sage writes stay with it |
| **Library** | Every file you open is kept, searchable, labelled by the device that added it |
| **Works offline-ish** | If the model call fails it still answers from your document — citations, key terms, structure |
| **Installable** | It is a PWA: "Add to Home Screen" on a phone gives a full-screen app |

## Quick start (your own computer)

```bash
git clone https://github.com/<you>/studymate.git
cd studymate
python -m venv .venv
.venv/bin/pip install -r requirements.txt      # Windows: .venv\Scripts\pip
uvicorn main:app --app-dir app --port 8000
```

Open <http://localhost:8000>.

**To get AI answers locally (free, unlimited):**

```bash
ollama pull llama3.1:8b
ollama serve
```

In the app: **Add AI key → Provider: Local model on your own computer → Model: llama3.1:8b**.

**Or bring a cloud key:** Gemini's free tier needs no credit card — <https://aistudio.google.com/apikey>. Paste it in **Add AI key**; the key is stored in your browser only.

## Deploy to the web (free, no credit card)

GitHub Pages **cannot** host this — the app is a Python server, not a static site. Use Render's free tier instead; it connects straight to your GitHub repo.

See **[DEPLOY.md](DEPLOY.md)** for the click-by-click version. The short form:

1. Push this repo to GitHub.
2. Render → **New → Blueprint** → pick the repo. It reads `render.yaml` automatically.
3. Open the URL Render gives you.
4. Each visitor clicks **Add AI key** and pastes their own free Gemini/Groq key.

> **Free-tier realities:** the service sleeps after 15 minutes of inactivity (first request then takes ~30 s), and it has **no persistent disk** — documents uploaded by visitors are cleared whenever the instance restarts. For permanent storage, run it on your own machine (or attach a paid disk).

## How the AI is picked

`app/llm.py` holds one registry of providers; each entry knows its base URL, model list and whether it can see images. The request path tries, in order:

1. the key the visitor pasted (browser-only),
2. the key the machine owner saved as a *site key*,
3. each configured backup provider,
4. the offline engine — which always answers from the document itself.

Keys are never written into the repo. The optional site key lives in `data/site_key.json` on the server and is never returned to a browser.

## OCR

`app/ocr.py` reads text out of pictures using [RapidOCR](https://github.com/RapidAI/RapidOCR) (ONNX, models bundled — no system packages). It runs:

- on upload for images,
- on every dragged region whose text layer is empty,
- on demand for scanned PDF pages, cached back into the document so it is paid for once.

Costs ~300 MB RAM and ~1 s per small image. Disable with `STUDYMATE_OCR=0`.

## Configuration

Every setting is an environment variable; all are optional.

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `8000` | port to listen on |
| `STUDYMATE_DATA` | `./data` | where documents and the workspace live |
| `STUDYMATE_MAX_MB` | `60` | upload size ceiling |
| `STUDYMATE_KEEP_DOCS` | `200` | documents kept before pruning the oldest |
| `STUDYMATE_OCR` | `1` | `0` disables OCR |
| `STUDYMATE_LLM_KEY` | — | server-side key (optional; visitors' own keys win) |
| `STUDYMATE_LLM_PROVIDER` | `gemini` | provider for `STUDYMATE_LLM_KEY` |
| `STUDYMATE_LLM_MODEL` | provider default | model id |
| `STUDYMATE_DAILY_LIMIT` | `250` | answers/day per visitor for a shared site key |
| `STUDYMATE_ORIGINS` | same-origin | extra allowed CORS origins |
| `PYTHONUTF8` | — | set `1` on Windows (default code page is cp1252) |

## Run with Docker

```bash
docker build -t studymate .
docker run -p 8000:8000 -v "$PWD/data:/data" studymate
```

## Project layout

```
app/
  main.py       FastAPI routes: upload, reader, tutor stream, library, workspace, keys
  extract.py    PDF/DOCX/PPTX/XLSX/text/image → pages + chunks (text layer, regions)
  ocr.py        pictures → text (RapidOCR / Tesseract, both optional)
  retrieve.py   which passages the tutor actually sees
  llm.py        provider registry, streaming, failover, vision capability
  keyring.py    per-key cooldown so a dead key is not retried for an hour
  site_key.py   the optional server-side key + per-visitor daily limit
  offline.py    the engine that answers with no model at all
  config.py     every environment variable in one place
  static/       the whole front end (vanilla JS, no build step)
```

## Licence

MIT — see [LICENSE](LICENSE).
