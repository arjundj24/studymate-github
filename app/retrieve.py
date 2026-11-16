"""Retrieval over document chunks: BM25, keyword search, and context assembly."""
from __future__ import annotations

import math
import re
from collections import Counter

STOP = set("""a about above after again against all am an and any are aren't as at be because been
before being below between both but by can cannot could couldn't did didn't do does doesn't doing
don't down during each few for from further had hadn't has hasn't have haven't having he her here
hers herself him himself his how i if in into is isn't it its itself let's me more most mustn't my
myself no nor not of off on once only or other ought our ours ourselves out over own same shan't she
should shouldn't so some such than that the their theirs them themselves then there these they this
those through to too under until up very was wasn't we were weren't what when where which while who
whom why with won't would wouldn't you your yours yourself yourselves also may might must shall will
one two three thus therefore however e.g i.e etc fig figure table eq equation et al pp see ref using
used use based show shows shown given can also within without upon among across per""".split())

TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'\-]{1,}|[0-9]+(?:\.[0-9]+)?")


def tokenize(text: str, drop_stop: bool = True) -> list[str]:
    toks = [t.lower().strip("'-\"") for t in TOKEN_RE.findall(text or "")]
    toks = [t for t in toks if len(t) > 1]
    if drop_stop:
        toks = [t for t in toks if t not in STOP]
    return toks


def sentences(text: str) -> list[str]:
    """Sentence-ish units, paragraph aware (see extract._split_sentences)."""
    text = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", text or "")
    out = []
    for para in re.split(r"\n\s*\n|\n", text):
        para = re.sub(r"\s+", " ", para).strip()
        if len(para) > 25:
            for p in re.split(r"(?<=[.!?])[\"')\]]?\s+", para):
                p = p.strip()
                # drop headings and other line fragments: real prose ends in punctuation
                if len(p) > 25 and (p[-1] in ".!?:" or len(p) > 80):
                    out.append(p)
    return out



class Index:
    """Inverted index + BM25 over a document's chunks."""

    def __init__(self, chunks: list[dict]):
        self.chunks = chunks
        self.docs = [tokenize(c["text"]) for c in chunks]
        self.lens = [len(d) for d in self.docs]
        self.avg = (sum(self.lens) / len(self.lens)) if self.lens else 1.0
        self.df: Counter = Counter()
        for d in self.docs:
            for term in set(d):
                self.df[term] += 1
        self.N = len(chunks)
        self.tf = [Counter(d) for d in self.docs]

    def search(self, query: str, k: int = 5) -> list[dict]:
        q = tokenize(query)
        if not q or not self.N:
            return []
        k1, b = 1.4, 0.72
        scores = []
        for i, tf in enumerate(self.tf):
            s = 0.0
            for term in q:
                f = tf.get(term, 0)
                if not f:
                    continue
                idf = math.log(1 + (self.N - self.df[term] + 0.5) / (self.df[term] + 0.5))
                s += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * self.lens[i] / self.avg))
            if s > 0:
                scores.append((s, i))
        scores.sort(reverse=True)
        out = []
        for s, i in scores[:k]:
            c = self.chunks[i]
            out.append({"score": round(s, 3), "page": c["page"], "chunk": i, "text": c["text"]})
        return out

    def keyword_search(self, query: str, limit: int = 40) -> list[dict]:
        """Literal-ish search for the reader's find box: returns page + snippet."""
        q = (query or "").strip()
        if not q:
            return []
        pat = re.compile(re.escape(q), re.I)
        hits = []
        for c in self.chunks:
            text = c["text"]
            for m in pat.finditer(text):
                a, b = max(0, m.start() - 70), min(len(text), m.end() + 90)
                snippet = re.sub(r"\s+", " ", text[a:b]).strip()
                hits.append({"page": c["page"], "snippet": snippet, "start": a, "end": b})
                break
            if len(hits) >= limit:
                break
        return hits

    def context(self, query: str, k: int = 4, budget: int = 6000) -> tuple[str, list[dict]]:
        """Assemble a grounded context block for the tutor prompt."""
        hits = self.search(query, k=k)
        if not hits:
            hits = [{"page": c["page"], "text": c["text"], "chunk": i}
                    for i, c in enumerate(self.chunks[:k])]
        parts, used, total, seen = [], [], 0, set()
        for h in hits:
            if h["chunk"] in seen:
                continue
            seen.add(h["chunk"])
            t = h["text"].strip()
            if total + len(t) > budget and parts:
                break
            parts.append(f"[p.{h['page']}] {t}")
            used.append({"page": h["page"], "text": t})
            total += len(t)
        return ("\n\n".join(parts) if parts else "[no extractable text in this document]"), used


SECTION_RE = re.compile(
    r"\b(?:section|chapter|part|step|slide|page|p\.?|unit|topic|lecture)\s*#?\s*(\d{1,3})\b", re.I)

HEADING_START = re.compile(r"^\s*(?:#{1,6}\s*|\*\*)?(?:section\s+|chapter\s+|part\s+)?\d{1,3}\s*[.):\-]\s+\S", re.I)


def _heading_score(line: str) -> float:
    """How much a line looks like a section heading rather than a list item."""
    s = 0.0
    if re.match(r"^\s*#{1,6}\s", line):
        s += 2.0                       # markdown heading
    if re.match(r"^\s*\*\*", line):
        s += 0.8
    if 3 <= len(line) <= 70:
        s += 1.0                       # headings are short
    if len(line) > 110:
        s -= 1.5                       # a wrapped paragraph line, not a heading
    if line.rstrip().endswith((".", ",", ";", ":")) and not re.match(r"^\s*\d+[.):]", line):
        s -= 0.8
    if re.match(r"^\s*\d+\s*[.)]\s", line) and len(line) < 45:
        s += 0.3                       # "4. Optimisation in practice"
    return s


def section_hint(doc: dict, question: str) -> dict | None:
    """If the question names a section/chapter/page, find where that starts.

    Returns {"number": n, "page": page_no, "heading": line} or None.
    """
    m = SECTION_RE.search(question or "")
    if not m:
        return None
    n = m.group(1)
    patterns = [
        re.compile(rf"^\s*#{{1,6}}\s*{n}\s*[.):\-]?\s+\S"),
        re.compile(rf"^\s*\**\s*(?:section|chapter|part|step|slide|unit|topic)\s*{n}\b", re.I),
        re.compile(rf"^\s*\**\s*{n}\s*[.):\-]\s+\S"),
        re.compile(rf"^\s*\**\s*{n}\s+[A-Z]"),
    ]
    best, best_score = None, -99.0
    for page in doc["pages"]:
        for line in (page.get("text") or "").split("\n"):
            line = line.strip()
            if not line or not any(p.match(line) for p in patterns):
                continue
            score = _heading_score(line)
            if score > best_score:
                best, best_score = {"number": int(n), "page": page["n"],
                                    "heading": line[:120]}, score
        if best_score >= 3.0:            # strong heading match — stop looking
            break
    return best


def section_span(doc: dict, hint: dict, budget: int = 4500) -> dict | None:
    """The text of the named section: from its heading to the next heading."""
    if not hint:
        return None
    pages = doc["pages"]
    bi = next((i for i, p in enumerate(pages) if p["n"] == hint["page"]), None)
    if bi is None:
        return None
    head = hint["heading"].strip()
    text = pages[bi].get("text") or ""
    pos = text.find(head)
    if pos < 0:
        pos = 0
    start = text[pos:]
    lines = start.split("\n")
    keep = [lines[0]]
    for line in lines[1:]:
        if HEADING_START.match(line) or re.match(r"^\s*#{1,6}\s", line):
            break
        keep.append(line)
        if sum(len(x) + 1 for x in keep) > budget:
            break
    body = "\n".join(keep).strip()
    # a section can continue on the next page when no new heading follows
    if len(body) < 400:
        for nxt in pages[bi + 1: bi + 3]:
            t = (nxt.get("text") or "")
            cut = len(t)
            for line in t.split("\n"):
                if HEADING_START.match(line) or re.match(r"^\s*#{1,6}\s", line):
                    cut = t.index(line)
                    break
            body += "\n" + t[:cut]
            if len(body) > budget or cut < len(t):
                break
    return {"page": hint["page"], "heading": head, "text": body[:budget]}


def whole_doc_text(doc: dict, budget: int = 24000) -> str:
    """Concatenated text with page markers, truncated to a budget."""
    out, total, truncated = [], 0, False
    for p in doc["pages"]:
        t = (p.get("text") or "").strip()
        if not t:
            continue
        piece = f"[p.{p['n']}] {t}"
        if total + len(piece) > budget:
            out.append(f"[p.{p['n']}] " + t[: max(0, budget - total)] + " …[truncated]")
            truncated = True
            break
        out.append(piece)
        total += len(piece)
    return "\n\n".join(out), truncated
