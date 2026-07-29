"""LLM synthesis: messy Notion notes -> decision-tree flashcards.

The whole value of DeepRecall lives in this prompt. A generic "summarise these
notes" call produces pros-and-cons lists that feel productive to read and teach
nothing. What we want instead is the shape an interviewer or an on-call
incident forces out of you:

  * the mechanism, at the level of syscalls and data structures;
  * a decision matrix keyed on *constraints*, not on features;
  * the tipping point where the technology becomes the wrong answer;
  * constraint modifiers that invalidate the architecture you just described.

Output is requested as JSON (not Markdown) so parsing is deterministic, then
rendered to Markdown locally. Cards are linted for buzzwords and structural
completeness, with one corrective retry before we give up and keep the draft.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

from .config import Config
from .ingestion import RawNote
from .vault import (
    SECTION_MATRIX,
    SECTION_MECHANISM,
    SECTION_MODIFIERS,
    SECTION_TIPPING_POINT,
    Card,
    slugify,
)

log = logging.getLogger(__name__)

MAX_LLM_RETRIES = 3
BASE_BACKOFF_SECONDS = 2.0
#: Gemini's free tier allows ~15 requests/minute; stay comfortably under it.
MIN_SECONDS_BETWEEN_CALLS = 4.0
REQUEST_TIMEOUT_SECONDS = 90.0


class SynthesisError(RuntimeError):
    """Raised when the LLM cannot be reached or returns unusable output."""


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a systems-engineering interviewer building active-recall flashcards for \
an experienced backend engineer. Your job is to convert messy notes into a card \
that tests *mechanical understanding*, not vocabulary.

HARD RULES — a card that breaks any of these is worthless:

1. NEVER output a "Pros and Cons" list, a feature comparison, or a marketing \
summary. The card must be organised around constraints and trade-offs.
2. NEVER use a vague performance adjective on its own. Words like "fast", \
"high throughput", "scalable", "efficient", "robust", "lightweight", \
"reliable", "flexible", "performant" and "seamless" are BANNED unless the same \
sentence names the mechanism that produces the property. \
   Bad:  "Kafka is fast and has high throughput."
   Good: "Kafka avoids user-space copies on the read path via sendfile(2), so \
          bytes go page cache -> socket buffer; enabling TLS defeats this and \
          costs ~30% throughput."
3. Be concrete and low-level. Name syscalls, data structures, wire protocols, \
config parameters with their real names and default values, algorithmic \
complexity, and rough numeric magnitudes (latency in µs/ms, sizes in KB/MB). \
Prefer `backticked` identifiers for anything that appears in code or config.
4. Do not invent facts. If the source notes are thin on a point, write what the \
mechanism *is* at the level you are confident about, and do not pad.
5. Everything must be tied back to the source notes' subject matter. Do not \
drift into an adjacent technology except in the Tipping Point, where naming \
the better alternative is the point.

You must return a single JSON object and nothing else — no prose, no code \
fences. Schema:

{
  "id": "kebab-case-slug, max 48 chars, derived from the question",
  "topic": "Broad field, Title Case. e.g. 'Distributed Systems', 'Databases', \
'Operating Systems', 'Networking', 'Concurrency'",
  "question": "One sharp question that forces the mechanism out. Rewrite the \
source question if it is vague. No yes/no questions.",
  "direct_mechanism": "The 30-second answer. 2-5 sentences of pure mechanics: \
what actually happens, in what order, in which component. Name the OS calls, \
data structures and parameters involved.",
  "decision_matrix": [
    {
      "condition": "The constraint that forces the choice, phrased as 'IF ...'",
      "choice": "What you do under that constraint — concrete and specific",
      "tradeoff": "What you give up, and the mechanism behind the cost"
    }
  ],
  "tipping_point": "When is this technology/pattern the WRONG choice? Name the \
specific workload characteristic that breaks it AND the alternative that \
handles it, with the reason that alternative wins.",
  "constraint_modifiers": [
    {
      "name": "Short label for the changed constraint, e.g. 'Strict Ordering'",
      "effect": "How the architecture above must change once this holds, and \
why — the second-order consequence, not a restatement."
    }
  ]
}

Provide 2-4 decision_matrix entries and 2-3 constraint_modifiers. Each \
constraint modifier must change the *architecture*, not merely the tuning.\
"""

USER_PROMPT_TEMPLATE = """\
Source page: {page_title}
Section path: {breadcrumbs}

QUESTION FROM NOTES:
{question}

RAW NOTES (unstructured, possibly incomplete or badly worded):
{raw_answer}

Produce the JSON card now."""

RETRY_PROMPT_TEMPLATE = """\
Your previous card was rejected by the quality linter. Problems found:

{violations}

Rewrite the ENTIRE JSON object, fixing every problem. Replace each vague claim \
with the underlying mechanism (syscall, data structure, parameter, complexity, \
or a number with units). Return only the JSON object."""


# ---------------------------------------------------------------------------
# Quality linting
# ---------------------------------------------------------------------------

BANNED_PATTERNS = [
    r"\bhigh[- ]throughput\b", r"\bhighly scalable\b", r"\bscalab\w+\b",
    r"\bblazing(?:ly)? fast\b", r"\bvery fast\b", r"\bsuper fast\b", r"\bfast\b",
    r"\brobust\b", r"\befficien\w+\b", r"\bperformant\b", r"\blightweight\b",
    r"\bseamless\w*\b", r"\bflexib\w+\b", r"\beasy to use\b", r"\bbest practice\b",
    r"\bindustry standard\b", r"\bbattle[- ]tested\b", r"\bpowerful\b",
    r"\bcutting[- ]edge\b", r"\bwebscale\b", r"\bblazing\b",
]
_BANNED = re.compile("|".join(BANNED_PATTERNS), re.IGNORECASE)

#: Cheap evidence that a sentence explains itself: an identifier, a number with
#: a unit, a syscall, or big-O notation.
_MECHANISM_MARKER = re.compile(
    r"`[^`]+`|\b\w+\(\)|\bO\(|\d+\s?(?:ms|µs|us|ns|s|KB|MB|GB|TB|kB|%|x|bytes?|bits?)\b|\d{2,}",
    re.IGNORECASE,
)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;])\s+|\n")


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT.split(text or "") if s.strip()]


def lint_card(data: dict[str, Any]) -> list[str]:
    """Return human-readable complaints; empty list means the card passes.

    A banned word is only a violation when its own sentence offers no mechanism
    — "fast because `sendfile(2)` skips the user-space copy" is fine.
    """
    problems: list[str] = []

    question = (data.get("question") or "").strip()
    if len(question) < 15:
        problems.append("`question` is missing or too short to test anything.")
    if question.lower().startswith(("is ", "are ", "does ", "do ", "can ", "should ")):
        problems.append("`question` is a yes/no question; rewrite it to demand a mechanism.")

    mechanism = (data.get("direct_mechanism") or "").strip()
    if len(mechanism) < 120:
        problems.append("`direct_mechanism` is too thin — give 2-5 sentences of real mechanics.")
    elif not _MECHANISM_MARKER.search(mechanism):
        problems.append(
            "`direct_mechanism` names no concrete mechanism: no syscall, `identifier`, "
            "complexity or number with units appears anywhere in it."
        )

    matrix = data.get("decision_matrix") or []
    if len(matrix) < 2:
        problems.append("`decision_matrix` needs at least 2 constraint-driven entries.")
    for i, row in enumerate(matrix, 1):
        if not isinstance(row, dict):
            problems.append(f"`decision_matrix[{i}]` is not an object.")
            continue
        for key in ("condition", "choice", "tradeoff"):
            if not (row.get(key) or "").strip():
                problems.append(f"`decision_matrix[{i}].{key}` is empty.")

    tipping = (data.get("tipping_point") or "").strip()
    if len(tipping) < 60:
        problems.append("`tipping_point` must name the breaking workload AND the better alternative.")

    modifiers = data.get("constraint_modifiers") or []
    if len(modifiers) < 2:
        problems.append("`constraint_modifiers` needs 2-3 entries that change the architecture.")
    for i, mod in enumerate(modifiers, 1):
        if not isinstance(mod, dict) or not (mod.get("effect") or "").strip():
            problems.append(f"`constraint_modifiers[{i}]` is missing an `effect`.")

    for sentence in _sentences(_all_prose(data)):
        hit = _BANNED.search(sentence)
        if hit and not _MECHANISM_MARKER.search(sentence):
            problems.append(
                f'Unexplained buzzword "{hit.group(0)}" in: "{sentence[:110]}" '
                "— state the mechanism that produces it."
            )

    return problems


def _all_prose(data: dict[str, Any]) -> str:
    parts = [data.get("direct_mechanism", ""), data.get("tipping_point", "")]
    for row in data.get("decision_matrix") or []:
        if isinstance(row, dict):
            parts += [row.get("condition", ""), row.get("choice", ""), row.get("tradeoff", "")]
    for mod in data.get("constraint_modifiers") or []:
        if isinstance(mod, dict):
            parts += [mod.get("name", ""), mod.get("effect", "")]
    return "\n".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


class LLMProvider(Protocol):
    name: str

    def generate(self, system: str, messages: list[str]) -> str:
        """Return raw model text for a system prompt plus a turn-by-turn history."""


@dataclass
class GeminiProvider:
    """Google AI Studio free tier via `google-genai`."""

    api_key: str
    model: str = "gemini-2.0-flash"
    name: str = "gemini"

    def __post_init__(self) -> None:
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise SynthesisError(
                "google-genai is not installed. Run: pip install -r requirements.txt"
            ) from exc
        self._types = types
        self._client = genai.Client(api_key=self.api_key)

    def generate(self, system: str, messages: list[str]) -> str:
        config = self._types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            temperature=0.3,
            max_output_tokens=2400,
        )
        # Alternating user/model turns; our history is user-only follow-ups, so
        # interleave the previous raw answer as the model turn where present.
        contents = [
            self._types.Content(role="user", parts=[self._types.Part(text=text)])
            for text in messages
        ]
        response = self._client.models.generate_content(
            model=self.model, contents=contents, config=config
        )
        text = getattr(response, "text", None)
        if not text:
            raise SynthesisError(f"Gemini returned no text (model={self.model})")
        return text


@dataclass
class GroqProvider:
    """Groq free tier via the OpenAI-compatible chat completions endpoint."""

    api_key: str
    model: str = "llama-3.3-70b-versatile"
    name: str = "groq"
    endpoint: str = "https://api.groq.com/openai/v1/chat/completions"

    def generate(self, system: str, messages: list[str]) -> str:
        import httpx

        payload = {
            "model": self.model,
            "temperature": 0.3,
            "max_tokens": 2400,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}]
            + [{"role": "user", "content": m} for m in messages],
        }
        try:
            response = httpx.post(
                self.endpoint,
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            raise SynthesisError(f"Groq request failed: {exc}") from exc

        if response.status_code >= 400:
            raise SynthesisError(
                f"Groq returned HTTP {response.status_code}: {response.text[:300]}"
            )
        try:
            return response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as exc:
            raise SynthesisError(f"Unexpected Groq response shape: {response.text[:300]}") from exc


def build_provider(config: Config) -> LLMProvider:
    config.require_llm()
    if config.llm_provider == "groq":
        return GroqProvider(api_key=config.groq_api_key, model=config.groq_model)
    return GeminiProvider(api_key=config.gemini_api_key, model=config.gemini_model)


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------


def extract_json(text: str) -> dict[str, Any]:
    """Parse the model's reply, tolerating code fences and leading prose."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise SynthesisError(f"No JSON object in model output: {text[:200]!r}") from None
        try:
            parsed = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            raise SynthesisError(f"Model output is not valid JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise SynthesisError("Model returned JSON, but not an object.")
    return parsed


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_card(data: dict[str, Any], note: RawNote | None = None, today: date | None = None) -> Card:
    """Turn validated JSON into a `Card` with the canonical section layout."""
    today = today or date.today()

    question = (data.get("question") or (note.question if note else "")).strip()
    card_id = slugify(str(data.get("id") or "").strip() or question, max_length=48)
    topic = (data.get("topic") or (note.suggested_topic if note else "Uncategorised")).strip()

    matrix_lines = []
    for row in data.get("decision_matrix") or []:
        if not isinstance(row, dict):
            continue
        condition = _as_if(row.get("condition", ""))
        choice = (row.get("choice") or "").strip().rstrip(".")
        tradeoff = (row.get("tradeoff") or "").strip()
        line = f"- **{condition}:** {choice}"
        if tradeoff:
            line += f" — *trade-off:* {tradeoff}"
        matrix_lines.append(line)

    modifier_lines = []
    for i, mod in enumerate(data.get("constraint_modifiers") or [], 1):
        if not isinstance(mod, dict):
            continue
        name = (mod.get("name") or f"Modifier {i}").strip()
        effect = (mod.get("effect") or "").strip()
        modifier_lines.append(f"- *Modifier {i} ({name}):* {effect}")

    sections = {
        SECTION_MECHANISM: (data.get("direct_mechanism") or "").strip(),
        SECTION_MATRIX: "\n".join(matrix_lines),
        SECTION_TIPPING_POINT: (data.get("tipping_point") or "").strip(),
        SECTION_MODIFIERS: "\n".join(modifier_lines),
    }

    meta: dict[str, Any] = {
        "id": card_id,
        "topic": topic,
        "created": today.isoformat(),
        "next_review": today.isoformat(),  # new cards are due immediately
        "interval": 0,
        "ease_factor": 2.5,
        "repetition_count": 0,
    }
    if note:
        # Lets `--sync` recognise this note next run even though the LLM
        # rewrote the question (and therefore the id).
        meta["source_note_id"] = note.suggested_id
        # The Notion block id survives a reworded question; the content hash is
        # what tells a later sync that the note's body changed.
        if note.block_id:
            meta["source_block_id"] = note.block_id
        meta["source_hash"] = note.content_hash
        if note.source_url:
            meta["source_url"] = note.source_url

    card = Card(id=card_id, topic=topic, question=question, sections=sections, meta=meta)
    # Written only here, never by `save()`, so that rating a card does not reset
    # it — that is what makes hand-edit detection reliable.
    card.meta["body_hash"] = card.body_digest()
    return card


def _as_if(condition: str) -> str:
    condition = condition.strip().rstrip(":.")
    if not condition:
        return "IF unspecified"
    return condition if condition.upper().startswith("IF") else f"IF {condition}"


# ---------------------------------------------------------------------------
# Synthesizer
# ---------------------------------------------------------------------------


class Synthesizer:
    """Drives the provider, lints the result, and renders a `Card`."""

    def __init__(self, config: Config, provider: LLMProvider | None = None):
        self.config = config
        self.provider = provider or build_provider(config)
        self._last_call_at = 0.0

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call_at
        if self._last_call_at and elapsed < MIN_SECONDS_BETWEEN_CALLS:
            time.sleep(MIN_SECONDS_BETWEEN_CALLS - elapsed)
        self._last_call_at = time.monotonic()

    def _call(self, messages: list[str]) -> str:
        """One provider call with exponential backoff on transient failures."""
        last_exc: Exception | None = None
        for attempt in range(MAX_LLM_RETRIES):
            self._throttle()
            try:
                return self.provider.generate(SYSTEM_PROMPT, messages)
            except Exception as exc:  # noqa: BLE001 - provider SDKs raise freely
                last_exc = exc
                text = str(exc).lower()
                fatal = any(k in text for k in ("api key", "unauthorized", "permission denied", "401", "403"))
                if fatal or attempt == MAX_LLM_RETRIES - 1:
                    break
                delay = BASE_BACKOFF_SECONDS * (2**attempt)
                log.warning(
                    "%s call failed (%s); retrying in %.1fs [%d/%d]",
                    self.provider.name, exc, delay, attempt + 1, MAX_LLM_RETRIES,
                )
                time.sleep(delay)
        raise SynthesisError(f"{self.provider.name} synthesis failed: {last_exc}") from last_exc

    def synthesize(self, note: RawNote) -> Card:
        """Convert one raw note into a linted `Card`.

        Raises `SynthesisError` if the provider is unreachable or its output is
        unparseable. A card that merely fails the *quality* lint after one
        corrective retry is kept, with the violations logged.
        """
        prompt = USER_PROMPT_TEMPLATE.format(
            page_title=note.page_title or "(untitled)",
            breadcrumbs=" › ".join(note.breadcrumbs) or "(top level)",
            question=note.question,
            raw_answer=note.raw_answer[:8000],
        )

        messages = [prompt]
        data = extract_json(self._call(messages))
        problems = lint_card(data)

        if problems:
            log.info("Card %r failed lint (%d issue(s)); requesting a rewrite",
                     note.question[:50], len(problems))
            messages = [
                prompt,
                RETRY_PROMPT_TEMPLATE.format(
                    violations="\n".join(f"- {p}" for p in problems[:10])
                ),
            ]
            try:
                retry_data = extract_json(self._call(messages))
            except SynthesisError as exc:
                log.warning("Rewrite failed (%s); keeping the first draft", exc)
            else:
                retry_problems = lint_card(retry_data)
                if len(retry_problems) <= len(problems):
                    data, problems = retry_data, retry_problems

        if problems:
            log.warning(
                "Keeping card %r with %d unresolved lint issue(s): %s",
                data.get("id") or note.suggested_id, len(problems), problems[0],
            )

        return render_card(data, note=note)
