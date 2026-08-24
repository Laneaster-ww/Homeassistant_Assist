"""Offline-Abruf der Home-Assistant-Dokumentation fuer reine Dokumentationsantworten."""

from __future__ import annotations

import logging
import math
import os
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date
from html import unescape
from pathlib import Path

import aiohttp
import yaml

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DEFAULT_DOCS_URLS

_LOGGER = logging.getLogger(__name__)

OFFLINE_DOCS_FILENAME = "homeassistant_docs_offline.md"

DOCS_ONLY_SYSTEM_PROMPT = """Du beantwortest Home-Assistant-Dokumentationsfragen.

Policy:
- Nutze AUSSCHLIESSLICH den Inhalt unter "Gespeicherte Dokumentation".
- Nutze kein allgemeines Modellwissen und keine Vermutungen.
- Wenn die gespeicherte Dokumentation nicht reicht, antworte GENAU (in der unten
  festgelegten Sprache übersetzt, sonst wörtlich):
  Dazu finde ich in der gespeicherten Home-Assistant-Dokumentation keine ausreichende Information.
- Antworte in der Sprache mit dem Code "{language}" (ISO 639-1), kurz und konkret -
  unabhängig davon, dass diese Anleitung selbst auf Deutsch verfasst ist.
- Schreibe Umlaute und Sonderzeichen als echte Zeichen aus (deutsch: ä, ö, ü, Ä, Ö, Ü, ß),
  niemals als ae, oe, ue oder ss.
- Nenne am Ende die passende Quelle aus den angegebenen offiziellen URLs, wenn eine Antwort möglich ist.

Gespeicherte Dokumentation:
{context}
"""

DOCS_QUESTION_WORDS = (
    "wie",
    "was",
    "wo",
    "warum",
    "welche",
    "welcher",
    "welches",
    "erklaer",
    "erklär",
    "doku",
    "dokumentation",
    "hilfe",
    # Englische Entsprechungen - die Chat-Sprache ist nicht mehr fest Deutsch
    # (siehe ollama_client.SYSTEM_PROMPT_TEMPLATE).
    "how",
    "what",
    "where",
    "why",
    "which",
    "explain",
    "docs",
    "documentation",
    "help",
)

DOCS_TOPIC_WORDS = (
    "home assistant",
    "assist",
    "conversation",
    "sprache",
    "voice",
    "pipeline",
    "intent",
    "tts",
    "stt",
    "integration",
    "custom component",
    "custom integration",
    "manifest",
    "configuration.yaml",
    "yaml",
    "automation",
    "automatisierung",
    "service",
    "dashboard",
    "lovelace",
    "entity",
    "entities",
    "geraet",
    "gerät",
    "device",
    "devices",
)

ACTION_WORDS = (
    "schalte",
    "starte",
    "stoppe",
    "oeffne",
    "öffne",
    "schliesse",
    "schließe",
    "erstelle",
    "lege an",
    "loesche",
    "lösche",
    "mach",
    "setze",
    # Englische Entsprechungen - die Chat-Sprache ist nicht mehr fest Deutsch.
    "turn on",
    "turn off",
    "switch on",
    "switch off",
    "start",
    "stop",
    "open",
    "close",
    "create",
    "delete",
    "remove",
    "set",
    "make",
)

# Fragen nach dem eigenen, aktuellen Zustand ("was habe ich eingerichtet", "welche
# Automationen sind aktiv") sind KEINE Dokumentationsfragen, auch wenn sie zufaellig ein
# DOCS_TOPIC_WORDS-Stichwort enthalten (z.B. "automatisierung"): die Antwort steht nicht in
# der HA-Doku, sondern im laufenden System (siehe "Bestehende Automationen" im System-Prompt
# von ollama_client.py). Solche Fragen muessen ueber den normalen Chat-Weg laufen, sonst
# antwortet der Dokumentationsmodus faelschlich mit "keine ausreichende Information".
STATUS_QUERY_WORDS = (
    "eingerichtet",
    "aktiv",
    "aktuell",
    "vorhanden",
    "existieren",
    "existiert",
    "angelegt",
    "konfiguriert",
    "habe ich",
    "hab ich",
    # Englische Entsprechungen - die Chat-Sprache ist nicht mehr fest Deutsch.
    "configured",
    "active",
    "current",
    "existing",
    "exist",
    "exists",
    "set up",
    "have i",
    "do i have",
)


@dataclass(frozen=True)
class DocumentationPolicy:
    """Einstellungen fuer reine Dokumentationsantworten."""

    enabled: bool
    strict: bool
    offline_path: str
    source_urls: list[str]


def docs_policy_from_data(hass: HomeAssistant, policy_data: dict) -> DocumentationPolicy:
    """Liest die Dokumentations-Policy-Einstellungen aus smart_homeassistant_policy.yaml."""

    data = policy_data.get("documentation", {}) or {}
    offline_path = data.get("offline_path") or str(Path(__file__).with_name(OFFLINE_DOCS_FILENAME))
    if not os.path.isabs(offline_path):
        offline_path = hass.config.path(offline_path)

    return DocumentationPolicy(
        enabled=bool(data.get("enabled", True)),
        strict=bool(data.get("strict_docs_only", True)),
        offline_path=offline_path,
        source_urls=list(data.get("source_urls") or DEFAULT_DOCS_URLS),
    )


def contains_term(text: str, terms: tuple[str, ...]) -> bool:
    """Prueft, ob eines der Stichworte als Wortanfang im Text vorkommt.

    Bewusst am Wortanfang verankert statt als freier Substring: mit einem einfachen
    ``word in text`` hat z.B. das Aktionswort "set" mitten in "Uebersetzung", "Preset"
    oder "Reset" angeschlagen und damit harmlose Dokumentationsfragen faelschlich als
    Geraete-Aktion eingestuft. Das Wortende bleibt bewusst offen, damit deutsche
    Beugungen weiter greifen ("erstelle" -> "erstellen", "automatisierung" ->
    "automatisierungen").
    """

    return any(re.search(rf"\b{re.escape(term)}", text) for term in terms)


def is_documentation_question(text: str) -> bool:
    """Heuristische Erkennung von HA-Dokumentationsfragen, keine Geraete-Aktionen."""

    normalized = text.strip().lower()
    if not normalized:
        return False

    if contains_term(normalized, ACTION_WORDS):
        return False

    if contains_term(normalized, STATUS_QUERY_WORDS):
        return False

    has_question = "?" in normalized or any(
        normalized.startswith(word) or f" {word} " in normalized
        for word in DOCS_QUESTION_WORDS
    )
    has_topic = contains_term(normalized, DOCS_TOPIC_WORDS)
    return has_question and has_topic


def load_offline_docs(path: str) -> str:
    """Liest das Offline-Doku-Markdown."""

    with open(path, encoding="utf-8") as f:
        return f.read()


def save_offline_docs(path: str, text: str) -> None:
    """Speichert das Offline-Doku-Markdown."""

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _html_to_text(html: str) -> str:
    """Kleine, abhaengigkeitsfreie HTML-zu-Text-Umwandlung fuer Doku-Schnappschuesse."""

    html = re.sub(r"(?is)<(script|style|nav|footer|header).*?</\1>", " ", html)
    html = re.sub(r"(?is)<br\s*/?>", "\n", html)
    html = re.sub(r"(?is)</(p|div|li|h[1-6]|tr)>", "\n", html)
    html = re.sub(r"(?is)<[^>]+>", " ", html)
    text = unescape(html)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


async def refresh_official_docs(
    hass: HomeAssistant, docs_policy: DocumentationPolicy, previous_markdown: str = ""
) -> str:
    """Laedt die offizielle Home-Assistant-Doku herunter und liefert einen neuen Offline-Markdown-Schnappschuss.

    Jede Quelle wird einzeln abgesichert: eine nicht erreichbare oder umgezogene URL
    liess bisher den kompletten Service ``refresh_documentation`` scheitern, obwohl die
    uebrigen vier Quellen einwandfrei geladen haben. Fuer eine fehlgeschlagene Quelle
    wird stattdessen ihr Abschnitt aus ``previous_markdown`` uebernommen (falls
    vorhanden), sodass ein Refresh die gespeicherte Doku nie verkleinert. Nur wenn
    ueberhaupt kein Abschnitt zusammenkommt, wird der letzte Fehler geworfen - dann waere
    das Ergebnis eine leere Doku, die jede Dokumentationsfrage unbeantwortbar macht.
    """

    session = async_get_clientsession(hass)
    previous_sections = {
        title.removeprefix("Source: "): body for title, body in _split_sections(previous_markdown)
    }
    sections = []
    last_error: Exception | None = None
    timeout = aiohttp.ClientTimeout(total=60)
    for url in docs_policy.source_urls:
        try:
            async with session.get(url, timeout=timeout) as resp:
                resp.raise_for_status()
                html = await resp.text()
        except Exception as exc:  # noqa: BLE001 - eine kaputte Quelle darf die anderen nicht mitreissen
            last_error = exc
            fallback = previous_sections.get(url)
            _LOGGER.warning(
                "Doku-Quelle %s konnte nicht geladen werden (%s) - %s",
                url,
                exc,
                "bisheriger Stand bleibt erhalten" if fallback else "Abschnitt entfällt",
            )
            if fallback:
                sections.append(fallback)
            continue
        text = _html_to_text(html)
        sections.append(f"## Source: {url}\n\n{text[:12000]}")

    if not sections:
        raise RuntimeError(
            "Keine einzige Doku-Quelle konnte geladen werden - die gespeicherte "
            f"Dokumentation bleibt unverändert. Letzter Fehler: {last_error}"
        )

    sources = "\n".join(f"  - {url}" for url in docs_policy.source_urls)
    return (
        "---\n"
        "title: Home Assistant offline documentation snapshot\n"
        f"source:\n{sources}\n"
        f"last_reviewed: {date.today().isoformat()}\n"
        "policy: Answers to Home Assistant documentation questions must use only this file or the listed official URLs.\n"
        "---\n\n"
        "# Home Assistant Documentation Snapshot\n\n"
        "This file was refreshed from the configured official Home Assistant documentation URLs.\n"
        "If a question cannot be answered from the sections below, the assistant must say that the answer is not present in the saved documentation.\n\n"
        + "\n\n".join(sections)
        + "\n"
    )


def _tokenize(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-zA-Z0-9_äöüÄÖÜß.-]{3,}", text.lower())}


def _split_sections(markdown: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    current_title = "Home Assistant Documentation Snapshot"
    current_lines: list[str] = []

    for line in markdown.splitlines():
        if line.startswith("## "):
            if current_lines:
                sections.append((current_title, "\n".join(current_lines).strip()))
            current_title = line.removeprefix("## ").strip()
            current_lines = [line]
        else:
            current_lines.append(line)

    if current_lines:
        sections.append((current_title, "\n".join(current_lines).strip()))
    return sections


# Woerter, die in fast jeder Frage UND in fast jedem Doku-Abschnitt vorkommen. Sie
# tragen nichts zur Unterscheidung bei, blaehen aber jeden Treffer auf.
_STOPWORDS = frozenset(
    {
        "der", "die", "das", "den", "dem", "des", "ein", "eine", "einen", "einem", "einer",
        "und", "oder", "aber", "wie", "was", "wer", "wo", "warum", "welche", "welcher",
        "welches", "ist", "sind", "war", "waren", "kann", "koennen", "muss", "muessen",
        "soll", "sollen", "wird", "werden", "hat", "haben", "fuer", "mit", "von", "zum",
        "zur", "auf", "aus", "bei", "nach", "ueber", "unter", "durch", "nicht", "auch",
        "man", "ich", "sich", "dass", "wenn", "dann", "noch", "nur", "sehr", "mehr",
        "the", "and", "for", "with", "from", "that", "this", "these", "those", "you",
        "your", "are", "was", "were", "can", "will", "would", "should", "have", "has",
        "not", "but", "how", "what", "which", "where", "why", "when", "into", "out",
        "home", "assistant", "documentation", "docs", "source", "https", "http", "www",
    }
)

# Ungefaehre Zielgroesse eines durchsuchbaren Haeppchens. Die "## Source:"-Abschnitte
# des Schnappschusses sind bis zu 12.000 Zeichen gross - als Sucheinheit viel zu grob:
# ein einziger Treffer irgendwo darin zog den kompletten Abschnitt in den Kontext, und
# vom 6000-Zeichen-Budget blieb fuer die uebrigen Treffer nichts mehr uebrig.
_CHUNK_TARGET_CHARS = 1200


def _split_chunks(markdown: str) -> list[tuple[str, str]]:
    """Zerlegt den Schnappschuss in durchsuchbare Haeppchen ``(Quelle, Text)``.

    Grober Schnitt nach Quelle (siehe :func:`_split_sections`), feiner Schnitt entlang
    von Absatzgrenzen bis etwa :data:`_CHUNK_TARGET_CHARS` Zeichen erreicht sind.
    """

    chunks: list[tuple[str, str]] = []
    for title, body in _split_sections(markdown):
        lines = body.splitlines()
        if lines and lines[0].startswith("## "):
            body = "\n".join(lines[1:])

        buffer: list[str] = []
        size = 0
        for paragraph in re.split(r"\n\s*\n", body):
            paragraph = paragraph.strip()
            if not paragraph:
                continue
            buffer.append(paragraph)
            size += len(paragraph)
            if size >= _CHUNK_TARGET_CHARS:
                chunks.append((title, "\n\n".join(buffer)))
                buffer, size = [], 0
        if buffer:
            chunks.append((title, "\n\n".join(buffer)))
    return chunks


def retrieve_docs_context(markdown: str, question: str, max_sections: int = 4) -> str:
    """Liefert die relevantesten Offline-Doku-Abschnitte zu einer Frage.

    Gewichtet nach inverser Dokumenthaeufigkeit statt nach der blossen Zahl gemeinsamer
    Tokens: ohne Gewichtung zaehlte ein Treffer auf "home", "assistant" oder "wie"
    genauso viel wie einer auf "numeric_state". Weil solche Allerweltswoerter in
    praktisch jedem Abschnitt vorkommen, entschied am Ende die Abschnittslaenge statt
    der Relevanz - die laengste Quelle gewann fast immer.

    Jedem Haeppchen wird seine Quelle vorangestellt, damit das Modell die im
    ``DOCS_ONLY_SYSTEM_PROMPT`` verlangte Quellenangabe auch belegen kann.
    """

    query_terms = _tokenize(question) - _STOPWORDS
    if not query_terms:
        return ""

    chunks = [(title, body, _tokenize(f"{title}\n{body}")) for title, body in _split_chunks(markdown)]
    if not chunks:
        return ""

    total = len(chunks)
    document_frequency = Counter(
        term for _title, _body, terms in chunks for term in query_terms & terms
    )

    ranked = []
    for title, body, terms in chunks:
        score = sum(
            math.log(1 + total / document_frequency[term]) for term in query_terms & terms
        )
        if score:
            ranked.append((score, title, body))

    if not ranked:
        return ""

    ranked.sort(key=lambda item: item[0], reverse=True)
    context = "\n\n".join(f"## {title}\n\n{body}" for _score, title, body in ranked[:max_sections])
    return context[:6000]


def offline_docs_metadata(markdown: str) -> dict:
    """Liest die leichten Metadaten aus dem Markdown-Front-Matter."""

    if not markdown.startswith("---"):
        return {}

    parts = markdown.split("---", 2)
    if len(parts) < 3:
        return {}

    try:
        return yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return {}
