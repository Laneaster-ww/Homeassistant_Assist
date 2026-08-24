"""Provider-uebergreifende LLM-Clients: Ollama, OpenAI-kompatibel, Anthropic, Google Gemini.

Jeder Client bekommt exakt dieselbe Aufgabe wie der urspruengliche, alleinige
:class:`ollama_client.OllamaClient`: das gemeinsame :class:`ollama_client.LLMOutput`-Schema
erzwingen bzw. nachtraeglich validieren, und dieselben zwei Methoden anbieten (``ask``/
``ask_plain``) - ``conversation.py`` behandelt dadurch jeden Provider identisch, unabhaengig
vom tatsaechlichen API-Format dahinter. Neue Provider werden ausschliesslich ueber
:func:`create_client` erzeugt, nie direkt instanziiert - das haelt die Zuordnung
Provider-Typ -> Client an einer einzigen Stelle.

Alle vier Clients sind gegen die jeweils oeffentlich dokumentierten API-Formate der Anbieter
gebaut (Stand: Implementierungszeitpunkt); ohne eigene Zugangsdaten zu jedem Dienst konnten
Anthropic/Gemini/OpenAI hier nicht gegen echten Live-Traffic getestet werden - bei
Formatabweichungen eines Anbieters schlaegt die Anfrage mit einer aussagekraeftigen
Fehlermeldung fehl, statt still falsche Daten zu liefern.
"""

from __future__ import annotations

import logging
from typing import Protocol

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DEFAULT_OLLAMA_URL
from .ollama_client import (
    LLMHTTPError,
    LLMOutput,
    OllamaClient,
    clean_api_key,
    extract_json,
    guard_api_key_transport,
    raise_for_status_with_body,
)
from .providers import ModelProvider

_LOGGER = logging.getLogger(__name__)

# CPU-Inferenz eines groesseren lokalen Modells mit erzwungenem JSON-Schema kann bei
# laengeren Antworten (z.B. dashboard_view_yaml) deutlich ueber eine Minute dauern; bei
# Cloud-Providern grosszuegig genug fuer laengere Denk-/Tool-Use-Antworten. Bewusst nicht
# mehr 300s - Entwuerfe werden bis zu dreimal erzeugt (siehe conversation), 3x300s haetten
# den Chat eine Viertelstunde blockiert.
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=120)

# Statuscodes, bei denen es sich lohnt, dieselbe Anfrage ohne die strukturierte
# Formatvorgabe zu wiederholen: der Dienst kennt das Feature nicht oder mag das Schema
# nicht. 401/403 (Key) und 429 (Kontingent) fehlen bewusst - die werden dadurch nicht
# besser, ein zweiter Versuch kostet dort nur Zeit.
_FORMAT_FALLBACK_STATUSES = frozenset({400, 404, 415, 422, 501})

# Schluessel, die Geminis Schema-Dialekt nicht kennt (siehe _gemini_schema).
_GEMINI_UNSUPPORTED_SCHEMA_KEYS = frozenset(
    {"$schema", "$defs", "$ref", "definitions", "title", "default", "additionalProperties"}
)


def _gemini_schema(schema: dict) -> dict:
    """Uebersetzt das Pydantic-JSON-Schema in Geminis OpenAPI-3.0-Subset.

    ``generationConfig.responseSchema`` versteht nur eine Teilmenge von JSON Schema und
    weist unbekannte Schluessel mit HTTP 400 zurueck ("Unknown name \"$defs\""). Genau
    solche enthaelt das von ``LLMOutput.model_json_schema()`` erzeugte Schema aber:
    ``$defs``/``$ref`` (durch das verschachtelte ``PlanStep``-Modell), fuer jedes
    ``Optional``-Feld ein ``anyOf: [{"type": "string"}, {"type": "null"}]`` sowie
    ``title``/``default``/``additionalProperties``. Ohne diese Umwandlung schlug bei
    Gemini jede strukturierte Anfrage fehl.

    Umgewandelt wird:

    * ``$ref`` -> die Definition aus ``$defs`` wird inline eingesetzt,
    * ``anyOf: [X, null]`` -> ``X`` mit ``nullable: true``,
    * nicht unterstuetzte Schluessel -> entfallen.

    Was sich damit trotzdem nicht abbilden laesst (etwa das frei geformte
    ``service_data``-Objekt ohne feste Properties), faengt der Fallback in
    :meth:`GeminiClient.ask` ab.
    """

    defs = schema.get("$defs", {})

    def convert(node):
        if isinstance(node, list):
            return [convert(item) for item in node]
        if not isinstance(node, dict):
            return node

        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            resolved = dict(defs.get(ref[len("#/$defs/") :], {}))
            resolved.update({key: value for key, value in node.items() if key != "$ref"})
            return convert(resolved)

        any_of = node.get("anyOf")
        if isinstance(any_of, list):
            non_null = [
                branch
                for branch in any_of
                if not (isinstance(branch, dict) and branch.get("type") == "null")
            ]
            if len(non_null) == 1:
                merged = dict(non_null[0])
                merged.update({key: value for key, value in node.items() if key != "anyOf"})
                converted = convert(merged)
                if len(non_null) != len(any_of):
                    converted["nullable"] = True
                return converted

        return {
            key: convert(value)
            for key, value in node.items()
            if key not in _GEMINI_UNSUPPORTED_SCHEMA_KEYS
        }

    return convert(schema)


class LLMClient(Protocol):
    """Gemeinsame Schnittstelle aller Provider-Clients (siehe Modul-Docstring)."""

    async def ask(self, messages: list[dict]) -> LLMOutput: ...

    async def ask_plain(self, system_prompt: str, user_prompt: str) -> str: ...


class OpenAICompatibleClient:
    """OpenAI-kompatible Chat-Completions-API.

    Deckt echtes OpenAI ebenso ab wie OpenRouter, LM Studio, Ollamas eigenen
    OpenAI-kompatiblen Endpoint oder jeden anderen Dienst, der ``/chat/completions``
    im OpenAI-Format anbietet - ``url`` bestimmt, welcher Server angesprochen wird.
    """

    DEFAULT_URL = "https://api.openai.com/v1"

    def __init__(self, hass: HomeAssistant, url: str, api_key: str, model: str) -> None:
        self.hass = hass
        self.url = self._normalize_base_url(url or self.DEFAULT_URL)
        self.api_key = clean_api_key(api_key)
        self.model = model
        guard_api_key_transport(self.url, self.api_key)

    @staticmethod
    def _normalize_base_url(url: str) -> str:
        """Macht aus einer versehentlich vollstaendigen Endpoint-URL wieder die Basis-URL.

        ``ask``/``ask_plain`` haengen "/chat/completions" selbst an. Wer stattdessen die
        vollstaendige Endpoint-URL aus der Anbieter-Doku kopiert - bei OpenRouter steht
        genau die dort gross auf der Startseite - landete bei
        ".../v1/chat/completions/chat/completions" und bekam ein 404, dessen Ursache im
        Chat ueberhaupt nicht erkennbar war. Beide Schreibweisen fuehren jetzt zum Ziel.
        """

        cleaned = url.strip().rstrip("/")
        for suffix in ("/chat/completions", "/completions"):
            if cleaned.endswith(suffix):
                cleaned = cleaned[: -len(suffix)]
        return cleaned

    def _headers(self) -> dict:
        """Auth-Header. Ohne Key bewusst gar keiner - lokale Endpunkte brauchen keinen."""

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        # OpenRouter ordnet Anfragen ueber diesen Header der aufrufenden Anwendung zu und
        # zeigt sie in der eigenen Nutzungsuebersicht getrennt an. Kein "HTTP-Referer":
        # dafuer gibt es keine echte Adresse, und eine erfundene waere eine falsche Angabe.
        if "openrouter.ai" in self.url:
            headers["X-Title"] = "Smart Homeassistant"
        return headers

    # Kombinationen aus (Basis-URL, Modell), die "response_format" nicht koennen. Ohne
    # dieses Gedaechtnis laeuft jede weitere Anfrage erneut in denselben 400er und kostet
    # den Nutzer jedes Mal eine zusaetzliche Runde Wartezeit. Klassenweit statt pro
    # Instanz, weil fuer jede einzelne Anfrage ein neuer Client gebaut wird
    # (siehe create_client).
    _unsupported_response_format: set[tuple[str, str]] = set()

    async def _post_chat(self, payload: dict) -> dict:
        """Ein Aufruf von ``/chat/completions`` samt aussagekraeftiger Fehlermeldung."""

        session = async_get_clientsession(self.hass)
        async with session.post(
            f"{self.url}/chat/completions",
            json=payload,
            headers=self._headers(),
            timeout=_REQUEST_TIMEOUT,
        ) as resp:
            await raise_for_status_with_body(resp, f"OpenAI-kompatibel ({self.model})")
            return await resp.json()

    @staticmethod
    def _content_of(data: dict) -> str:
        """Der Antworttext aus einer Chat-Completions-Antwort."""

        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError(f"Unerwartetes OpenAI-Antwortformat: {data!r}") from exc

    async def ask(self, messages: list[dict]) -> LLMOutput:
        """Erzwingt das LLMOutput-Schema ueber ``response_format`` - mit Rueckfallebenen.

        Von streng nach nachsichtig: erzwungenes JSON-Schema -> blosser JSON-Modus ->
        gar keine Formatvorgabe. Nicht jeder OpenAI-kompatible Dienst kann
        ``json_schema`` (etliche der freien Modelle bei OpenRouter zum Beispiel nicht,
        und manche Anbieter verlangen dafuer zusaetzlich ``strict: true`` mit
        ``additionalProperties: false``). Bisher sah so ein 400er wie ein Totalausfall
        aus, obwohl das Modell die Antwort ohne die Vorgabe problemlos liefert -
        ``extract_json`` und ``LLMOutput.model_validate`` pruefen sie danach ohnehin.
        """

        base_payload = {"model": self.model, "messages": messages, "temperature": 0.2}
        cache_key = (self.url, self.model)
        attempts: list[tuple[str, dict | None]] = [
            (
                "json_schema",
                {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "llm_output",
                        "schema": LLMOutput.model_json_schema(),
                        "strict": False,
                    },
                },
            ),
            ("json_object", {"type": "json_object"}),
            ("ohne Formatvorgabe", None),
        ]
        if cache_key in self._unsupported_response_format:
            attempts = attempts[1:]

        for position, (label, response_format) in enumerate(attempts):
            payload = dict(base_payload)
            if response_format is not None:
                payload["response_format"] = response_format
            try:
                data = await self._post_chat(payload)
            except LLMHTTPError as exc:
                is_last = position == len(attempts) - 1
                if is_last or exc.status not in _FORMAT_FALLBACK_STATUSES:
                    raise
                if label == "json_schema":
                    self._unsupported_response_format.add(cache_key)
                _LOGGER.warning(
                    "Modell %s hat die Formatvorgabe '%s' abgelehnt (%s) - naechster "
                    "Versuch mit '%s'",
                    self.model,
                    label,
                    exc,
                    attempts[position + 1][0],
                )
                continue
            return LLMOutput.model_validate(extract_json(self._content_of(data)))

        # Unerreichbar: die Schleife kehrt entweder zurueck oder wirft im letzten Durchlauf.
        raise ValueError(f"Keine Antwort von Modell {self.model} erhalten.")

    async def ask_plain(self, system_prompt: str, user_prompt: str) -> str:
        data = await self._post_chat(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.2,
            }
        )
        return self._content_of(data).strip()


class AnthropicClient:
    """Anthropic Messages API.

    Kein natives ``response_format`` wie bei OpenAI - stattdessen die dafuer uebliche
    Technik: ein einzelnes Tool mit dem LLMOutput-Schema definieren und ``tool_choice``
    auf genau dieses Tool zwingen, dann die Tool-Eingabe als strukturierte Antwort lesen.
    """

    DEFAULT_URL = "https://api.anthropic.com/v1"
    ANTHROPIC_VERSION = "2023-06-01"

    def __init__(self, hass: HomeAssistant, url: str, api_key: str, model: str) -> None:
        self.hass = hass
        self.url = (url or self.DEFAULT_URL).rstrip("/")
        self.api_key = clean_api_key(api_key)
        self.model = model
        guard_api_key_transport(self.url, self.api_key)

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": self.ANTHROPIC_VERSION,
        }

    @staticmethod
    def _split_system(messages: list[dict]) -> tuple[str, list[dict]]:
        """Anthropic nimmt den System-Prompt getrennt entgegen, nicht als Message-Rolle."""

        system_parts = [m["content"] for m in messages if m.get("role") == "system"]
        rest = [m for m in messages if m.get("role") != "system"]
        return "\n\n".join(system_parts), rest

    async def ask(self, messages: list[dict]) -> LLMOutput:
        system_prompt, chat_messages = self._split_system(messages)
        session = async_get_clientsession(self.hass)
        payload = {
            "model": self.model,
            "max_tokens": 4096,
            "system": system_prompt,
            "messages": chat_messages,
            "temperature": 0.2,
            "tools": [
                {
                    "name": "llm_output",
                    "description": "Strukturierte Antwort im vorgegebenen Format.",
                    "input_schema": LLMOutput.model_json_schema(),
                }
            ],
            "tool_choice": {"type": "tool", "name": "llm_output"},
        }
        async with session.post(
            f"{self.url}/messages",
            json=payload,
            headers=self._headers(),
            timeout=_REQUEST_TIMEOUT,
        ) as resp:
            await raise_for_status_with_body(resp, f"Anthropic ({self.model})")
            data = await resp.json()

        for block in data.get("content", []):
            if block.get("type") == "tool_use":
                return LLMOutput.model_validate(block["input"])
        raise ValueError(f"Anthropic-Antwort enthielt keinen Tool-Use-Block: {data!r}")

    async def ask_plain(self, system_prompt: str, user_prompt: str) -> str:
        session = async_get_clientsession(self.hass)
        payload = {
            "model": self.model,
            "max_tokens": 2048,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
            "temperature": 0.2,
        }
        async with session.post(
            f"{self.url}/messages",
            json=payload,
            headers=self._headers(),
            timeout=_REQUEST_TIMEOUT,
        ) as resp:
            await raise_for_status_with_body(resp, f"Anthropic ({self.model})")
            data = await resp.json()
        parts = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
        if not parts:
            raise ValueError(f"Anthropic-Antwort enthielt keinen Text-Block: {data!r}")
        return "".join(parts).strip()


class GeminiClient:
    """Google Generative Language API (Gemini).

    Strukturiertes Format ueber ``generationConfig.responseSchema``. Gemini nutzt einen
    eingeschraenkten OpenAPI-3.0-aehnlichen Schema-Dialekt statt vollem JSON-Schema, das
    Pydantic-Schema muss dafuer erst uebersetzt werden (siehe :func:`_gemini_schema`) -
    ungefiltert weitergereicht lehnt Gemini es mit HTTP 400 ab. Was sich auch uebersetzt
    nicht abbilden laesst, faengt die Rueckfallebene in :meth:`ask` ab. Weil damit nicht
    garantiert ist, dass Gemini jedes Detail so durchsetzt wie Ollama/OpenAI/Anthropic,
    validiert :meth:`ask` die Antwort in jedem Fall nochmal gegen :class:`LLMOutput`.
    """

    DEFAULT_URL = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(self, hass: HomeAssistant, url: str, api_key: str, model: str) -> None:
        self.hass = hass
        self.url = (url or self.DEFAULT_URL).rstrip("/")
        self.api_key = clean_api_key(api_key)
        self.model = model
        guard_api_key_transport(self.url, self.api_key)

    @staticmethod
    def _to_gemini_contents(messages: list[dict]) -> tuple[str, list[dict]]:
        """Gemini kennt keine 'system'-Rolle in 'contents' - getrennt uebergeben, und
        'assistant' heisst bei Gemini 'model'."""

        system_parts = []
        contents = []
        for message in messages:
            if message.get("role") == "system":
                system_parts.append(message["content"])
                continue
            role = "model" if message.get("role") == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": message["content"]}]})
        return "\n\n".join(system_parts), contents

    def _endpoint(self) -> str:
        return f"{self.url}/models/{self.model}:generateContent"

    def _headers(self) -> dict:
        """Der API-Key gehoert in den Header, NICHT als "?key=..." in die URL.

        Gemini akzeptiert beides, aber bei einem HTTP-Fehler nimmt aiohttp die komplette
        Anfrage-URL in die Exception-Meldung auf - der Key stand damit im Klartext im
        Traceback, den ``conversation._handle_new_message`` nach home-assistant.log
        schreibt (und den Nutzer routinemaessig in Foren und Issues posten).
        """

        return {"Content-Type": "application/json", "x-goog-api-key": self.api_key}

    # Wie bei OpenAICompatibleClient: Modelle merken, die "responseSchema" nicht
    # annehmen, damit nicht jede Anfrage erneut in denselben 400er laeuft.
    _unsupported_response_schema: set[tuple[str, str]] = set()

    async def _post(self, payload: dict) -> dict:
        """Ein Aufruf von ``:generateContent`` samt aussagekraeftiger Fehlermeldung."""

        session = async_get_clientsession(self.hass)
        async with session.post(
            self._endpoint(), json=payload, headers=self._headers(), timeout=_REQUEST_TIMEOUT
        ) as resp:
            await raise_for_status_with_body(resp, f"Gemini ({self.model})")
            return await resp.json()

    @staticmethod
    def _text_of(data: dict) -> str:
        """Der Antworttext aus einer generateContent-Antwort."""

        try:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError(f"Unerwartetes Gemini-Antwortformat: {data!r}") from exc

    async def ask(self, messages: list[dict]) -> LLMOutput:
        """Fragt Gemini mit uebersetztem Schema, notfalls nur im JSON-Modus.

        Zwei Stufen: uebersetztes ``responseSchema`` (siehe :func:`_gemini_schema`) und,
        falls Gemini das ablehnt, nur ``responseMimeType: application/json``. Die zweite
        Stufe wird gebraucht, weil Geminis Schema-Dialekt frei geformte Objekte ohne
        feste Properties nicht kennt - ``PlanStep.service_data`` ist genau so eines.
        Die Antwort wird in beiden Faellen gegen :class:`LLMOutput` validiert.
        """

        system_text, contents = self._to_gemini_contents(messages)
        cache_key = (self.url, self.model)
        attempts: list[dict | None] = [_gemini_schema(LLMOutput.model_json_schema()), None]
        if cache_key in self._unsupported_response_schema:
            attempts = attempts[1:]

        for position, response_schema in enumerate(attempts):
            generation_config: dict = {
                "temperature": 0.2,
                "responseMimeType": "application/json",
            }
            if response_schema is not None:
                generation_config["responseSchema"] = response_schema
            payload: dict = {"contents": contents, "generationConfig": generation_config}
            if system_text:
                payload["systemInstruction"] = {"parts": [{"text": system_text}]}

            try:
                data = await self._post(payload)
            except LLMHTTPError as exc:
                is_last = position == len(attempts) - 1
                if is_last or exc.status not in _FORMAT_FALLBACK_STATUSES:
                    raise
                self._unsupported_response_schema.add(cache_key)
                _LOGGER.warning(
                    "Gemini-Modell %s hat das uebersetzte responseSchema abgelehnt (%s) - "
                    "naechster Versuch nur mit responseMimeType",
                    self.model,
                    exc,
                )
                continue
            return LLMOutput.model_validate(extract_json(self._text_of(data)))

        # Unerreichbar: die Schleife kehrt entweder zurueck oder wirft im letzten Durchlauf.
        raise ValueError(f"Keine Antwort von Modell {self.model} erhalten.")

    async def ask_plain(self, system_prompt: str, user_prompt: str) -> str:
        data = await self._post(
            {
                "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
                "systemInstruction": {"parts": [{"text": system_prompt}]},
                "generationConfig": {"temperature": 0.2},
            }
        )
        return self._text_of(data).strip()


def create_client(hass: HomeAssistant, provider: ModelProvider) -> LLMClient:
    """Baut den zum Provider-Typ passenden Client. Einzige Stelle, die Typ auf Klasse
    abbildet - neue Provider-Typen kommen nur hier und in ``providers.PROVIDER_TYPES`` dazu.
    """

    if provider.type == "ollama":
        return OllamaClient(hass, provider.url or DEFAULT_OLLAMA_URL, provider.model)
    if provider.type == "openai":
        return OpenAICompatibleClient(hass, provider.url, provider.api_key, provider.model)
    if provider.type == "anthropic":
        return AnthropicClient(hass, provider.url, provider.api_key, provider.model)
    if provider.type == "gemini":
        return GeminiClient(hass, provider.url, provider.api_key, provider.model)
    raise ValueError(f"Unbekannter Provider-Typ: {provider.type}")
