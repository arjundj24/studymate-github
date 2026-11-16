"""
Provider-agnostic chat client. The user's API key never touches disk — the
browser holds it and sends it per request; the server only forwards it to the
provider the user chose.

Supported: OpenAI, Anthropic, Google Gemini, and any OpenAI-compatible
endpoint (Ollama, Groq, OpenRouter, vLLM, LM Studio...).
"""
from __future__ import annotations

import json
from typing import AsyncIterator

import httpx

# One registry for every provider we can talk to. `kind` picks the wire format,
# `base` and `model` are the defaults, `signup` is where a visitor gets a key.
# Free-tier providers come first: they are what most people should pick.
PROVIDERS: dict[str, dict] = {
    "gemini": {
        "label": "Google Gemini — free tier", "kind": "gemini",
        "base": "https://generativelanguage.googleapis.com", "model": "gemini-2.5-flash",
        "signup": "https://aistudio.google.com/apikey", "free": True,
        "note": "Free API key in ~1 minute with a Google account. Best default.",
    },
    "github": {
        "label": "GitHub Models — GPT-4.1, o4-mini, Llama, DeepSeek", "kind": "openai",
        "base": "https://models.github.ai/inference", "model": "openai/gpt-4.1-mini",
        "signup": "https://github.com/settings/tokens", "free": True,
        "note": "Free with any GitHub account: create a token with the models:read scope. "
                "Gives you GPT-4.1 / 4o / o4-mini without paying OpenAI anything.",
        "ctx_chars": 15000,      # free tier caps input at ~8K tokens per request
    },
    "groq": {
        "label": "Groq — free tier", "kind": "openai",
        "base": "https://api.groq.com/openai/v1", "model": "llama-3.3-70b-versatile",
        "signup": "https://console.groq.com/keys", "free": True,
        "note": "Free and extremely fast. Great for quizzes and flashcards.",
    },
    "openrouter": {
        "label": "OpenRouter — free models", "kind": "openai",
        "base": "https://openrouter.ai/api/v1", "model": "meta-llama/llama-3.3-70b-instruct:free",
        "signup": "https://openrouter.ai/keys", "free": True,
        "note": "One key, many models, several of them free (marked :free).",
    },
    "cerebras": {
        "label": "Cerebras — free tier", "kind": "openai",
        "base": "https://api.cerebras.ai/v1", "model": "llama-3.3-70b",
        "signup": "https://cloud.cerebras.ai", "free": True,
        "note": "Free tier with a generous daily quota.",
    },
    "mistral": {
        "label": "Mistral — free tier", "kind": "openai",
        "base": "https://api.mistral.ai/v1", "model": "mistral-small-latest",
        "signup": "https://console.mistral.ai/api-keys", "free": True,
        "note": "Free experiment tier from Mistral.",
    },
    "openai": {
        "label": "OpenAI (paid)", "kind": "openai",
        "base": "https://api.openai.com/v1", "model": "gpt-4o-mini",
        "signup": "https://platform.openai.com/api-keys", "free": False,
        "note": "Pay per use; no free tier.",
    },
    "anthropic": {
        "label": "Anthropic Claude (paid)", "kind": "anthropic",
        "base": "https://api.anthropic.com", "model": "claude-sonnet-4-5",
        "signup": "https://console.anthropic.com/settings/keys", "free": False,
        "note": "Pay per use; no free tier.",
    },
    "local": {
        "label": "Local model on your own computer (Ollama)", "kind": "openai",
        "base": "http://localhost:11434/v1", "model": "llama3.2",
        "signup": "https://ollama.com/download", "free": True,
        "note": "Runs on your own machine: no key, no quota, no cost, works offline. "
                "Install Ollama, then `ollama pull llama3.2` (8 GB RAM) or `qwen3:8b` (16 GB).",
        "no_key": True, "ctx_chars": 9000,
    },
    "custom": {
        "label": "Custom / self-hosted (Ollama, LM Studio…)", "kind": "openai",
        "base": "", "model": "llama3.1", "signup": "", "free": True,
        "note": "Anything that speaks the OpenAI chat-completions API.",
    },
}

DEFAULT_MODELS = {k: v["model"] for k, v in PROVIDERS.items()}
DEFAULT_BASE = {k: v["base"] for k, v in PROVIDERS.items()}

SYSTEM = """You are StudyMate, an expert tutor embedded in a document reader.

Ground rules:
- The learner has a document open. Its text is given as context with [p.N] page markers.
  Answer from that material first. Cite pages inline as (p.3).
- If the answer is genuinely not in the document, say so plainly, then offer the
  general knowledge answer clearly marked "Outside the document:".
- Never invent quotes, numbers, citations or page references.
- Teach, don't dump: short paragraphs or bullets, define jargon the first time,
  use one concrete example or analogy when it helps.
- When the learner asks for a summary, quiz, flashcards or glossary, follow the
  requested format exactly and keep every item traceable to the text.
- If the learner is confused, find the smallest piece they're missing and build up from there.
- Be warm and encouraging, never condescending. Match their level."""

STUDY_FORMATS = """Format instructions:
- SUMMARY: 5-8 bullets, each one self-contained, each ending with its page ref (p.N). Then one "In one line:" sentence.
- KEY CONCEPTS: numbered list. For each: **Term** — what it is (p.N) — why it matters here.
- GLOSSARY: table-ready pairs "Term — definition (p.N)", alphabetical, only terms that appear in the document.
- QUIZ: 5 multiple-choice questions with 4 options (A-D), then a separate "Answers" section with a one-line reason and page ref for each.
- FLASHCARDS: 8 cards as "Q: ... / A: ... (p.N)" — Q must be answerable from the document, A must be a full sentence.
- EXPLAIN: rewrite the passage in plain language, define each hard term in a mini-glossary, and give one worked example."""


def _normalise(provider: str, model: str | None) -> tuple[str, str]:
    provider = (provider or "gemini").lower().strip()
    if provider not in PROVIDERS:
        provider = "custom"
    return provider, (model or DEFAULT_MODELS[provider]).strip()


def kind_of(provider: str) -> str:
    return PROVIDERS.get((provider or "").lower().strip(), PROVIDERS["custom"])["kind"]


# Names that mean "this model can see". Anything else gets no images attached.
VISION_HINTS = ("vision", "llava", "moondream", "pixtral", "minicpm-v",
                "qwen2.5-vl", "qwen2-vl", "gemma3", "gemma-3", "internvl", "-vl")


def supports_vision(provider: str, model: str | None) -> bool:
    """Can this provider+model accept an image?

    Text-only models (llama3.1:8b locally, most free-tier models) reject a request
    outright with "Multimodal data provided, but model does not support multimodal
    requests", so a dragged region must fall back to the text layer instead.
    """
    pid = (provider or "").strip().lower()
    m = (model or "").strip().lower()
    if any(h in m for h in VISION_HINTS):
        return True
    if pid in ("gemini", "openai", "anthropic"):
        return True
    return False


def context_chars(provider: str, default: int = 24000) -> int:
    """How much document text this provider can take in one request."""
    pid = (provider or "").lower().strip()
    return int(PROVIDERS.get(pid, {}).get("ctx_chars") or default)


def base_for(provider: str, base_url: str | None) -> str:
    """Explicit base URL wins; otherwise the registry default."""
    return (base_url or DEFAULT_BASE.get(provider, "") or "").strip()


async def stream_chat(
    provider: str,
    api_key: str,
    model: str | None,
    messages: list[dict],
    base_url: str | None = None,
    images: list[dict] | None = None,      # [{"mime": "image/png", "data": "<b64>"}]
    temperature: float = 0.4,
) -> AsyncIterator[str]:
    """Yield text deltas from the provider."""
    provider, model = _normalise(provider, model)
    kind = kind_of(provider)

    if kind == "anthropic":
        url = (base_for(provider, base_url) or "https://api.anthropic.com") + "/v1/messages"
        sysmsg = SYSTEM
        body_msgs = []
        for m in messages:
            if m["role"] == "system":
                sysmsg += "\n\n" + m["content"]
                continue
            body_msgs.append({"role": m["role"], "content": m["content"]})
        if images:
            last = body_msgs[-1]
            blocks = [{"type": "image", "source": {"type": "base64",
                                                   "media_type": img["mime"],
                                                   "data": img["data"]}} for img in images]
            blocks.append({"type": "text", "text": last["content"]})
            last["content"] = blocks
        payload = {"model": model, "max_tokens": 2048, "temperature": temperature,
                   "system": sysmsg, "messages": body_msgs, "stream": True}
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01",
                   "content-type": "application/json"}
        async with httpx.AsyncClient(timeout=180) as cl:
            async with cl.stream("POST", url, json=payload, headers=headers) as r:
                if r.status_code >= 400:
                    raise RuntimeError((await r.aread()).decode()[:400])
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        ev = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if ev.get("type") == "content_block_delta":
                        t = ev.get("delta", {}).get("text")
                        if t:
                            yield t
        return

    if kind == "gemini":
        url = (f"{(base_for(provider, base_url) or 'https://generativelanguage.googleapis.com').rstrip('/')}/v1beta/models/"
               f"{model}:streamGenerateContent?alt=sse&key={api_key}")
        contents = []
        for m in messages:
            if m["role"] == "system":
                continue
            role = "user" if m["role"] == "user" else "model"
            contents.append({"role": role, "parts": [{"text": m["content"]}]})
        if images:
            contents[-1]["parts"] = [
                {"inline_data": {"mime_type": img["mime"], "data": img["data"]}} for img in images
            ] + contents[-1]["parts"]
        payload = {
            "contents": contents,
            "systemInstruction": {"parts": [{"text": SYSTEM}]},
            "generationConfig": {"temperature": temperature, "maxOutputTokens": 2048},
        }
        async with httpx.AsyncClient(timeout=180) as cl:
            async with cl.stream("POST", url, json=payload,
                                 headers={"content-type": "application/json"}) as r:
                if r.status_code >= 400:
                    raise RuntimeError((await r.aread()).decode()[:400])
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        ev = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    for cand in ev.get("candidates", []):
                        for part in cand.get("content", {}).get("parts", []):
                            if part.get("text"):
                                yield part["text"]
        return

    # OpenAI and anything OpenAI-compatible (Groq, OpenRouter, Cerebras, Mistral, Ollama…)
    url = (base_for(provider, base_url) or "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
    msgs = [{"role": "system", "content": SYSTEM}]
    for m in messages:
        if m["role"] == "system":
            msgs[0]["content"] += "\n\n" + m["content"]
        else:
            msgs.append({"role": m["role"], "content": m["content"]})
    if images:
        last = msgs[-1]
        parts = [{"type": "text", "text": last["content"]}]
        for img in images:
            parts.append({"type": "image_url",
                          "image_url": {"url": f"data:{img['mime']};base64,{img['data']}"}})
        last["content"] = parts
    payload = {"model": model, "messages": msgs, "stream": True, "temperature": temperature}
    headers = {"Authorization": f"Bearer {api_key}", "content-type": "application/json"}
    async with httpx.AsyncClient(timeout=180) as cl:
        async with cl.stream("POST", url, json=payload, headers=headers) as r:
            if r.status_code >= 400:
                raise RuntimeError((await r.aread()).decode()[:400])
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    ev = json.loads(data)
                except json.JSONDecodeError:
                    continue
                for ch in ev.get("choices", []):
                    t = (ch.get("delta") or {}).get("content")
                    if t:
                        yield t


async def complete(provider, api_key, model, messages, base_url=None, images=None,
                   temperature=0.4) -> str:
    buf = []
    async for tok in stream_chat(provider, api_key, model, messages, base_url, images, temperature):
        buf.append(tok)
    return "".join(buf)


async def verify_key(provider: str, api_key: str, model: str | None, base_url: str | None = None) -> dict:
    """Tiny round-trip so the settings panel can confirm a key works."""
    try:
        out = await complete(provider, api_key, model,
                             [{"role": "user", "content": "Reply with exactly: ok"}],
                             base_url=base_url, temperature=0)
        return {"ok": True, "reply": out.strip()[:60]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:400]}
