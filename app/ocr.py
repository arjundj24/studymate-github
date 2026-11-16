"""Read text out of pictures.

Screenshots, phone photos of notes, scanned pages and diagram crops have no text
layer — a text-only model sees nothing. This module fills that gap with OCR.

Two engines are supported, both optional:
  * rapidocr-onnxruntime  (fast, bundled models, ~90 MB, no system packages)
  * pytesseract           (needs the Tesseract binary installed)

If neither is installed the rest of the app still works; callers just get "".
Set STUDYMATE_OCR=0 to turn OCR off entirely (useful on tiny cloud instances).
"""

from __future__ import annotations

import os
import threading

_LOCK = threading.Lock()
_ENGINE = None          # ("rapidocr", obj) | ("tesseract", None) | ("none", None)
_LOADED = False


def enabled() -> bool:
    return os.environ.get("STUDYMATE_OCR", "1").strip().lower() not in ("0", "false", "off", "no")


def _load():
    """Import an engine once, lazily — importing onnxruntime costs ~1s."""
    global _ENGINE, _LOADED
    if _LOADED:
        return _ENGINE
    with _LOCK:
        if _LOADED:
            return _ENGINE
        if not enabled():
            _ENGINE = ("none", None)
        else:
            try:
                from rapidocr_onnxruntime import RapidOCR  # type: ignore

                _ENGINE = ("rapidocr", RapidOCR())
            except Exception:  # noqa: BLE001 — not installed, or model files missing
                try:
                    import pytesseract  # type: ignore  # noqa: F401

                    _ENGINE = ("tesseract", None)
                except Exception:  # noqa: BLE001
                    _ENGINE = ("none", None)
        _LOADED = True
    return _ENGINE


def available() -> bool:
    return _load()[0] != "none"


def engine_name() -> str:
    return {"rapidocr": "rapidocr", "tesseract": "tesseract"}.get(_load()[0], "")


def read_text(path: str, max_chars: int = 12000) -> str:
    """OCR an image file and return its text, or "" if OCR is unavailable."""
    if not path or not os.path.exists(path):
        return ""
    kind, engine = _load()
    if kind == "none":
        return ""
    try:
        if kind == "rapidocr":
            result, _ = engine(path)                      # (boxes, elapsed)
            if not result:
                return ""
            lines = []
            for item in result:
                # item = [box(4 points), text, score]
                try:
                    text = str(item[1]).strip()
                    score = float(item[2])
                except (IndexError, TypeError, ValueError):
                    continue
                if text and score >= 0.35:
                    lines.append(text)
            out = "\n".join(lines)
        else:
            from PIL import Image
            import pytesseract  # type: ignore

            with Image.open(path) as im:
                out = pytesseract.image_to_string(im.convert("RGB"))
        out = "\n".join(ln.strip() for ln in out.splitlines() if ln.strip())
        return out[:max_chars]
    except Exception:  # noqa: BLE001 — never let OCR break an upload
        return ""


def read_text_bytes(raw: bytes, suffix: str = ".png", max_chars: int = 12000) -> str:
    """OCR raw image bytes (used when the source file is a PDF page render)."""
    import tempfile

    tmp = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as f:
            f.write(raw)
            tmp = f.name
        return read_text(tmp, max_chars=max_chars)
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
