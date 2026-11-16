"""
Offline study engine — no API key required.

Everything here is classical NLP (sentence scoring, TF-IDF term ranking, cloze
question generation, extractive answer assembly). Output is labelled as
extractive so the learner knows it is quoting the document rather than
reasoning about it.
"""
from __future__ import annotations

import math
import random
import re
from collections import Counter

from retrieve import STOP, Index, section_hint, section_span, sentences, tokenize


def _sent_scores(texts: list[str]) -> list[float]:
    """TF-IDF-weighted sentence salience."""
    sents = [s for t in texts for s in sentences(t)]
    if not sents:
        return []
    doc_freq: Counter = Counter()
    toks = []
    for s in sents:
        tk = tokenize(s)
        toks.append(tk)
        for w in set(tk):
            doc_freq[w] += 1
    N = len(sents)
    idf = {w: math.log(1 + N / (1 + c)) for w, c in doc_freq.items()}
    scores = []
    for i, tk in enumerate(toks):
        if not tk:
            scores.append(0.0)
            continue
        tf = Counter(tk)
        s = sum((1 + math.log(f)) * idf.get(w, 1.0) for w, f in tf.items())
        s /= math.sqrt(len(tk)) or 1  # length normalise
        s *= 1.0 + 0.25 * (1 - min(i, 40) / 60)  # early sentences matter more
        if len(tk) < 6:
            s *= 0.4
        scores.append(s)
    return scores



def _excerpt(text: str, maxlen: int = 520) -> str:
    """Take whole sentences from the start, skipping truncated openers."""
    ss = sentences(text)
    if not ss:
        return re.sub(r"\s+", " ", text)[:maxlen]
    # a chunk that begins mid-sentence starts with a lowercase fragment
    if ss and ss[0][:1].islower() and len(ss) > 1:
        ss = ss[1:]
    out = ""
    for s in ss:
        if len(out) + len(s) > maxlen and out:
            break
        out = (out + " " + s).strip()
    if not out:
        out = ss[0][:maxlen].rsplit(" ", 1)[0] + "…"
    return out


def summarize(doc: dict, bullets: int = 8) -> dict:
    per_page = [(p["n"], p.get("text") or "") for p in doc["pages"]]
    flat, refs = [], []
    for n, t in per_page:
        for s in sentences(t):
            flat.append(s)
            refs.append(n)
    if not flat:
        return {"mode": "offline", "bullets": [],
                "note": "This document has no machine-readable text (it may be a scan or an image)."
                        " Add an API key to have the tutor read it visually."}
    scores = _sent_scores([s for s in flat])
    order = sorted(range(len(flat)), key=lambda i: -scores[i])
    picked = []
    seen_words = set()
    for i in order:
        s = flat[i]
        key = set(tokenize(s))
        if not key:
            continue
        overlap = len(key & seen_words) / max(1, len(key))
        if overlap > 0.75 and len(picked) >= 3:
            continue  # avoid near-duplicate bullets
        picked.append(i)
        seen_words |= key
        if len(picked) >= bullets:
            break
    picked.sort()
    return {
        "mode": "offline",
        "bullets": [{"text": flat[i], "page": refs[i]} for i in picked],
        "note": "Extractive summary — these are the highest-salience sentences from your "
                "document. Add an API key for a written, paraphrased overview.",
    }


def key_concepts(doc: dict, limit: int = 12) -> dict:
    chunks = doc["chunks"]
    text_all = " ".join(c["text"] for c in chunks)
    toks = tokenize(text_all)
    if not toks:
        return {"mode": "offline", "terms": []}
    tf = Counter(toks)
    # candidate terms: single tokens + adjacent bigrams
    bigrams: Counter = Counter()
    for c in chunks:
        for s in sentences(c["text"]):
            tk = [t for t in tokenize(s)]
            for a, b in zip(tk, tk[1:]):
                bigrams[f"{a} {b}"] += 1
    def rank(counts: Counter, min_count: int):
        return {k: v for k, v in counts.items() if v >= min_count and len(k) > 3}
    cands = {}
    for k, v in rank(tf, max(2, len(toks) // 900)).items():
        cands[k] = v
    for k, v in rank(bigrams, 3).items():
        cands[k] = v * 2.1  # multiword phrases are often the real concepts
    top = sorted(cands.items(), key=lambda kv: -kv[1])[: limit * 3]
    terms = []
    used = set()
    for term, _score in top:
        if any(term in u or u in term for u in used):
            continue
        ctx = _best_sentence(chunks, term)
        if not ctx:
            continue
        used.add(term)
        terms.append({"term": term, "page": ctx["page"], "context": ctx["snippet"]})
        if len(terms) >= limit:
            break
    return {"mode": "offline", "terms": terms}


def _best_sentence(chunks: list[dict], term: str, definitional: bool = False) -> dict | None:
    """The sentence that best introduces `term` (never a heading, never a raw slice)."""
    pat = re.compile(r"\b" + re.escape(term) + r"\b", re.I)
    best, best_score = None, -99.0
    for c in chunks:
        for s in sentences(c["text"]):
            m = pat.search(s)
            if not m:
                continue
            score = 0.0
            if m.start() <= 45:
                score += 2.0                       # term is the subject of the sentence
            tail = s[m.end():m.end() + 90]
            if re.search(r"\b(is|are|means|refers to|defined as|describes|denotes|stands for)\b", tail, re.I):
                score += 2.0
            elif re.search(r"\b(is|are|means|refers to|defined as|describes)\b", s, re.I):
                score += 0.6
            if s.rstrip().endswith((",", ";", ":")):
                score -= 1.5
            if re.match(r"^(what|why|how|when|where|if|but|and)\b", s, re.I):
                score -= 2.0
            if 45 <= len(s) <= 320:
                score += 0.8
            if s.count(term.lower()) > 2:
                score += 0.3
            if definitional and not re.search(r"\b(is|are|means|refers to|defined as)\b", s, re.I):
                score -= 1.0
            if score > best_score:
                best, best_score = {"page": c["page"], "snippet": s}, score
    return best


def glossary(doc: dict, limit: int = 10) -> dict:
    concepts = key_concepts(doc, limit * 2)
    out = []
    seen = set()
    for t in concepts.get("terms", []):
        base = t["term"].split()[0].lower()
        if base in seen:
            continue
        best = _best_sentence(doc["chunks"], t["term"], definitional=True)
        if not best:
            continue
        seen.add(base)
        out.append({"term": t["term"].title(),
                    "definition": {"text": best["snippet"], "page": best["page"]}})
        if len(out) >= limit:
            break
    return {"mode": "offline", "entries": out}


def flashcards(doc: dict, count: int = 8) -> dict:
    concepts = key_concepts(doc, count * 2)
    cards = []
    for t in concepts.get("terms", []):
        s = t["context"]
        if len(s) < 40:
            continue
        cards.append({"front": f"What is the role of “{t['term'].title()}” in this material?",
                      "back": s, "page": t["page"]})
        if len(cards) >= count:
            break
    for b in summarize(doc, bullets=count)["bullets"][: max(0, count - len(cards))]:
        cards.append({"front": "Recall the key point of this passage", "back": b["text"], "page": b["page"]})
    return {"mode": "offline", "cards": cards}


def quiz(doc: dict, count: int = 5) -> dict:
    """Cloze MCQs built from salient sentences; distractors are sibling terms."""
    sents, refs = [], []
    for p in doc["pages"]:
        for s in sentences(p.get("text") or ""):
            if 60 <= len(s) <= 320:
                sents.append(s)
                refs.append(p["n"])
    if len(sents) < 2:
        return {"mode": "offline", "questions": []}
    all_scores = _sent_scores(["\n".join(sents)])
    concepts = [t["term"] for t in key_concepts(doc, 30).get("terms", [])]
    pool = list({c.lower() for c in concepts})
    rng = random.Random(hash(tuple(sents[:5])) & 0xFFFFFFFF)
    questions, used_terms = [], set()
    ranked = sorted(range(len(sents)), key=lambda i: -all_scores[i])
    for i in ranked:
        s = sents[i]
        words = [t for t in tokenize(s) if len(t) > 4]
        if not words:
            continue
        term = next((w for w in sorted(set(words), key=lambda w: -len(w)) if w not in used_terms), None)
        if not term:
            continue
        pat = re.compile(r"\b" + re.escape(term) + r"\b", re.I)
        if not pat.search(s):
            continue
        blanked = pat.sub("_______", s, count=1)
        distract = [w for w in pool if w != term.lower() and abs(len(w) - len(term)) <= 8]
        rng.shuffle(distract)
        options = [term] + distract[:3]
        if len(options) < 3:
            continue
        rng.shuffle(options)
        used_terms.add(term)
        questions.append({
            "question": blanked,
            "options": options,
            "answer": term,
            "page": refs[i],
        })
        if len(questions) >= count:
            break
    return {"mode": "offline", "questions": questions,
            "note": "Auto-generated cloze questions from the document's own sentences."}


def explain(text: str, doc: dict, level: str = "student") -> dict:
    """Offline 'help me understand this' — pull related passages + vocabulary."""
    if not (text or "").strip():
        return {"mode": "offline", "related": [],
                "note": "That area has no text layer (it looks like a picture, chart or scan), so offline mode "
                        "cannot read it. Add an API key — Sage will then look at the cropped image itself."}
    idx = Index(doc["chunks"])
    hits = idx.search(text, k=4)
    words = [w for w in tokenize(text) if len(w) > 5]
    related = [h for h in hits if any(w in h["text"].lower() for w in words)][:3]
    return {
        "mode": "offline",
        "related": [{"page": h["page"], "text": _excerpt(h["text"], 600)} for h in (related or hits)],
        "note": "Offline mode can't paraphrase, so here is what the document itself says about "
                "the selected passage. Add an API key in settings and the tutor will explain it in its own words.",
    }


SUMMARISE_RE = re.compile(r"\b(summari[sz]e|summar(?:y|ise|ize)|tl;?dr|overview|"
                          r"main (?:point|idea|argument|takeaway)s?)\b", re.I)
QUIZ_RE = re.compile(r"\b(quiz|test me|examin|practice questions?|questions? about)\b", re.I)


def _scoped(doc: dict, hint: dict | None) -> dict:
    """A view of the document restricted to one section, when the learner named one."""
    if not hint:
        return doc
    span = section_span(doc, hint)
    if span and len(span["text"]) > 120:
        from extract import _chunk_pages
        body = span["text"]
        first, _, rest = body.partition("\n")
        # don't quote the heading back inside every bullet — it is already in the preamble
        if rest.strip() and (first.lstrip().startswith("#") or len(first) < 80) and \
                not first.rstrip().endswith((".", "?", "!", ":")):
            body = rest
        pages = [{"n": span["page"], "label": f"p.{span['page']}", "text": body}]
        return {**doc, "pages": pages, "chunks": _chunk_pages(pages)}
    pages = [p for p in doc["pages"] if p["n"] == hint["page"]] or doc["pages"]
    nums = {p["n"] for p in pages}
    chunks = [c for c in doc["chunks"] if c["page"] in nums] or doc["chunks"]
    return {**doc, "pages": pages, "chunks": chunks}


def _focus_hits(focus: str, doc: dict, k: int = 2) -> list[dict]:
    """Chunks that best cover the passage the learner pointed at."""
    ft = set(tokenize(focus))
    if not ft:
        return []
    scored = []
    for c in doc["chunks"]:
        ct = set(tokenize(c["text"]))
        overlap = len(ft & ct) / max(1, min(len(ft), 25))
        if overlap > 0:
            scored.append((overlap, c))
    scored.sort(key=lambda x: -x[0])
    return [{"page": c["page"], "text": c["text"], "chunk": c["id"], "score": round(s, 3)}
            for s, c in scored[:k]]


def _answer_from(hits: list[dict], question: str, focus: str = "", limit: int = 3) -> str:
    """Pick the sentences inside the given passages that best answer the question."""
    qt = set(tokenize(question))
    ft = set(tokenize(focus))
    picked = []
    for h in hits:
        ss = [s for s in sentences(h["text"]) if s[:1].isupper() or s[:1].isdigit()]
        if not ss:
            continue
        scored = sorted(ss, key=lambda s: -(len(qt & set(tokenize(s))) + 0.6 * len(ft & set(tokenize(s)))))
        for s in scored[:2]:
            line = f"• (p.{h['page']}) {s.strip()}"
            if line not in picked:
                picked.append(line)
        if len(picked) >= limit:
            break
    return "\n".join(picked[:limit])


def answer(question: str, doc: dict, focus: str = "") -> dict:
    hint = section_hint(doc, question)
    scoped = _scoped(doc, hint)

    # the learner pointed at a specific passage or region: answer from there first
    if focus and focus.strip():
        fh = _focus_hits(focus, doc, k=2)
        if fh:
            md = ["Here is what your material says about that:"]
            body = _answer_from(fh, question or focus, focus=focus, limit=3)
            if body:
                md.append(body)
            md += ["", f"_Answering from the passage you selected (p.{fh[0]['page']}). "
                       f"Add an API key for a written explanation in Sage's own words._"]
            return {"mode": "offline", "answer": "\n".join(md),
                    "sources": [{"page": h["page"], "text": _excerpt(h["text"], 400)} for h in fh]}

    # route obvious intents to the right offline skill instead of a keyword search
    if SUMMARISE_RE.search(question or ""):
        r = summarize(scoped, 5)
        where = f" (section starting on p.{hint['page']}: “{hint['heading']}”)" if hint else ""
        md = [f"Here is a summary{where}:"]
        md += [f"• (p.{b['page']}) {b['text']}" for b in r["bullets"]]
        md += ["", f"_{r['note']}_"]
        return {"mode": "offline", "answer": "\n".join(md),
                "sources": [{"page": b["page"]} for b in r["bullets"][:3]]}
    if QUIZ_RE.search(question or ""):
        r = quiz(scoped, 3)
        if r["questions"]:
            md = ["Quick check from your material:", ""]
            for i, q in enumerate(r["questions"], 1):
                md.append(f"**Q{i}.** {q['question']}")
                md += [f"- {'ABCD'[j]}) {o}" for j, o in enumerate(q["options"])]
                md.append("")
            md += ["---", "**Answers**"] + [f"- Q{i}: **{q['answer']}** (p.{q['page']})"
                                            for i, q in enumerate(r["questions"], 1)]
            return {"mode": "offline", "answer": "\n".join(md), "sources": []}

    idx = Index(doc["chunks"])
    hits = idx.search(question, k=6)
    if hint:
        on_page = [h for h in hits if h["page"] == hint["page"]]
        if on_page:                       # relevant matches already on the named section
            hits = on_page + [h for h in hits if h["page"] != hint["page"]][:2]
        else:                             # nothing matched there — show what that section says
            page_hits = [{"page": c["page"], "text": c["text"], "chunk": c["id"]}
                         for c in doc["chunks"] if c["page"] == hint["page"]][:3]
            hits = page_hits + [h for h in hits if h["page"] != hint["page"]][:1]
    if not hits:
        # nothing matched: fall back to the most salient passages rather than a dead end
        r = summarize(doc, 3)
        if r["bullets"]:
            md = ["I couldn't match those words to a passage, so here are the most important "
                  "points of your material instead:", ""]
            md += [f"• (p.{b['page']}) {b['text']}" for b in r["bullets"]]
            md += ["", "_Try naming a term from the document, or select a passage and press Explain._"]
            return {"mode": "offline", "answer": "\n".join(md), "sources": []}
    if not hits:
        return {"mode": "offline", "answer": "I couldn't find anything in this document about that. "
                                            "Try different wording, or search the file with the find box.",
                "sources": []}
    lines = ["Here is what your material says about that:"]
    if hint:
        lines[0] = f"Here is what your material says about that (section starting on p.{hint['page']}: “{hint['heading']}”):"
    for h in hits[:3]:
        lines.append(f"• (p.{h['page']}) " + _excerpt(h["text"], 460))
    return {
        "mode": "offline",
        "answer": "\n".join(lines),
        "sources": [{"page": h["page"], "text": h["text"][:400]} for h in hits[:3]],
        "note": "Extractive answer — quoted from the document. Add an API key for a real conversational tutor.",
    }
