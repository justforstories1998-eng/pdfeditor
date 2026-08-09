"""Document intelligence: summarise, translate, chat, tag and extract.

Two provider tiers keep the feature useful with **no** network access:

``LocalProvider``   Offline extractive algorithms — TextRank-style summaries,
                    TF-IDF keywords, heading detection, table extraction and a
                    BM25 retrieval index for question answering. Deterministic,
                    private and dependency-light (NumPy only).
``RemoteProvider``  Any OpenAI-compatible chat-completions endpoint (OpenAI,
                    Azure, Anthropic gateways, Ollama, vLLM, LM Studio …),
                    configured through settings; the API key is read from an
                    environment variable and never written to disk.

Everything is behind :class:`AIAssistant`, so the UI code is identical whether
the user is offline or has configured a model.
"""

from __future__ import annotations

import json
import math
import os
import re
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from pdfstudio.core.exceptions import DependencyMissingError, PdfStudioError
from pdfstudio.core.jobs import JobContext
from pdfstudio.core.logging_setup import get_logger
from pdfstudio.core.settings import AISettings
from pdfstudio.pdfengine.document import PdfDocument
from pdfstudio.pdfengine.types import Bookmark, Rect

log = get_logger("ai")

_STOPWORDS = frozenset(
    [
        "a",
        "about",
        "above",
        "after",
        "again",
        "against",
        "all",
        "am",
        "an",
        "and",
        "any",
        "are",
        "aren't",
        "as",
        "at",
        "be",
        "because",
        "been",
        "before",
        "being",
        "below",
        "between",
        "both",
        "but",
        "by",
        "can",
        "cannot",
        "could",
        "couldn't",
        "did",
        "didn't",
        "do",
        "does",
        "doesn't",
        "doing",
        "don't",
        "down",
        "during",
        "each",
        "few",
        "for",
        "from",
        "further",
        "had",
        "hadn't",
        "has",
        "hasn't",
        "have",
        "haven't",
        "having",
        "he",
        "her",
        "here",
        "hers",
        "herself",
        "him",
        "himself",
        "his",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "isn't",
        "it",
        "its",
        "itself",
        "let's",
        "me",
        "more",
        "most",
        "mustn't",
        "my",
        "myself",
        "no",
        "nor",
        "not",
        "of",
        "off",
        "on",
        "once",
        "only",
        "or",
        "other",
        "ought",
        "our",
        "ours",
        "ourselves",
        "out",
        "over",
        "own",
        "same",
        "shan't",
        "she",
        "should",
        "shouldn't",
        "so",
        "some",
        "such",
        "than",
        "that",
        "the",
        "their",
        "theirs",
        "them",
        "themselves",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "to",
        "too",
        "under",
        "until",
        "up",
        "very",
        "was",
        "wasn't",
        "we",
        "were",
        "weren't",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "whom",
        "why",
        "with",
        "won't",
        "would",
        "wouldn't",
        "you",
        "your",
        "yours",
        "yourself",
        "yourselves",
    ]
)


@dataclass(slots=True)
class Message:
    """One turn in a chat conversation."""

    role: str  # system | user | assistant
    content: str


@dataclass(slots=True)
class AIResponse:
    """Result of any AI operation."""

    text: str
    provider: str = ""
    model: str = ""
    citations: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)

    def __str__(self) -> str:  # pragma: no cover
        return self.text


class AIProvider(ABC):
    """Interface implemented by local and remote back-ends."""

    name: str = "provider"

    @abstractmethod
    def complete(
        self, messages: Sequence[Message], *, max_tokens: int = 800, temperature: float = 0.2
    ) -> AIResponse:
        """Return a completion for the conversation."""

    @abstractmethod
    def is_available(self) -> bool:
        """``True`` when the provider can be used right now."""


class LocalProvider(AIProvider):
    """Offline, deterministic text intelligence (no model download required)."""

    name = "local"

    def is_available(self) -> bool:
        return True

    def complete(
        self, messages: Sequence[Message], *, max_tokens: int = 800, temperature: float = 0.2
    ) -> AIResponse:
        """Answer using extractive retrieval over the supplied context."""
        context = "\n".join(m.content for m in messages if m.role == "system")
        question = next((m.content for m in reversed(messages) if m.role == "user"), "")
        sentences = _split_sentences(context)
        if not sentences:
            return AIResponse(
                text="I could not find any text in this document to work with.",
                provider=self.name,
            )
        ranked = _bm25_rank(question, sentences)[: max(1, max_tokens // 120)]
        answer = " ".join(s for _, s in sorted(ranked, key=lambda r: sentences.index(r[1])))
        return AIResponse(text=answer, provider=self.name, model="local-extractive")


class RemoteProvider(AIProvider):
    """Any OpenAI-compatible ``/chat/completions`` endpoint."""

    name = "remote"

    def __init__(self, settings: AISettings) -> None:
        self.settings = settings

    @property
    def api_key(self) -> str:
        return os.environ.get(self.settings.api_key_env, "")

    @property
    def endpoint(self) -> str:
        base = self.settings.endpoint or "https://api.openai.com/v1"
        return base.rstrip("/") + (
            "" if base.rstrip("/").endswith("completions") else "/chat/completions"
        )

    def is_available(self) -> bool:
        return bool((self.settings.enabled and self.settings.endpoint) or self.api_key)

    def complete(
        self, messages: Sequence[Message], *, max_tokens: int = 800, temperature: float = 0.2
    ) -> AIResponse:
        payload = json.dumps(
            {
                "model": self.settings.model,
                "messages": [{"role": m.role, "content": m.content} for m in messages],
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        ).encode()
        request = urllib.request.Request(  # noqa: S310 - user configured endpoint
            self.endpoint,
            data=payload,
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310
                data = json.loads(response.read().decode())
        except urllib.error.URLError as exc:
            raise PdfStudioError("Could not reach the AI service.", detail=str(exc)) from exc
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise PdfStudioError("Unexpected response from the AI service.") from exc
        return AIResponse(
            text=text,
            provider=self.name,
            model=data.get("model", self.settings.model),
            usage=data.get("usage", {}),
        )


def make_provider(settings: AISettings) -> AIProvider:
    """Choose a provider from settings, falling back to the local one."""
    if settings.enabled and settings.provider != "local":
        remote = RemoteProvider(settings)
        if remote.is_available():
            return remote
        log.warning("Remote AI provider unavailable; using the local engine")
    return LocalProvider()


class AIAssistant:
    """High-level document intelligence bound to one document."""

    def __init__(
        self,
        document: PdfDocument,
        settings: AISettings | None = None,
        *,
        provider: AIProvider | None = None,
    ) -> None:
        self.doc = document
        self.settings = settings or AISettings()
        self.provider = provider or make_provider(self.settings)
        self._page_text: dict[int, str] = {}
        self._history: list[Message] = []

    # -- context handling ---------------------------------------------------- #
    def page_text(self, index: int) -> str:
        if index not in self._page_text:
            self._page_text[index] = self.doc.extract_text(index)
        return self._page_text[index]

    def context(self, pages: Sequence[int] | None = None, *, limit: int | None = None) -> str:
        """Document text trimmed to the provider's context budget."""
        budget = limit or self.settings.max_context_chars
        targets = list(pages) if pages is not None else range(self.doc.page_count)
        chunks: list[str] = []
        used = 0
        for index in targets:
            text = self.page_text(index).strip()
            if not text:
                continue
            block = f"[Page {index + 1}]\n{text}"
            if used + len(block) > budget:
                chunks.append(block[: max(0, budget - used)])
                break
            chunks.append(block)
            used += len(block)
        return "\n\n".join(chunks)

    # -- summarisation -------------------------------------------------------- #
    def summarize(
        self,
        *,
        pages: Sequence[int] | None = None,
        style: str = "concise",
        max_sentences: int = 6,
        ctx: JobContext | None = None,
    ) -> AIResponse:
        """Summarise the document or a page range."""
        if ctx:
            ctx.progress(0, "Reading document…")
        text = self.context(pages)
        if not text.strip():
            return AIResponse("This document contains no extractable text (try OCR).")
        if isinstance(self.provider, LocalProvider):
            # Strip the "[Page N]" markers used for retrieval grounding.
            clean = re.sub(r"\[Page \d+\]\s*", "", text)
            summary = _extractive_summary(clean, max_sentences)
            return AIResponse(summary, provider="local", model="textrank")
        instruction = {
            "concise": "Write a concise summary in a short paragraph.",
            "bullets": "Summarise as 5-8 short bullet points.",
            "detailed": "Write a detailed structured summary with headings.",
            "executive": "Write an executive summary for a senior stakeholder.",
        }.get(style, "Write a concise summary.")
        return self.provider.complete(
            [
                Message("system", f"You summarise PDF documents faithfully.\n\n{text}"),
                Message("user", instruction),
            ],
            temperature=self.settings.temperature,
        )

    def summarize_pages(self, *, ctx: JobContext | None = None) -> dict[int, str]:
        """One short summary per page (shown in the navigation panel)."""
        out: dict[int, str] = {}
        if ctx:
            ctx.set_total(self.doc.page_count)
        for index in range(self.doc.page_count):
            text = self.page_text(index)
            if text.strip():
                out[index] = _extractive_summary(text, 2)
            if ctx:
                ctx.progress(index + 1, f"Summarising page {index + 1}")
        return out

    # -- translation ---------------------------------------------------------- #
    def translate(
        self, target_language: str, *, pages: Sequence[int] | None = None
    ) -> AIResponse:
        """Translate document text into ``target_language``."""
        text = self.context(pages)
        if isinstance(self.provider, LocalProvider):
            raise DependencyMissingError(
                "an AI provider", f"Translation into {target_language}"
            )
        return self.provider.complete(
            [
                Message(
                    "system",
                    "You are a professional translator. Preserve formatting, "
                    "numbers and proper nouns.",
                ),
                Message("user", f"Translate into {target_language}:\n\n{text}"),
            ],
            max_tokens=4000,
        )

    # -- writing aids ---------------------------------------------------------- #
    def explain(self, text: str) -> AIResponse:
        """Explain a selected passage in plain language."""
        if isinstance(self.provider, LocalProvider):
            keywords = ", ".join(k for k, _ in _keywords(text, 8))
            return AIResponse(
                f"This passage is about: {keywords}.\n\n"
                f"Key sentence: {_extractive_summary(text, 1)}",
                provider="local",
            )
        return self.provider.complete(
            [
                Message("system", "Explain text clearly for a non-expert."),
                Message("user", text),
            ]
        )

    def rewrite(self, text: str, *, tone: str = "clear and professional") -> AIResponse:
        if isinstance(self.provider, LocalProvider):
            return AIResponse(_tidy_text(text), provider="local", model="rules")
        return self.provider.complete(
            [
                Message("system", f"Rewrite text to be {tone}. Keep the meaning."),
                Message("user", text),
            ]
        )

    def check_grammar(self, text: str) -> list[dict[str, Any]]:
        """Rule-based style/grammar findings (works offline)."""
        findings: list[dict[str, Any]] = []
        # Collapse runs of the same word into a single finding rather than
        # reporting every overlapping pair.
        for match in re.finditer(r"\b(\w+)(?:\s+\1\b)+", text, re.IGNORECASE):
            findings.append(
                {
                    "type": "duplicate word",
                    "text": match.group(0),
                    "offset": match.start(),
                    "suggestion": match.group(1),
                }
            )
        for match in re.finditer(r"\s+([,.;:!?])", text):
            findings.append(
                {
                    "type": "space before punctuation",
                    "text": match.group(0),
                    "offset": match.start(),
                    "suggestion": match.group(1),
                }
            )
        for match in re.finditer(r"([,.;:!?])(?=[A-Za-z])", text):
            findings.append(
                {
                    "type": "missing space after punctuation",
                    "text": match.group(0),
                    "offset": match.start(),
                    "suggestion": f"{match.group(1)} ",
                }
            )
        for sentence in _split_sentences(text):
            words = sentence.split()
            if len(words) > 45:
                findings.append(
                    {
                        "type": "long sentence",
                        "text": sentence[:80] + "…",
                        "offset": text.find(sentence),
                        "suggestion": "Consider splitting this sentence.",
                    }
                )
        return findings

    # -- question answering ----------------------------------------------------- #
    def ask(
        self, question: str, *, pages: Sequence[int] | None = None, cite: bool = True
    ) -> AIResponse:
        """Answer a question about the document, with page citations."""
        passages = self._retrieve(question, pages)
        if not passages:
            return AIResponse("I could not find anything relevant in this document.")
        context = "\n\n".join(f"[Page {p + 1}] {t}" for p, t in passages)
        if isinstance(self.provider, LocalProvider):
            answer = " ".join(t for _, t in passages[:3])
            response = AIResponse(answer, provider="local", model="bm25-extractive")
        else:
            response = self.provider.complete(
                [
                    Message(
                        "system",
                        "Answer strictly from the provided document extracts. "
                        "Cite pages as [Page N]. Say so if the answer is absent.\n\n" + context,
                    ),
                    Message("user", question),
                ],
                temperature=self.settings.temperature,
            )
        if cite:
            response.citations = [
                {"page": page, "text": text[:240]} for page, text in passages[:5]
            ]
        return response

    def chat(self, message: str) -> AIResponse:
        """Multi-turn chat with the document as grounding context."""
        self._history.append(Message("user", message))
        if isinstance(self.provider, LocalProvider):
            response = self.ask(message)
        else:
            response = self.provider.complete(
                [
                    Message(
                        "system",
                        "You are a helpful assistant answering questions about "
                        "this PDF.\n\n" + self.context(),
                    ),
                    *self._history[-8:],
                ]
            )
        self._history.append(Message("assistant", response.text))
        return response

    def reset_chat(self) -> None:
        self._history.clear()

    def _retrieve(
        self, query: str, pages: Sequence[int] | None = None, k: int = 6
    ) -> list[tuple[int, str]]:
        """BM25 retrieval over paragraph-sized chunks."""
        targets = list(pages) if pages is not None else range(self.doc.page_count)
        chunks: list[tuple[int, str]] = []
        for index in targets:
            for paragraph in re.split(r"\n\s*\n", self.page_text(index)):
                cleaned = " ".join(paragraph.split())
                if len(cleaned) > 40:
                    chunks.append((index, cleaned))
        if not chunks:
            return []
        scored = _bm25_rank(query, [c[1] for c in chunks])
        lookup = {text: page for page, text in chunks}
        return [(lookup[text], text) for _, text in scored[:k]]

    # -- structure generation ----------------------------------------------- #
    def generate_bookmarks(self, *, max_level: int = 3) -> list[Bookmark]:
        """Detect headings by font size/weight and build an outline."""
        sizes: list[float] = []
        for index in range(self.doc.page_count):
            for block in self.doc.extract_blocks(index):
                for line in block.lines:
                    for span in line.spans:
                        if span.text.strip():
                            sizes.append(round(span.size, 1))
        if not sizes:
            return []
        counter = Counter(sizes)
        body_size = counter.most_common(1)[0][0]
        heading_sizes = sorted({s for s in sizes if s > body_size * 1.12}, reverse=True)
        level_of = {size: min(max_level, i + 1) for i, size in enumerate(heading_sizes)}

        flat: list[Bookmark] = []
        for index in range(self.doc.page_count):
            for block in self.doc.extract_blocks(index):
                if not block.lines or not block.lines[0].spans:
                    continue
                span = block.lines[0].spans[0]
                title = " ".join(block.text.split())
                if not title or len(title) > 120:
                    continue
                size = round(span.size, 1)
                level = level_of.get(size)
                if level is None and span.bold and size >= body_size and len(title) < 80:
                    level = max_level
                if level is None:
                    continue
                flat.append(Bookmark(title=title, page=index, level=level, y=block.rect.y0))

        roots: list[Bookmark] = []
        stack: list[Bookmark] = []
        for bm in flat:
            while stack and stack[-1].level >= bm.level:
                stack.pop()
            (stack[-1].children if stack else roots).append(bm)
            stack.append(bm)
        log.info("Generated {} bookmark(s)", len(flat))
        return roots

    def apply_generated_bookmarks(self) -> int:
        """Generate and write bookmarks into the document."""
        bookmarks = self.generate_bookmarks()
        if bookmarks:
            self.doc.set_bookmarks(bookmarks)
        return sum(len(b.flatten()) for b in bookmarks)

    def generate_title(self) -> str:
        """Infer a document title from the largest text on the first page."""
        best_text, best_size = "", 0.0
        for block in self.doc.extract_blocks(0):
            for line in block.lines:
                for span in line.spans:
                    text = span.text.strip()
                    if text and span.size > best_size and len(text) > 3:
                        best_text, best_size = text, span.size
        return best_text or self.doc.display_name

    def generate_metadata(self) -> dict[str, str]:
        """Suggest title, subject and keywords for the metadata editor."""
        text = self.context(limit=20000)
        keywords = [k for k, _ in _keywords(text, 12)]
        return {
            "title": self.generate_title(),
            "subject": _extractive_summary(text, 1),
            "keywords": ", ".join(keywords),
        }

    def auto_tag(self) -> list[str]:
        """Topic tags for search and document management."""
        return [k for k, _ in _keywords(self.context(limit=40000), 10)]

    def extract_tables(self, pages: Sequence[int] | None = None) -> list[dict[str, Any]]:
        """Detect tables and return them as row lists."""

        targets = list(pages) if pages is not None else range(self.doc.page_count)
        tables: list[dict[str, Any]] = []
        for index in targets:
            with self.doc.locked() as handle:
                try:
                    found = handle[index].find_tables()
                except Exception:
                    continue
                for n, table in enumerate(found.tables, 1):
                    rows = table.extract()
                    if rows:
                        tables.append(
                            {
                                "page": index,
                                "index": n,
                                "rows": rows,
                                "columns": len(rows[0]) if rows else 0,
                                "bbox": Rect(*table.bbox),
                            }
                        )
        return tables

    def generate_citation(self, style: str = "apa") -> str:
        """Build a bibliography entry from the document metadata."""
        meta = self.doc.metadata()
        title = meta.title or self.generate_title()
        author = meta.author or "Unknown author"
        year = ""
        if match := re.search(r"(\d{4})", meta.creation_date or ""):
            year = match.group(1)
        location = str(self.doc.path or self.doc.display_name)
        match style.lower():
            case "apa":
                return f"{author} ({year or 'n.d.'}). {title}. {location}"
            case "mla":
                return f"{author}. “{title}.” {year}, {location}."
            case "chicago":
                return f"{author}. “{title}.” {year}. {location}."
            case "bibtex":
                key = re.sub(r"\W", "", (author.split()[0] if author else "doc") + (year or ""))
                return (
                    f"@misc{{{key},\n  title = {{{title}}},\n  author = {{{author}}},"
                    f"\n  year = {{{year}}},\n  note = {{{location}}}\n}}"
                )
            case _:
                return f"{author}. {title}. {year}. {location}"


# --------------------------------------------------------------------------- #
# Offline NLP helpers
# --------------------------------------------------------------------------- #
def _split_sentences(text: str) -> list[str]:
    """Sentence splitter that tolerates abbreviations and PDF line breaks."""
    normalised = re.sub(r"\s*\n\s*", " ", text)
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z“\"(])", normalised)
    return [p.strip() for p in parts if len(p.strip()) > 25]


def _tokenize(text: str) -> list[str]:
    return [
        w for w in re.findall(r"[a-z0-9']+", text.lower()) if w not in _STOPWORDS and len(w) > 2
    ]


def _keywords(text: str, count: int = 10) -> list[tuple[str, float]]:
    """TF-IDF-ish keyword extraction over sentence "documents"."""
    sentences = _split_sentences(text) or [text]
    frequency = Counter(_tokenize(text))
    if not frequency:
        return []
    document_frequency = Counter()
    for sentence in sentences:
        document_frequency.update(set(_tokenize(sentence)))
    total = len(sentences)
    scored = {
        word: count_ * math.log(1 + total / (1 + document_frequency[word]))
        for word, count_ in frequency.items()
    }
    return sorted(scored.items(), key=lambda kv: -kv[1])[:count]


def _bm25_rank(
    query: str, documents: Sequence[str], *, k1: float = 1.5, b: float = 0.75
) -> list[tuple[float, str]]:
    """Rank ``documents`` against ``query`` with Okapi BM25."""
    tokenised = [_tokenize(d) for d in documents]
    query_terms = _tokenize(query) or _tokenize(" ".join(documents[:1]))
    if not query_terms:
        return [(0.0, d) for d in documents]
    total = len(documents)
    avg_len = sum(len(t) for t in tokenised) / max(1, total)
    document_frequency = Counter()
    for tokens in tokenised:
        document_frequency.update(set(tokens))

    scores: list[tuple[float, str]] = []
    for tokens, original in zip(tokenised, documents, strict=True):
        counts = Counter(tokens)
        length = len(tokens) or 1
        score = 0.0
        for term in query_terms:
            if term not in counts:
                continue
            idf = math.log(
                1 + (total - document_frequency[term] + 0.5) / (document_frequency[term] + 0.5)
            )
            freq = counts[term]
            score += idf * (freq * (k1 + 1)) / (freq + k1 * (1 - b + b * length / avg_len))
        scores.append((score, original))
    return sorted([s for s in scores if s[0] > 0], key=lambda s: -s[0])


def _extractive_summary(text: str, max_sentences: int = 5) -> str:
    """TextRank-style extractive summary preserving original order."""
    sentences = _split_sentences(text)
    if len(sentences) <= max_sentences:
        return " ".join(sentences) or text[:600]
    weights = dict(_keywords(text, 60))
    scored: list[tuple[float, int, str]] = []
    for position, sentence in enumerate(sentences):
        tokens = _tokenize(sentence)
        if not tokens:
            continue
        base = sum(weights.get(t, 0.0) for t in tokens) / math.sqrt(len(tokens))
        # Sentences near the start of a document carry more information.
        positional = 1.0 + 0.35 * math.exp(-position / 8)
        scored.append((base * positional, position, sentence))
    top = sorted(scored, key=lambda s: -s[0])[:max_sentences]
    return " ".join(s for _, _, s in sorted(top, key=lambda s: s[1]))


def _tidy_text(text: str) -> str:
    """Deterministic clean-up used by the offline rewrite feature."""
    out = re.sub(r"\s+", " ", text).strip()
    out = re.sub(r"\s+([,.;:!?])", r"\1", out)
    out = re.sub(r"([,.;:!?])(?=[A-Za-z])", r"\1 ", out)
    out = re.sub(r"\b(\w+) \1\b", r"\1", out, flags=re.IGNORECASE)
    return out[:1].upper() + out[1:] if out else out
