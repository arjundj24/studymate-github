"""
Document extraction: turn any uploaded material into a uniform structure.

    Doc = {
      id, filename, ext, kind, pages[], chunks[], n_pages, n_words, ...
    }

Each page holds the plain text the tutor reasons over, plus enough info for
the reader panel to render it (server-rendered PNG for PDFs, a table for
spreadsheets, markdown/plain text for everything else).
"""
from __future__ import annotations

import csv
import html
import io
import json
import os
import re
import time
import uuid

try:                                     # env-driven when deployed, ./data locally
    from config import DATA_DIR, DOCS_DIR   # type: ignore
except Exception:                        # noqa: BLE001
    DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    DOCS_DIR = os.path.join(DATA_DIR, "docs")
DATA_DIR = str(DATA_DIR)
DOCS_DIR = str(DOCS_DIR)

RENDER_ZOOM = 2.0          # PDF pages are rasterised at 2x for display
CHUNK_CHARS = 1100
CHUNK_OVERLAP = 180
FLOW_PAGE_CHARS = 2600

TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".tex", ".json", ".yaml", ".yml",
    ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".c", ".h", ".cpp", ".cs", ".go",
    ".rs", ".rb", ".php", ".sql", ".sh", ".html", ".css", ".ipynb", ".xml",
}
CODE_EXTS = {".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".c", ".h", ".cpp", ".cs",
             ".go", ".rs", ".rb", ".php", ".sql", ".sh", ".html", ".css", ".xml", ".json", ".ipynb"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
SHEET_EXTS = {".csv", ".tsv", ".xlsx", ".xls"}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _read_text(path: str) -> str:
    raw = open(path, "rb").read()
    for enc in ("utf-8", "utf-16", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _split_sentences(text: str) -> list[str]:
    """Sentence-ish units, respecting paragraph boundaries.

    Paragraphs are kept separate so a heading never gets glued onto the sentence
    after it (that used to leak "3. Loss, gradients" into quotations).
    """
    text = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", text or "")   # repair PDF hyphenation
    out: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        para = re.sub(r"\s+", " ", para).strip()
        if not para:
            continue
        parts = re.split(r"(?<=[.!?])[\"')\]]?\s+(?=[A-Z0-9\"'(])", para)
        buf = ""
        for p in parts:
            buf = (buf + " " + p).strip() if buf else p
            if len(buf) >= 30:
                out.append(buf)
                buf = ""
        if buf:
            out.append(buf)
    return out


def _chunk_pages(pages: list[dict]) -> list[dict]:
    """Split page text into sentence-aligned chunks that remember their page.

    Paragraph breaks survive inside a chunk, so retrieval returns readable prose.
    """
    chunks = []
    for page in pages:
        text = page.get("text") or ""
        if not text.strip():
            continue
        chunks_for_page: list[str] = []
        buf, prev_para = "", -1
        for pi, para in enumerate(re.split(r"\n\s*\n", text)):
            for s in _split_sentences(para):
                if len(buf) + len(s) + 2 > CHUNK_CHARS and buf:
                    chunks_for_page.append(buf.strip())
                    tail = buf[-CHUNK_OVERLAP:]
                    if " " in tail:
                        tail = tail.split(" ", 1)[1]      # start on a word boundary
                    m = re.search(r"[.!?]\s", tail)
                    if m:
                        tail = tail[m.end():]             # …and on a sentence boundary
                    buf, prev_para = (tail + " " + s), pi
                else:
                    if buf:
                        buf += ("\n\n" if pi != prev_para else " ") + s
                    else:
                        buf, prev_para = s, pi
        if buf.strip():
            chunks_for_page.append(buf.strip())
        for c in chunks_for_page:
            chunks.append({"page": page["n"], "text": c})
    for i, c in enumerate(chunks):
        c["id"] = i
    return chunks


def _paginate_flow(text: str, label: str = "Part") -> list[dict]:
    """Break a flowing document into pseudo-pages at paragraph boundaries."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    pages, buf, n = [], "", 1
    for p in paras:
        if len(buf) + len(p) + 2 > FLOW_PAGE_CHARS and buf:
            pages.append({"n": n, "label": f"{label} {n}", "text": buf.strip()})
            buf, n = p, n + 1
        else:
            buf = f"{buf}\n\n{p}" if buf else p
    if buf.strip():
        pages.append({"n": n, "label": f"{label} {n}", "text": buf.strip()})
    return pages or [{"n": 1, "label": f"{label} 1", "text": text}]


# --------------------------------------------------------------------------- #
# per-format extractors
# --------------------------------------------------------------------------- #
def _pdf_page_text(page) -> str:
    """Rebuild reading order paragraph text from text blocks (line breaks within a
    block are artefacts of the PDF layout, not of the writing)."""
    paras = []
    for block in page.get_text("blocks"):
        raw = block[4] if len(block) > 4 else ""
        lines = [ln.strip() for ln in str(raw).split("\n") if ln.strip()]
        buf = ""
        for ln in lines:
            if buf.endswith("-") and not buf.endswith("--"):
                buf = buf[:-1] + ln            # repair hyphenated line break
            else:
                buf = (buf + " " + ln).strip() if buf else ln
        if buf:
            paras.append(re.sub(r"\s+", " ", buf).strip())
    return "\n\n".join(paras) if paras else page.get_text("text")


def _extract_pdf(path: str, doc_id: str) -> dict:
    import pymupdf as fitz

    pdf = fitz.open(path)
    pages = []
    for i, page in enumerate(pdf):
        text = _pdf_page_text(page)
        pages.append({"n": i + 1, "label": f"Page {i + 1}", "text": text})
    meta = {"kind": "paged", "paged": True, "n_pages": len(pages)}
    pdf.close()
    return {"pages": pages, **meta}


def _extract_docx(path: str) -> dict:
    import docx

    d = docx.Document(path)
    lines = [block for block in _iter_docx_blocks(d) if block and block.strip()]
    text = "\n\n".join(lines)
    return {"pages": _paginate_flow(text, "Part"), "kind": "flowing", "paged": False}


def _iter_docx_blocks(d):
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    body = d.element.body
    for child in body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            p = Paragraph(child, d)
            t = p.text.strip()
            if not t:
                continue
            style = (p.style.name or "").lower()
            if style.startswith("heading"):
                level = "".join(ch for ch in style if ch.isdigit()) or "1"
                t = f"{'#' * min(int(level), 6)} {t}"
            yield t
        elif tag == "tbl":
            tbl = Table(child, d)
            rows = []
            for r in tbl.rows:
                rows.append(" | ".join(c.text.strip().replace("\n", " ") for c in r.cells))
            if rows:
                yield "\n".join(rows)


def _extract_pptx(path: str) -> dict:
    from pptx import Presentation

    prs = Presentation(path)
    pages = []
    for i, slide in enumerate(prs.slides):
        buf = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                t = shape.text_frame.text.strip()
                if t:
                    buf.append(t)
            if getattr(shape, "has_table", False):
                for r in shape.table.rows:
                    buf.append(" | ".join(c.text.strip() for c in r.cells))
        notes = ""
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            notes = slide.notes_slide.notes_text_frame.text.strip()
        text = "\n\n".join(buf)
        if notes:
            text += "\n\n[Speaker notes] " + notes
        pages.append({"n": i + 1, "label": f"Slide {i + 1}", "text": text})
    return {"pages": pages, "kind": "slides", "paged": True}


def _extract_sheet(path: str, ext: str) -> dict:
    pages = []
    if ext == ".xlsx":
        import openpyxl

        wb = openpyxl.load_workbook(path, data_only=True)
        for idx, ws in enumerate(wb.worksheets):
            rows = []
            for row in ws.iter_rows(values_only=True):
                cells = ["" if c is None else str(c) for c in row]
                if any(c.strip() for c in cells):
                    rows.append(cells)
            pages.append({
                "n": idx + 1,
                "label": ws.title or f"Sheet {idx + 1}",
                "text": "\n".join(" | ".join(r) for r in rows),
                "table": rows,
            })
    else:
        raw = _read_text(path)
        delim = "\t" if ext == ".tsv" else ","
        rows = list(csv.reader(io.StringIO(raw), delimiter=delim))
        rows = [r for r in rows if any(str(c).strip() for c in r)]
        pages.append({
            "n": 1, "label": "Sheet", "text": "\n".join(" | ".join(r) for r in rows),
            "table": rows,
        })
    return {"pages": pages, "kind": "sheet", "paged": False}


def _extract_text(path: str, ext: str) -> dict:
    text = _read_text(path)
    kind = "code" if ext in CODE_EXTS else "text"
    return {"pages": _paginate_flow(text, "Part"), "kind": kind, "paged": False}


def _extract_image(path: str, ext: str) -> dict:
    """Photos and screenshots have no text layer, so the words have to be read."""
    import ocr

    text = ocr.read_text(path)
    return {
        "pages": [{"n": 1, "label": "Page 1" if text else "Image",
                   "text": text, "image_only": not text, "ocr": bool(text)}],
        "kind": "image",
        "paged": False,
    }


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def extract(path: str, filename: str) -> dict:
    ext = os.path.splitext(filename)[1].lower()
    if ext == ".pdf":
        base = _extract_pdf(path, "")
    elif ext == ".docx":
        base = _extract_docx(path)
    elif ext in (".pptx", ".ppt"):
        base = _extract_pptx(path)
    elif ext in SHEET_EXTS:
        base = _extract_sheet(path, ext)
    elif ext in IMAGE_EXTS:
        base = _extract_image(path, ext)
    else:
        base = _extract_text(path, ext)

    pages = base["pages"]
    if not base.get("paged"):
        for p in pages:
            p.setdefault("label", f"Part {p['n']}")
    chunks = _chunk_pages(pages)
    words = sum(len((p.get("text") or "").split()) for p in pages)
    return {
        "ext": ext,
        "kind": base["kind"],
        "paged": base.get("paged", False),
        "pages": pages,
        "chunks": chunks,
        "n_pages": len(pages),
        "n_words": words,
        "n_chars": sum(len(p.get("text") or "") for p in pages),
    }


def new_doc_id() -> str:
    return uuid.uuid4().hex[:12]


def save_doc(doc_id: str, filename: str, raw_bytes: bytes, extracted: dict,
             extra: dict | None = None) -> dict:
    ddir = os.path.join(DOCS_DIR, doc_id)
    os.makedirs(ddir, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", filename)[-120:] or "file"
    with open(os.path.join(ddir, "source" + (os.path.splitext(filename)[1].lower() or "")), "wb") as f:
        f.write(raw_bytes)
    meta = {
        "id": doc_id,
        "filename": filename,
        "safe_name": safe,
        "created": time.time(),
        **(extra or {}),
        **{k: v for k, v in extracted.items()},
    }
    # pages keep their full text; chunks are what retrieval/tutor use
    with open(os.path.join(ddir, "doc.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f)
    return meta


def load_doc(doc_id: str) -> dict | None:
    if not re.fullmatch(r"[a-f0-9]{6,32}", doc_id):
        return None
    p = os.path.join(DOCS_DIR, doc_id, "doc.json")
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def page_image(doc_id: str, n: int, zoom: float = RENDER_ZOOM) -> str | None:
    """Render a PDF page to PNG (cached) and return the path."""
    ddir = os.path.join(DOCS_DIR, doc_id)
    cache = os.path.join(ddir, f"page-{n}@{zoom}.png")
    if os.path.exists(cache):
        return cache
    srcs = [f for f in os.listdir(ddir) if f.startswith("source.")]
    if not srcs or not srcs[0].lower().endswith(".pdf"):
        return None
    import pymupdf as fitz

    with fitz.open(os.path.join(ddir, srcs[0])) as pdf:
        if n < 1 or n > len(pdf):
            return None
        pix = pdf[n - 1].get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        pix.save(cache)
    return cache


def page_words(doc_id: str, n: int) -> dict | None:
    """Word boxes for one PDF page, normalised to 0..1 so the text layer scales with the image."""
    src = source_path(doc_id)
    if not src or not src.lower().endswith(".pdf"):
        return None
    ddir = os.path.dirname(src)
    cache = os.path.join(ddir, f"words-{n}.json")
    if os.path.exists(cache):
        try:
            with open(cache, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    import pymupdf as fitz

    with fitz.open(src) as pdf:
        if n < 1 or n > len(pdf):
            return None
        page = pdf[n - 1]
        rect = page.rect
        w, h = rect.width or 1, rect.height or 1
        words = []
        for x0, y0, x1, y1, text, *_ in page.get_text("words"):
            if not text.strip():
                continue
            words.append([round(x0 / w, 5), round(y0 / h, 5),
                          round(max(x1 - x0, 0.8) / w, 5), round(max(y1 - y0, 0.8) / h, 5),
                          text])
    out = {"w": round(w, 2), "h": round(h, 2), "words": words}
    try:
        with open(cache, "w", encoding="utf-8") as f:
            json.dump(out, f)
    except OSError:
        pass
    return out


def source_path(doc_id: str) -> str | None:
    ddir = os.path.join(DOCS_DIR, doc_id)
    if not os.path.isdir(ddir):
        return None
    for f in os.listdir(ddir):
        if f.startswith("source."):
            return os.path.join(ddir, f)
    return None


def doc_outline(doc: dict) -> list[dict]:
    """Cheap outline: detected headings in flowing docs, labels otherwise."""
    out = []
    for p in doc["pages"]:
        t = (p.get("text") or "").strip()
        first = t.split("\n")[0][:90] if t else p.get("label", "")
        first = re.sub(r"^#+\s*", "", first)
        out.append({"n": p["n"], "label": p.get("label", f"Page {p['n']}"), "title": first})
    return out


def _ocr_fill(png_path: str, existing: str = "") -> str:
    """Try OCR and merge it with whatever the text layer already had."""
    try:
        import ocr
    except Exception:  # noqa: BLE001
        return existing
    got = ocr.read_text(png_path, max_chars=8000)
    if not got:
        return existing
    if not existing:
        return got
    return (existing + "\n" + got)[:12000]


def ocr_page(doc_id: str, n: int) -> dict | None:
    """Read the words out of a page that has no text layer (a scan or a photo-PDF).

    The result is cached inside doc.json so it is paid for once, and the page
    becomes searchable and answerable like any other.
    """
    doc = load_doc(doc_id)
    if not doc:
        return None
    pages = doc.get("pages") or []
    page = next((p for p in pages if int(p.get("n", 0)) == int(n)), None)
    if page is None:
        return None
    if (page.get("text") or "").strip() and not page.get("image_only"):
        return {"n": n, "text": page["text"], "cached": True}

    import ocr

    if not ocr.available():
        return {"n": n, "text": "", "error": "OCR is not installed on this computer"}

    ddir = os.path.join(DOCS_DIR, doc_id)
    srcs = [f for f in os.listdir(ddir) if f.startswith("source.")] if os.path.isdir(ddir) else []
    if not srcs:
        return None
    src = os.path.join(ddir, srcs[0])
    ext = os.path.splitext(src)[1].lower()

    text = ""
    png = os.path.join(ddir, f"ocr-{n}.png")
    try:
        if ext == ".pdf":
            import pymupdf as fitz

            with fitz.open(src) as pdf:
                if n < 1 or n > len(pdf):
                    return None
                pix = pdf[n - 1].get_pixmap(matrix=fitz.Matrix(RENDER_ZOOM, RENDER_ZOOM))
                pix.save(png)
            text = ocr.read_text(png, max_chars=20000)
        else:
            text = ocr.read_text(src, max_chars=20000)
    finally:
        if os.path.exists(png):
            try:
                os.remove(png)
            except OSError:
                pass

    if not text:
        return {"n": n, "text": "", "error": "No readable text found in that image"}

    page["text"] = text
    page["ocr"] = True
    page["image_only"] = False
    doc["pages"] = pages
    doc["chunks"] = _chunk_pages(pages)
    doc["n_words"] = sum(len((p.get("text") or "").split()) for p in pages)
    with open(os.path.join(ddir, "doc.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f)
    return {"n": n, "text": text, "words": len(text.split())}


def region_crop(doc_id: str, n: int, rect_px: list[float]) -> dict | None:
    """Crop a rectangle the learner dragged on page `n`.

    rect_px is in *rendered image pixels* (RENDER_ZOOM x points), so divide to
    get PDF points. Returns {text, path, w, h}; text may be empty for scans.
    """
    ddir = os.path.join(DOCS_DIR, doc_id)
    if not os.path.isdir(ddir):
        return None
    srcs = [f for f in os.listdir(ddir) if f.startswith("source.")]
    if not srcs:
        return None
    src = os.path.join(ddir, srcs[0])
    ext = os.path.splitext(src)[1].lower()
    x0, y0, x1, y1 = [float(v) for v in rect_px]
    x0, x1 = sorted((x0, x1))
    y0, y1 = sorted((y0, y1))
    rid = uuid.uuid4().hex[:10]
    out = os.path.join(ddir, f"region-{rid}.png")

    if ext == ".pdf":
        import pymupdf as fitz

        with fitz.open(src) as pdf:
            if n < 1 or n > len(pdf):
                return None
            page = pdf[n - 1]
            z = RENDER_ZOOM
            clip = fitz.Rect(x0 / z, y0 / z, x1 / z, y1 / z) & page.rect
            if clip.is_empty:
                return None
            text = page.get_text("text", clip=clip).strip()
            pix = page.get_pixmap(matrix=fitz.Matrix(z, z), clip=clip)
            pix.save(out)
            if len(text) < 15:                      # a diagram or a scanned area
                text = _ocr_fill(out, text)
            return {"text": text, "path": out, "w": pix.width, "h": pix.height}

    if ext in IMAGE_EXTS or ext in (".tif", ".tiff"):
        try:
            from PIL import Image
        except ImportError:  # pragma: no cover
            return None
        im = Image.open(src)
        box = (int(max(0, x0)), int(max(0, y0)),
               int(min(im.width, x1)), int(min(im.height, y1)))
        if box[2] - box[0] < 4 or box[3] - box[1] < 4:
            return None
        crop = im.crop(box)
        crop.save(out)
        im.close()
        return {"text": _ocr_fill(out), "path": out, "w": crop.width, "h": crop.height}

    return None
