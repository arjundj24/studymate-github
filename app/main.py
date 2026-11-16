"""StudyMate — AI document tutor. FastAPI backend."""
from __future__ import annotations

import base64
from urllib.parse import unquote
import hashlib
import json
import re
import mimetypes
import os
import shutil
import time
import uuid

import httpx

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles

import extract as X
import llm
import offline
import site_key as SK
import keyring as KR
from retrieve import Index, section_hint, section_span, whole_doc_text

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")

try:
    import config as CFG
except Exception:                        # noqa: BLE001  (keeps old behaviour if config.py is absent)
    class CFG:                          # type: ignore
        MAX_MB, ORIGINS, PORT, BACKUP_ENABLED = 60, ["*"], 8000, False

MAX_BYTES = CFG.MAX_MB * 1024 * 1024

WS_DIR = os.path.join(X.DATA_DIR, "workspace")
WS_INDEX = os.path.join(WS_DIR, "index.json")

app = FastAPI(title="StudyMate", version="1.1")
app.add_middleware(CORSMiddleware, allow_origins=CFG.ORIGINS, allow_methods=["*"], allow_headers=["*"])

os.makedirs(X.DOCS_DIR, exist_ok=True)
os.makedirs(WS_DIR, exist_ok=True)


def _asset_version() -> str:
    """Fingerprint of the front-end so a stale cached bundle can never run
    against a newer index.html (that failure looked exactly like 'upload does nothing')."""
    h = hashlib.md5()
    for name in ("index.html", "app.css", "app.js"):
        p = os.path.join(STATIC, name)
        try:
            h.update(open(p, "rb").read())
        except OSError:
            pass
    return h.hexdigest()[:10]


ASSET_V = _asset_version()


@app.middleware("http")
async def _no_stale_assets(request: Request, call_next):
    resp = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


# --------------------------------------------------------------------------- #
# static
# --------------------------------------------------------------------------- #
@app.get("/")
def root():
    with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as f:
        html = f.read().replace("__ASSET_V__", ASSET_V)
    return HTMLResponse(html, headers={"Cache-Control": "no-store, max-age=0"})


@app.get("/api/version")
def version():
    try:
        import ocr as _ocr
        cap = {"available": _ocr.available(), "engine": _ocr.engine_name()}
    except Exception:  # noqa: BLE001
        cap = {"available": False, "engine": ""}
    return {"asset_version": ASSET_V, "ocr": cap}


app.mount("/static", StaticFiles(directory=STATIC), name="static")


# --------------------------------------------------------------------------- #
# workspace — every material you opened, and everything Sage writes for you
# --------------------------------------------------------------------------- #
def _ws_load() -> list[dict]:
    try:
        with open(WS_INDEX, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _ws_save(items: list[dict]) -> None:
    os.makedirs(WS_DIR, exist_ok=True)
    tmp = WS_INDEX + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=1)
    os.replace(tmp, WS_INDEX)


def _ws_owner(it: dict) -> str | None:
    """Which material's workspace a row belongs to (falls back to its own document)."""
    return it.get("owner") or it.get("doc_id")


def _ws_add(entry: dict) -> dict:
    items = _ws_load()
    owner = _ws_owner(entry)
    for it in items:                         # one row per file *inside one workspace*
        if _ws_owner(it) != owner:
            continue
        if entry.get("doc_id") and it.get("doc_id") == entry["doc_id"]:
            return it
        if entry.get("name") == it.get("name") and entry.get("size") == it.get("size"):
            return it
    items.insert(0, entry)
    _ws_save(items[:600])
    return entry


def _ws_entry(name: str, path: str, source: str, **extra) -> dict:
    try:
        size = os.path.getsize(os.path.join(X.DATA_DIR, path))
    except OSError:
        size = 0
    return {
        "id": uuid.uuid4().hex[:12],
        "name": name,
        "path": path,
        "source": source,                    # "upload" | "sage" | "workspace"
        "size": size,
        "created": time.time(),
        **extra,
    }


def _ws_add_material(doc: dict) -> None:
    """A document opened in the reader is automatically part of the workspace."""
    src = X.source_path(doc["id"])
    if not src:
        return
    rel = os.path.relpath(src, X.DATA_DIR)
    try:
        digest = hashlib.sha1(open(src, "rb").read()).hexdigest()
    except OSError:
        digest = None
    _ws_add(_ws_entry(doc["filename"], rel, "upload", doc_id=doc["id"], owner=doc["id"],
                      mime=mimetypes.guess_type(src)[0], pages=doc["n_pages"],
                      words=doc["n_words"], ext=doc["ext"], sha1=digest))


def _ws_backfill() -> None:
    """Pick up documents that were uploaded before the workspace existed."""
    if not os.path.isdir(X.DOCS_DIR):
        return
    items = _ws_load()
    known = {it.get("doc_id") for it in items}
    seen = {(it.get("name"), it.get("size")) for it in items}
    for d in os.listdir(X.DOCS_DIR):
        if d in known or os.path.exists(os.path.join(X.DOCS_DIR, d, ".fromws")):
            continue
        doc = X.load_doc(d)
        if not doc:
            continue
        try:
            size = os.path.getsize(X.source_path(d) or "")
        except OSError:
            size = None
        if (doc["filename"], size) in seen:
            continue
        _ws_add_material(doc)
        seen.add((doc["filename"], size))


@app.get("/api/workspace")
def workspace_list(doc_id: str | None = None):
    """Workspace files. `doc_id` scopes the list to one material's own workspace."""
    items = [it for it in _ws_load()
             if os.path.exists(os.path.join(X.DATA_DIR, it.get("path", "")))]
    if doc_id:
        items = [it for it in items if _ws_owner(it) == doc_id]
    counts: dict[str, int] = {}
    for it in items:
        o = _ws_owner(it)
        if o:
            counts[o] = counts.get(o, 0) + 1
    return {"files": items, "counts": counts}


@app.post("/api/workspace/upload")
async def workspace_upload(file: UploadFile = File(...), doc_id: str | None = Form(None)):
    name = file.filename or "file"
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "Empty file")
    if len(raw) > MAX_BYTES:
        raise HTTPException(413, "File is larger than 60 MB")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", name)[-90:] or "file"
    fid = uuid.uuid4().hex[:12]
    rel = os.path.join("workspace", f"{fid}-{slug}")
    with open(os.path.join(X.DATA_DIR, rel), "wb") as f:
        f.write(raw)
    entry = _ws_add(_ws_entry(name, rel, "workspace",
                              mime=file.content_type or mimetypes.guess_type(name)[0],
                              ext=os.path.splitext(name)[1].lower(),
                              doc_id=(doc_id or None), owner=(doc_id or None)))
    return entry


@app.post("/api/workspace/from-text")
async def workspace_from_text(request: Request):
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "Nothing to save")
    title = (body.get("title") or "Sage note").strip()[:80]
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", title).strip("-")[:60] or "sage-note"
    fid = uuid.uuid4().hex[:12]
    rel = os.path.join("workspace", f"{fid}-{slug}.md")
    with open(os.path.join(X.DATA_DIR, rel), "w", encoding="utf-8") as f:
        f.write(text + "\n")
    entry = _ws_add(_ws_entry(f"{title}.md", rel, "sage", mime="text/markdown",
                              ext=".md", doc_id=body.get("doc_id"),
                              owner=body.get("owner") or body.get("doc_id"),
                              mode=body.get("mode")))
    return entry


@app.post("/api/workspace/from-library")
async def workspace_from_library(request: Request):
    """Bring a material from the library into the open material's workspace."""
    body = await request.json()
    doc_id = (body.get("doc_id") or "").strip()
    doc = X.load_doc(doc_id) if doc_id else None
    if not doc:
        raise HTTPException(404, "No such material in the library")
    owner = (body.get("owner") or body.get("scope") or doc_id).strip()
    for it in _ws_load():
        if _ws_owner(it) == owner and it.get("doc_id") == doc_id:
            return {**it, "reused": True}
    src = X.source_path(doc_id)
    if not src:
        raise HTTPException(404, "The file for that material is missing")
    rel = os.path.relpath(src, X.DATA_DIR)
    entry = _ws_add(_ws_entry(doc["filename"], rel, "upload", doc_id=doc_id, owner=owner,
                              mime=mimetypes.guess_type(src)[0], pages=doc.get("n_pages"),
                              words=doc.get("n_words"), ext=doc.get("ext")))
    return entry


@app.get("/api/workspace/{wid}/raw")
def workspace_raw(wid: str):
    for it in _ws_load():
        if it["id"] == wid:
            p = os.path.join(X.DATA_DIR, it["path"])
            if not os.path.exists(p):
                raise HTTPException(404, "Missing file")
            dl = it.get("mime") not in ("application/pdf", "text/markdown", "text/plain")
            return FileResponse(p, media_type=it.get("mime") or "application/octet-stream",
                                filename=it["name"] if dl else None)
    raise HTTPException(404, "Not found")


@app.get("/api/workspace/{wid}/text")
def workspace_text(wid: str, limit: int = Query(20000, ge=500, le=200000)):
    for it in _ws_load():
        if it["id"] == wid:
            p = os.path.join(X.DATA_DIR, it["path"])
            if not os.path.exists(p):
                raise HTTPException(404, "Missing file")
            if (it.get("ext") or "").lower() in (".md", ".markdown", ".txt", ".csv", ".json"):
                return {"text": X._read_text(p)[:limit], "name": it["name"]}
            raise HTTPException(415, "Not a text file")
    raise HTTPException(404, "Not found")


@app.post("/api/workspace/{wid}/open")
def workspace_open(wid: str):
    """Read a workspace file in the document reader."""
    for it in _ws_load():
        if it["id"] == wid:
            p = os.path.join(X.DATA_DIR, it["path"])
            if not os.path.exists(p):
                raise HTTPException(404, "Missing file")
            if it.get("doc_id") and X.load_doc(it["doc_id"]):
                return {"doc_id": it["doc_id"]}
            raw = open(p, "rb").read()
            data = X.extract(p, it["name"])
            doc = X.save_doc(X.new_doc_id(), it["name"], raw, data)
            try:  # tag: this document is only a reader view of a workspace file
                with open(os.path.join(X.DOCS_DIR, doc["id"], ".fromws"), "w", encoding="utf-8") as f:
                    f.write(wid)
            except OSError:
                pass
            it["doc_id"] = doc["id"]
            items = _ws_load()
            for i, x in enumerate(items):
                if x["id"] == wid:
                    items[i] = it
            _ws_save(items)
            return {"doc_id": doc["id"]}
    raise HTTPException(404, "Not found")


@app.delete("/api/workspace/{wid}")
def workspace_delete(wid: str):
    items = _ws_load()
    keep, removed = [], None
    for it in items:
        if it["id"] == wid:
            removed = it
        else:
            keep.append(it)
    if removed and removed.get("source") == "workspace":
        try:                                   # uploads opened as documents keep their bytes
            os.remove(os.path.join(X.DATA_DIR, removed["path"]))
        except OSError:
            pass
    _ws_save(keep)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# documents
# --------------------------------------------------------------------------- #
def _public(doc: dict) -> dict:
    return {
        "id": doc["id"],
        "filename": doc["filename"],
        "ext": doc["ext"],
        "kind": doc["kind"],
        "paged": doc["paged"],
        "n_pages": doc["n_pages"],
        "n_words": doc["n_words"],
        "n_chars": doc["n_chars"],
        "has_text": (doc.get("n_words") or 0) > 12,
        "created": doc.get("created"),
        "outline": X.doc_outline(doc),
    }


@app.post("/api/upload")
async def upload(request: Request, file: UploadFile = File(...)):
    name = file.filename or "material"
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "Empty file")
    if len(raw) > MAX_BYTES:
        raise HTTPException(413, "File is larger than 60 MB")
    digest = hashlib.sha1(raw).hexdigest()
    for other in _ws_load():                      # same file already opened? reuse it
        if other.get("size") == len(raw) and other.get("sha1") == digest and other.get("doc_id"):
            existing = X.load_doc(other["doc_id"])
            if existing and existing.get("filename") == name:
                return {**_public(existing), "reused": True}
    doc_id = X.new_doc_id()
    tmp_dir = os.path.join(X.DOCS_DIR, doc_id)
    os.makedirs(tmp_dir, exist_ok=True)
    tmp_path = os.path.join(tmp_dir, "upload" + (os.path.splitext(name)[1].lower()))
    with open(tmp_path, "wb") as f:
        f.write(raw)
    try:
        data = X.extract(tmp_path, name)
    except Exception as e:  # noqa: BLE001
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(422, f"Could not read this file: {e}") from e
    os.remove(tmp_path)
    me = _visitor(request)
    doc = X.save_doc(doc_id, name, raw, data,
                     extra={"visitor": me["id"], "device": me["label"]})
    try:
        _ws_add_material(doc)
    except Exception:  # noqa: BLE001 — the workspace must never block an upload
        pass
    return _public(doc)


@app.get("/api/docs")
def list_docs(request: Request, scope: str = "all"):
    me = _visitor(request)
    out = []
    if os.path.isdir(X.DOCS_DIR):
        for d in os.listdir(X.DOCS_DIR):
            doc = X.load_doc(d)
            if doc:
                mine = bool(me["id"]) and doc.get("visitor") == me["id"]
                out.append({"id": doc["id"], "filename": doc["filename"],
                            "kind": doc["kind"], "n_pages": doc["n_pages"],
                            "n_words": doc["n_words"], "created": doc.get("created"),
                            "device": doc.get("device") or "",
                            "mine": mine})
    out.sort(key=lambda d: -(d.get("created") or 0))
    if (scope or "").lower() == "mine":
        out = [d for d in out if d["mine"]]
    return {"docs": out[:30], "me": me}


@app.delete("/api/docs/{doc_id}")
def delete_doc(doc_id: str):
    p = os.path.join(X.DOCS_DIR, doc_id)
    if os.path.isdir(p):
        shutil.rmtree(p, ignore_errors=True)
    return {"ok": True}


@app.get("/api/docs/{doc_id}")
def get_doc(doc_id: str):
    doc = X.load_doc(doc_id)
    if not doc:
        raise HTTPException(404, "Document not found")
    return _public(doc)


@app.get("/api/docs/{doc_id}/page/{n}")
def get_page(doc_id: str, n: int):
    doc = X.load_doc(doc_id)
    if not doc:
        raise HTTPException(404, "Document not found")
    if n < 1 or n > doc["n_pages"]:
        raise HTTPException(404, "No such page")
    page = doc["pages"][n - 1]
    out = {"n": page["n"], "label": page.get("label", f"Page {n}"),
           "text": page.get("text") or "", "kind": doc["kind"],
           "table": page.get("table"), "image_only": page.get("image_only", False)}
    if doc["ext"] == ".pdf":
        out["image_url"] = f"/api/docs/{doc_id}/render/{n}.png"
    if out["image_only"]:
        out["image_url"] = f"/api/docs/{doc_id}/source"
    return out


@app.get("/api/docs/{doc_id}/words/{n}")
def page_words(doc_id: str, n: int):
    """Selectable text layer for a rendered PDF page (normalised word boxes)."""
    if not X.load_doc(doc_id):
        raise HTTPException(404, "No such document")
    data = X.page_words(doc_id, n)
    if not data:
        return {"w": 0, "h": 0, "words": []}
    return data


@app.get("/api/docs/{doc_id}/render/{n}.png")
def render_page(doc_id: str, n: int, zoom: float = Query(2.0, ge=1.0, le=3.0)):
    path = X.page_image(doc_id, n, zoom)
    if not path:
        raise HTTPException(404, "Not renderable")
    return FileResponse(path, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/docs/{doc_id}/source")
def source(doc_id: str):
    p = X.source_path(doc_id)
    if not p:
        raise HTTPException(404, "Not found")
    mt, _ = mimetypes.guess_type(p)
    return FileResponse(p, media_type=mt or "application/octet-stream")


@app.get("/api/docs/{doc_id}/search")
def search(doc_id: str, q: str = ""):
    doc = X.load_doc(doc_id)
    if not doc:
        raise HTTPException(404, "Document not found")
    idx = Index(doc["chunks"])
    return {"query": q, "hits": idx.keyword_search(q)}


@app.get("/api/docs/{doc_id}/full")
def full_doc(doc_id: str):
    """Every page's renderable content in one shot — the continuous-scroll reader uses this."""
    doc = X.load_doc(doc_id)
    if not doc:
        raise HTTPException(404, "Document not found")
    pages = []
    for page in doc["pages"]:
        p = {"n": page["n"], "label": page.get("label", f"Page {page['n']}"),
             "text": page.get("text") or "", "table": page.get("table"),
             "image_only": page.get("image_only", False)}
        if doc["ext"] == ".pdf":
            p["image_url"] = f"/api/docs/{doc_id}/render/{page['n']}.png"
        if p["image_only"]:
            p["image_url"] = f"/api/docs/{doc_id}/source"
        pages.append(p)
    return {**_public(doc), "pages": pages}


@app.post("/api/docs/{doc_id}/region")
async def make_region(doc_id: str, request: Request):
    """Crop the rectangle the learner dragged on a page (pixels at RENDER_ZOOM)."""
    doc = X.load_doc(doc_id)
    if not doc:
        raise HTTPException(404, "Document not found")
    body = await request.json()
    try:
        n = int(body.get("page", 1))
        rect = body.get("rect") or []
        if len(rect) != 4:
            raise ValueError("rect needs 4 numbers")
    except (TypeError, ValueError) as e:
        raise HTTPException(400, str(e)) from e
    crop = X.region_crop(doc_id, n, rect)
    if not crop:
        raise HTTPException(422, "That area could not be read (try a larger region)")
    rid = os.path.basename(crop["path"]).replace("region-", "").replace(".png", "")
    return {"region_id": rid, "page": n, "text": crop["text"][:6000],
            "w": crop["w"], "h": crop["h"],
            "image_url": f"/api/docs/{doc_id}/region/{rid}.png"}


@app.post("/api/docs/{doc_id}/ocr/{n}")
def ocr_document_page(doc_id: str, n: int):
    """Read the words out of a page with no text layer — a scan, a photo, a screenshot."""
    if not X.load_doc(doc_id):
        raise HTTPException(404, "No such document")
    res = X.ocr_page(doc_id, n)
    if res is None:
        raise HTTPException(404, "No such page")
    return res


@app.get("/api/docs/{doc_id}/region/{rid}.png")
def region_png(doc_id: str, rid: str):
    if not re.fullmatch(r"[a-f0-9]{6,20}", rid):
        raise HTTPException(404, "Not found")
    p = os.path.join(X.DOCS_DIR, doc_id, f"region-{rid}.png")
    if not os.path.exists(p):
        raise HTTPException(404, "Not found")
    return FileResponse(p, media_type="image/png")


# --------------------------------------------------------------------------- #
# tutor
# --------------------------------------------------------------------------- #
PROMPTS = {
    "summary": "Give me a summary of this material using the SUMMARY format.",
    "concepts": "List the key concepts I must understand from this material, using the KEY CONCEPTS format.",
    "glossary": "Build a glossary of the important terms in this material using the GLOSSARY format.",
    "quiz": "Quiz me on this material using the QUIZ format. Don't reveal answers until the Answers section.",
    "flashcards": "Make flashcards from this material using the FLASHCARDS format.",
    "explain": None,   # needs the selected text
    "chat": None,
}


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.post("/api/tutor")
async def tutor(request: Request):
    body = await request.json()
    doc = X.load_doc(body.get("doc_id", ""))
    if not doc:
        raise HTTPException(404, "Document not found")

    mode = body.get("mode") or "chat"
    question = (body.get("question") or "").strip()
    history = body.get("history") or []
    provider = body.get("provider")
    api_key = (body.get("api_key") or "").strip()
    model = body.get("model")
    base_url = body.get("base_url")
    key_source = "user" if api_key else None
    over_limit = False
    chain: list[dict] = []
    if api_key:                                       # the visitor brought their own key
        chain = [{"provider": provider, "key": api_key, "model": model,
                  "base_url": base_url, "source": "user"}]
    else:                                             # fall back to the site's free keys
        creds_all = SK.credentials_chain()
        if creds_all:
            ip = request.client.host if request.client else "?"
            if not SK.within_limit(ip):
                over_limit = True
            else:
                chain = [dict(c, source="site") for c in creds_all]
                provider = chain[0]["provider"]
                model = chain[0]["model"] or model
                base_url = chain[0]["base_url"] or base_url
                api_key = chain[0]["key"]
                key_source = "site"
    level = body.get("level") or "student"
    selection = (body.get("selection") or "").strip()
    regions = [r for r in (body.get("regions") or []) if isinstance(r, dict)][:3]
    if not selection and regions:
        selection = " ".join((r.get("text") or "").strip() for r in regions).strip()[:4000]

    limited = over_limit
    limit = CFG.DAILY_LIMIT

    async def _gen():
        if limited:
            yield _sse("meta", {"mode": "offline"})
            yield _sse("delta", {"text": ("> **Daily limit reached** for this site's shared AI key "
                                          f"({limit} answers per day per visitor). Everything below still "
                                          "works from your document.\n\n")})
        # ---- 1. no key -> offline engine ---------------------------------- #
        if not api_key or limited:
            yield _sse("meta", {"mode": "offline"})
            result = _offline_response(mode, question, selection, doc, level)
            for chunk in _as_markdown_chunks(result):
                yield _sse("delta", {"text": chunk})
            yield _sse("meta", {"sources": result.get("sources", [])})
            yield _sse("done", {})
            return

        # ---- 2. LLM path: walk the free-key chain until one answers --------- #
        usable, asleep = KR.order(chain)
        if not usable:                                   # every key is cooling down
            mins = max(1, KR.soonest_wake(chain) // 60)
            yield _sse("meta", {"mode": "offline", "all_resting": True, "retry_minutes": mins})
            yield _sse("delta", {"text": (f"> **All free AI keys are resting** — the daily free quotas are used up. "
                                          f"The first one comes back in about {mins} minute{'s' if mins != 1 else ''} "
                                          "(quotas reset at midnight Pacific). Everything below still works "
                                          "from your document.\n\n")})
            result = _offline_response(mode, question, selection, doc, level)
            for chunk in _as_markdown_chunks(result):
                yield _sse("delta", {"text": chunk})
            yield _sse("meta", {"sources": result.get("sources", [])})
            yield _sse("done", {})
            return
        work = usable + asleep                         # resting keys are only a last resort
        first = work[0]                                  # the healthiest key speaks first
        if first.get("provider") == "local" and not first.get("model"):
            first["model"] = await _first_local_model(first.get("base_url")) or None
        yield _sse("meta", {"mode": "llm", "provider": first.get("provider"),
                            "model": first.get("model") or llm.DEFAULT_MODELS.get(first.get("provider") or "", ""),
                            "key_source": key_source, "attempt": 1,
                            "keys": len(work), "skipped": len(asleep)})
        msgs = [{"role": m["role"], "content": m["content"]} for m in history[-8:]
                if m.get("role") in ("user", "assistant") and m.get("content")]
        emitted = False
        errors: list[str] = []
        for i, cred in enumerate(work):
            cprov, ckey = cred.get("provider"), cred.get("key")
            cmodel = cred.get("model") or None
            cbase = cred.get("base_url") or None
            if cprov == "local" and not cmodel:          # Ollama: pick an installed model
                cmodel = await _first_local_model(cbase) or llm.PROVIDERS["local"]["model"]
            vision = llm.supports_vision(cprov or "", cmodel)
            user_msg, images = _build_turn(mode, question, selection, doc, level, body, regions,
                                           ctx_chars=llm.context_chars(cprov or ""), vision=vision)
            msgs_here = msgs + [{"role": "user", "content": user_msg}]
            if i:
                # tell the learner (and the UI) that a backup key took over
                yield _sse("meta", {"mode": "llm", "provider": cprov,
                                    "model": cmodel or llm.DEFAULT_MODELS.get(cprov or "", ""),
                                    "key_source": cred.get("source", key_source),
                                    "attempt": i + 1, "failover": True,
                                    "failed": errors[-1] if errors else None})
                yield _sse("delta", {"text": f"> Switched to the backup key ({llm.PROVIDERS.get(cprov, {}).get('label', cprov)}) "
                                              f"— the first free key is unavailable right now.\n\n"})
            try:
                async for tok in llm.stream_chat(cprov, ckey, cmodel, msgs_here,
                                                 base_url=cbase, images=images):
                    emitted = True
                    yield _sse("delta", {"text": tok})
                KR.clear(cprov or "", ckey or "")
                yield _sse("done", {})
                return
            except Exception as e:  # noqa: BLE001
                err = str(e)[:300]
                # the model turned out to be text-only → retry the same key on the
                # text layer instead of failing the whole answer
                if not emitted and ("multimodal" in err.lower()
                                    or ("image" in err.lower() and "support" in err.lower())):
                    try:
                        text_msg, _ = _build_turn(mode, question, selection, doc, level, body,
                                                  regions, ctx_chars=llm.context_chars(cprov or ""),
                                                  vision=False)
                        async for tok in llm.stream_chat(
                                cprov, ckey, cmodel,
                                msgs + [{"role": "user", "content": text_msg}],
                                base_url=cbase, images=[]):
                            emitted = True
                            yield _sse("delta", {"text": tok})
                        KR.clear(cprov or "", ckey or "")
                        yield _sse("done", {})
                        return
                    except Exception as e2:  # noqa: BLE001
                        err = str(e2)[:300]
                errors.append(f"{cprov}: {err}")
                if emitted:
                    yield _sse("error", {"message": err})
                    return
                # a local model has no key and no quota — never "rest" it, just report
                if cprov != "local":
                    rest = KR.mark(cprov or "", ckey or "", err)   # remember, don't retry it
                    if i + 1 < len(work):
                        yield _sse("meta", {"resting": {"provider": cprov, "why": rest["why"],
                                                        "minutes": max(1, rest["seconds"] // 60)}})
                continue
        err = errors[-1] if errors else "no key available"
        if True:
            if emitted:
                yield _sse("error", {"message": err})
                return
            # the model call failed before producing anything — still be useful
            why = _friendly_error(err)
            detail = f" `{err}`" if CFG.DEBUG else ""
            yield _sse("delta", {"text": f"> **Tutor unavailable** — {why}. "
                                          f"Answering from your document instead.{detail}\n\n"})
            result = _offline_response(mode, question, selection, doc, level)
            for chunk in _as_markdown_chunks(result):
                yield _sse("delta", {"text": chunk})
            yield _sse("meta", {"mode": "offline", "fell_back": True})
            yield _sse("done", {})

    async def gen():
        """Never let an unexpected error break the stream mid-answer."""
        try:
            async for chunk in _gen():
                yield chunk
        except Exception as e:  # noqa: BLE001
            yield _sse("error", {"message": str(e)[:400]})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})



_local_cache: dict[str, tuple[float, str | None]] = {}


async def _first_local_model(base_url: str | None) -> str | None:
    """If no model was named for a local Ollama, use whatever it has installed."""
    root = (base_url or CFG.SITE_BASE or llm.PROVIDERS["local"]["base"]).strip().rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    hit = _local_cache.get(root)
    if hit and time.time() - hit[0] < 30:
        return hit[1]
    name = None
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            data = (await client.get(f"{root}/api/tags")).json()
        names = [x.get("name") for x in data.get("models", []) if x.get("name")]
        # prefer a bigger general model over tiny ones, unless only one is installed
        name = next((n for n in names if any(t in n for t in ("14b", "8b", "7b", "12b"))), None) \
            or (names[0] if names else None)
    except Exception:  # noqa: BLE001
        name = None
    _local_cache[root] = (time.time(), name)
    return name



def _device_label(ua: str) -> str:
    """A human name for a browser, e.g. "Chrome · Android"."""
    u = (ua or "").lower()
    where = ("iPhone" if "iphone" in u else "iPad" if "ipad" in u
             else "Android" if "android" in u else "Windows" if "windows" in u
             else "Mac" if "mac os" in u or "macintosh" in u
             else "Linux" if "linux" in u else "Unknown")
    app = ("Edge" if "edg/" in u else
           "Chrome" if ("chrome" in u or "crios" in u) and "edg" not in u else
           "Firefox" if "firefox" in u or "fxios" in u else
           "Safari" if "safari" in u else "Browser")
    return f"{app} · {where}"


def _visitor(request) -> dict:
    """Which browser is this? A per-device id the client keeps in localStorage."""
    vid = (request.headers.get("x-sm-visitor") or "").strip().lower()
    if not re.fullmatch(r"[a-f0-9]{8,32}", vid):
        vid = ""
    name = ""
    try:
        name = unquote(request.headers.get("x-sm-device") or "").strip()[:40]
    except Exception:  # noqa: BLE001
        name = ""
    return {"id": vid, "label": name or _device_label(request.headers.get("user-agent", ""))}


def _friendly_error(err: str) -> str:
    """Turn a provider's JSON into something a student can act on."""
    e = (err or "").lower()
    if "multimodal" in e or ("image" in e and "support" in e):
        return "this model cannot read pictures, so it answered from the text instead"
    if "context" in e and ("length" in e or "token" in e or "window" in e):
        return "the selection was too long for this model, so it answered from the document"
    if any(k in e for k in ("connection", "refused", "unreachable", "timeout", "timed out")):
        return "the AI service on this computer did not answer"
    if "quota" in e or "rate limit" in e or "429" in e:
        return "the free quota for this key is used up"
    return "the model call failed"


def _build_turn(mode, question, selection, doc, level, body, regions=None,
                ctx_chars: int = 24000, vision: bool = True) -> tuple[str, list[dict]]:
    regions = regions or []
    ctx_budget = max(1500, min(6000, ctx_chars // 3))
    ctx, used = Index(doc["chunks"]).context(question or PROMPTS.get(mode) or "key points",
                                             k=5, budget=ctx_budget)
    if mode in ("summary", "concepts", "glossary"):
        full, truncated = whole_doc_text(doc, budget=max(4000, min(22000, ctx_chars)))
        ctx = full
    doc_header = (f"DOCUMENT: {doc['filename']}  ({doc['n_pages']} pages / sections, "
                  f"{doc['n_words']} words)\n")
    parts = [doc_header, "\n--- DOCUMENT CONTENT ---\n", ctx, "\n--- END DOCUMENT ---\n"]
    if mode == "flashcards":
        parts.append("\n" + llm.STUDY_FORMATS.split("FLASHCARDS")[0] + "FLASHCARDS: 8 cards as \"Q: ... / A: ... (p.N)\".")
    f = llm.STUDY_FORMATS
    fmt_key = {"summary": "SUMMARY", "concepts": "KEY CONCEPTS", "glossary": "GLOSSARY",
               "quiz": "QUIZ", "flashcards": "FLASHCARDS", "explain": "EXPLAIN"}.get(mode)
    if fmt_key:
        block = f.split("Format instructions:")[1]
        spec = [ln for ln in block.splitlines() if ln.strip().startswith("- " + fmt_key)]
        if spec:
            parts.append("\nFormat required:\n" + spec[0])
    hint = section_hint(doc, question)
    if hint:
        parts.append(f"\nNOTE: the learner refers to section {hint['number']}, which begins on "
                     f"page {hint['page']} (\"{hint['heading']}\"). Focus there unless they say otherwise.")
        span = section_span(doc, hint)
        if span and len(span["text"]) > 120:
            parts.append(f"\n--- THAT SECTION (p.{span['page']}) ---\n{span['text']}\n--- END SECTION ---")
    for rg in regions:
        parts.append(f"\nTHE LEARNER SELECTED THIS REGION OF PAGE {rg.get('page')} "
                     + ("(a crop of that area is attached as an image):" if vision
                        else "(you cannot see the picture; this is its text):")
                     + f"\n\"\"\"\n"
                     f"{(rg.get('text') or ('[no text layer — read the attached crop]' if vision
                                           else '[this area has no text layer, so it cannot be read by a text-only model — say what you can infer from the surrounding page instead]'))[:3000]}"
                     f"\n\"\"\"\n")
    if selection and not regions:
        parts.append(f"\nTHE LEARNER SELECTED THIS PASSAGE:\n\"\"\"\n{selection[:4000]}\n\"\"\"\n")
    if question:
        parts.append(f"\nTHE LEARNER ASKS: {question}")
    elif mode == "explain":
        parts.append("\nTASK: Explain the selected passage in plain language (EXPLAIN format).")
    else:
        parts.append(f"\nTASK: {PROMPTS.get(mode) or 'Help me study this material.'}")
    if level and level != "student":
        parts.append(f"\nCalibrate the explanation for a {level} audience.")
    parts.append("\nReply in Markdown. Cite pages as (p.N).")

    images = []
    for rg in (regions if vision else []):
        rid = str(rg.get("region_id") or "")
        if not re.fullmatch(r"[a-f0-9]{6,20}", rid):
            continue
        rp = os.path.join(X.DOCS_DIR, doc["id"], f"region-{rid}.png")
        if os.path.exists(rp) and os.path.getsize(rp) < 6 * 1024 * 1024:
            images.append({"mime": "image/png",
                           "data": base64.b64encode(open(rp, "rb").read()).decode()})
    if vision and doc["kind"] == "image" and not images:
        sp = X.source_path(doc["id"])
        if sp:
            mime = mimetypes.guess_type(sp)[0] or "image/png"
            raw = open(sp, "rb").read()
            if len(raw) < 8 * 1024 * 1024:
                images.append({"mime": mime, "data": base64.b64encode(raw).decode()})
    return "".join(parts), images


def _offline_response(mode, question, selection, doc, level) -> dict:
    if mode == "summary":
        r = offline.summarize(doc, 8)
        md = ["### Summary", "", r["note"], ""]
        for b in r["bullets"]:
            md.append(f"- {b['text']} (p.{b['page']})")
        return {"markdown": "\n".join(md), "sources": [{"page": b["page"]} for b in r["bullets"][:5]]}
    if mode == "concepts":
        r = offline.key_concepts(doc, 12)
        md = ["### Key concepts", ""]
        for i, t in enumerate(r["terms"], 1):
            md.append(f"**{i}. {t['term'].title()}** — appears on p.{t['page']}: _{t['context'][:220]}_")
            md.append("")
        return {"markdown": "\n".join(md)}
    if mode == "glossary":
        r = offline.glossary(doc, 10)
        md = ["### Glossary", "", "| Term | In your material |", "| --- | --- |"]
        for e in r["entries"]:
            md.append(f"| **{e['term']}** | {e['definition']['text'][:220]} (p.{e['definition']['page']}) |")
        return {"markdown": "\n".join(md)}
    if mode == "quiz":
        r = offline.quiz(doc, 5)
        if not r["questions"]:
            return {"markdown": "_Not enough text to generate a quiz. Add an API key for a written quiz._"}
        md = ["### Quiz (auto-generated)", ""]
        for i, q in enumerate(r["questions"], 1):
            md.append(f"**Q{i}.** {q['question']}")
            for j, o in enumerate(q["options"]):
                md.append(f"- {'ABCD'[j]}) {o}")
            md.append("")
        md += ["---", "**Answers**", ""]
        for i, q in enumerate(r["questions"], 1):
            md.append(f"- Q{i}: **{q['answer']}** (p.{q['page']})")
        return {"markdown": "\n".join(md)}
    if mode == "flashcards":
        r = offline.flashcards(doc, 8)
        md = ["### Flashcards", ""]
        for i, c in enumerate(r["cards"], 1):
            md.append(f"**{i}. {c['front']}**")
            md.append(f"{c['back']} (p.{c['page']})")
            md.append("")
        return {"markdown": "\n".join(md)}
    if (mode == "explain" or selection) and not question:
        r = offline.explain(selection or question, doc, level)
        md = ["### What the document says", "", f"_{r['note']}_", ""]
        for s in r["related"]:
            md.append(f"> {s['text']}\n>\n> — p.{s['page']}")
            md.append("")
        return {"markdown": "\n".join(md),
                "sources": [{"page": s["page"]} for s in r["related"]]}
    r = offline.answer(question, doc, focus=selection)
    md = [r["answer"]]
    if r.get("note"):
        md += ["", f"_{r['note']}_"]
    return {"markdown": "\n".join(md),
            "sources": [{"page": s.get("page"), "text": (s.get("text") or "")[:300]}
                        for s in r.get("sources", [])]}


def _as_markdown_chunks(result: dict, size: int = 60):
    md = result.get("markdown", "")
    for i in range(0, len(md), size):
        yield md[i:i + size]


# --------------------------------------------------------------------------- #
# settings / health
# --------------------------------------------------------------------------- #
@app.post("/api/verify-key")
async def verify_key(request: Request):
    body = await request.json()
    key = (body.get("api_key") or "").strip()
    if not key:
        raise HTTPException(400, "No key")
    return await llm.verify_key(body.get("provider") or "openai", key,
                               body.get("model"), body.get("base_url"))


@app.on_event("startup")
def _startup():
    try:
        import backup as BK
        BK.restore_on_boot()             # pull the last snapshot if this disk is empty
    except Exception:  # noqa: BLE001
        pass
    try:
        _ws_backfill()
    except Exception:  # noqa: BLE001
        pass
    try:
        import backup as BK
        BK.start_watcher()               # push a snapshot after changes (free hosts are ephemeral)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# AI key plumbing
# --------------------------------------------------------------------------- #
@app.get("/api/providers")
def providers():
    """Catalogue the settings panel renders — free tiers first."""
    out = []
    for pid, p in llm.PROVIDERS.items():
        out.append({"id": pid, "label": p["label"], "kind": p["kind"], "model": p["model"],
                    "base_url": p["base"], "signup": p["signup"], "free": bool(p.get("free")),
                    "note": p.get("note", "")})
    return {"providers": out}


@app.get("/api/local-models")
async def local_models(base: str | None = None):
    """Which models are installed in the Ollama running next to this app?

    Saves the learner from typing model names by hand — Settings fills a picklist
    from this. Fails quietly when Ollama is not running.
    """
    root = (base or CFG.SITE_BASE or llm.PROVIDERS["local"]["base"] or "").strip().rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            r = await client.get(f"{root}/api/tags")
            r.raise_for_status()
            data = r.json()
        models = [m.get("name") for m in data.get("models", []) if m.get("name")]
        size = sum(int(m.get("size") or 0) for m in data.get("models", [])) / 1e9
        return {"ok": True, "models": models, "count": len(models),
                "gigabytes": round(size, 1), "base": root}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "models": [], "count": 0, "error": str(e)[:200], "base": root}


@app.get("/api/ai-status")
def ai_status(request: Request):
    """Does this site bring its own key? (Never returns the key.)"""
    st = SK.status()
    try:
        st["remaining"] = SK.usage_left(request.client.host if request.client else "?")
    except Exception:  # noqa: BLE001
        st["remaining"] = None
    try:                                    # per-key health: ready vs resting (never the key)
        st["keys"] = KR.summary(SK.credentials_chain())
    except Exception:  # noqa: BLE001
        st["keys"] = []
    return st


@app.post("/api/site-key")
async def site_key_save(request: Request):
    body = await request.json()
    try:
        return SK.save(body.get("api_key"), body.get("provider") or "gemini",
                       body.get("model"), body.get("base_url"),
                       body.get("passcode"), body.get("new_passcode"),
                       backups=body.get("backups") or [])
    except PermissionError as e:
        raise HTTPException(403, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/site-key/clear")
async def site_key_clear(request: Request):
    body = await request.json()
    try:
        return SK.clear(body.get("passcode"))
    except PermissionError as e:
        raise HTTPException(403, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/site-key/test")
async def site_key_test(request: Request):
    """Try the stored site key against the provider before anyone relies on it."""
    creds = SK.credentials()
    if not creds:
        raise HTTPException(404, "No site key configured")
    return await llm.verify_key(creds["provider"], creds["key"], creds["model"], creds["base_url"])


# ---- PWA plumbing (root-scoped so the service worker can control every page) ----
@app.get("/sw.js")
def service_worker():
    return FileResponse(os.path.join(STATIC, "sw.js"), media_type="application/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(os.path.join(STATIC, "manifest.webmanifest"),
                        media_type="application/manifest+json",
                        headers={"Cache-Control": "public, max-age=3600"})


@app.get("/favicon.ico")
def favicon():
    return FileResponse(os.path.join(STATIC, "icons", "favicon-32.png"),
                        media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/healthz")
@app.get("/api/health")
def health():
    try:
        uploads = len(os.listdir(X.DOCS_DIR))
    except OSError:
        uploads = -1
    return {"ok": True, "time": time.time(), "docs": uploads,
            "storage": str(X.DATA_DIR), "backup": bool(getattr(CFG, "BACKUP_ENABLED", False))}
