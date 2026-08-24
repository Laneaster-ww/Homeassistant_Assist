(function () {
  const ELEMENT_NAME = "smart-homeassistant-floating-window";
  const STORAGE_KEY = "smart_homeassistant_floating_window";
  const DEFAULT_AGENT_ID = "conversation.smart_homeassistant";
  const SERVICE_DOMAIN = "smart_homeassistant";

  const PROVIDER_TYPES = [
    { value: "ollama", label: "Ollama", needsKey: false, urlHint: "z.B. http://homeassistant.local:11434" },
    { value: "openai", label: "OpenAI-kompatibel", needsKey: true, urlHint: "leer = offizieller OpenAI-Endpoint" },
    { value: "anthropic", label: "Anthropic (Claude)", needsKey: true, urlHint: "leer = offizieller Anthropic-Endpoint" },
    { value: "gemini", label: "Google Gemini", needsKey: true, urlHint: "leer = offizieller Gemini-Endpoint" },
  ];

  function providerTypeLabel(type) {
    return PROVIDER_TYPES.find((t) => t.value === type)?.label || type;
  }

  if (customElements.get(ELEMENT_NAME)) {
    return;
  }

  class SmartHomeassistantFloatingWindow extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({ mode: "open" });
      this._hass = null;
      // Die Conversation-ID wird bewusst SOFORT selbst erzeugt (statt erst beim ersten
      // "conversation/process"-Aufruf vom Server zu bekommen): so kann ein Modell schon
      // VOR der ersten Nachricht ausgewaehlt werden (siehe _selectProvider), während
      // conversation.py weiterhin auch selbst erzeugte IDs akzeptiert.
      this._conversationId = this._newConversationId();
      this._listening = false;
      this._recognition = null;
      this._input = "";
      this._error = "";
      // Sperrt Senden/Mikrofon, solange eine Anfrage laeuft - eine lokale Modellantwort
      // kann eine Minute und mehr dauern, in der sonst beliebig viele parallele
      // Anfragen abgeschickt werden koennten.
      this._busy = false;
      this._messages = [
        {
          role: "assistant",
          text: "Bereit. Was soll ich für dich tun?",
        },
      ];
      this._providers = [];
      this._providersLoaded = false;
      this._selectedProviderId = null;
      this._modelPanelOpen = false;
      this._addFormOpen = false;
      this._addForm = { name: "", type: "ollama", model: "", url: "", apiKey: "" };
      this._addFormError = "";
      this._addFormBusy = false;
      // null = Formular legt einen neuen Provider an; sonst die ID des Providers, der
      // gerade bearbeitet wird. Dasselbe Formular bedient beide Faelle.
      this._editingProviderId = null;
      this._state = this._loadState();
      this._selectedProviderId = this._state.providerId || null;
    }

    set hass(hass) {
      // Bewusst OHNE _render(): Home Assistant tauscht das hass-Objekt bei jeder
      // Zustandsaenderung im ganzen Haus aus (also permanent). Da im Chat-Fenster nichts
      // von Entity-Zustaenden abhaengt, wurde hier bisher nur unnoetig der komplette
      // Shadow DOM neu aufgebaut - mitten im Tippen, wodurch Fokus und Cursorposition
      // im Eingabefeld verloren gingen. Gerendert wird jetzt nur noch, wenn sich die
      // Anzeige tatsächlich ändert (Nachricht, Fehler, Modell-Panel).
      this._hass = hass;
    }

    get hass() {
      return this._hass;
    }

    connectedCallback() {
      this._render();
    }

    disconnectedCallback() {
      this._stopListening();
    }

    _newConversationId() {
      if (window.crypto?.randomUUID) {
        return window.crypto.randomUUID();
      }
      // Fallback für sehr alte Browser ohne crypto.randomUUID.
      return `sha-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    }

    _loadState() {
      try {
        return {
          open: false,
          ...JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}"),
        };
      } catch (_err) {
        return { open: false };
      }
    }

    _saveState() {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(this._state));
    }

    _setOpen(open) {
      this._state.open = open;
      this._saveState();
      this._render();
    }

    _setInput(value) {
      this._input = value;
      const input = this.shadowRoot.getElementById("sha-input");
      if (input && input.value !== value) {
        input.value = value;
      }
    }

    _currentLanguage() {
      // "Standard ist die in Home Assistant eingestellte Sprache": hass.language ist die
      // Sprache des aktuell angemeldeten Nutzers (faellt intern bereits auf die
      // Systemsprache zurück, falls der Nutzer keine eigene gesetzt hat). Ohne hass als
      // letzten Fallback nichts mitschicken - der Server faellt dann selbst auf die
      // Systemsprache zurück (siehe conversation.py, async_process).
      return this._hass?.language || this._hass?.config?.language || undefined;
    }

    async _sendText(text) {
      const cleanText = String(text || "").trim();
      if (!cleanText || !this._hass || this._busy) {
        return;
      }

      this._busy = true;
      this._messages.push({ role: "user", text: cleanText });
      // Position des Platzhalters merken, statt beim Eintreffen der Antwort einfach
      // "die letzte Nachricht" zu ersetzen: sonst ueberschreibt die zuerst eintreffende
      // Antwort den Platzhalter der zuletzt gestellten Frage und die Antworten stehen
      // vertauscht im Verlauf.
      const pendingIndex =
        this._messages.push({ role: "assistant", text: "Verarbeite Anfrage ..." }) - 1;
      this._input = "";
      this._error = "";
      this._render();

      try {
        const result = await this._hass.callWS({
          type: "conversation/process",
          text: cleanText,
          agent_id: DEFAULT_AGENT_ID,
          conversation_id: this._conversationId,
          language: this._currentLanguage(),
        });

        this._conversationId = result.conversation_id || this._conversationId;
        this._messages[pendingIndex] = {
          role: "assistant",
          text:
            result?.response?.speech?.plain?.speech ||
            result?.response?.speech?.plain?.extra_data?.speech ||
            "Keine Textantwort erhalten.",
        };
      } catch (error) {
        this._messages.splice(pendingIndex, 1);
        this._error = error?.message || String(error);
      } finally {
        this._busy = false;
      }

      this._render();
      this._scrollToEnd();
    }

    _clearConversation() {
      this._conversationId = this._newConversationId();
      this._input = "";
      this._error = "";
      this._messages = [
        {
          role: "assistant",
          text: "Bereit. Was soll ich für dich tun?",
        },
      ];
      // Die Modellwahl bleibt bewusst erhalten (localStorage-Zustand) - "Verlauf leeren"
      // soll nur das Gespräch zuruecksetzen, nicht das zuletzt gewaehlte Modell.
      if (this._selectedProviderId) {
        this._setConversationModel(this._selectedProviderId);
      }
      this._render();
    }

    // --- Modell wechseln --------------------------------------------------------

    async _callService(service, serviceData, wantsResponse) {
      if (!this._hass) {
        return null;
      }
      const result = await this._hass.callWS({
        type: "call_service",
        domain: SERVICE_DOMAIN,
        service,
        service_data: serviceData || {},
        return_response: Boolean(wantsResponse),
      });
      return wantsResponse ? result?.response : null;
    }

    async _loadProviders() {
      try {
        const response = await this._callService("list_model_providers", {}, true);
        this._providers = response?.providers || [];
        this._providersLoaded = true;
        // Ohne eigene Auswahl (frischer Besuch) zeigt sich der Server-Standard.
        if (!this._selectedProviderId) {
          const defaultProvider = this._providers.find((p) => p.is_default);
          if (defaultProvider) {
            this._selectedProviderId = defaultProvider.id;
          }
        }
      } catch (error) {
        this._error = `Modelle konnten nicht geladen werden: ${error?.message || error}`;
      }
      this._render();
    }

    async _toggleModelPanel() {
      this._modelPanelOpen = !this._modelPanelOpen;
      this._addFormOpen = false;
      this._addFormError = "";
      if (this._modelPanelOpen && !this._providersLoaded) {
        this._render();
        await this._loadProviders();
        return;
      }
      this._render();
    }

    async _setConversationModel(providerId) {
      try {
        await this._callService("set_conversation_model", {
          conversation_id: this._conversationId,
          provider_id: providerId,
        });
      } catch (error) {
        this._error = `Modell konnte nicht gesetzt werden: ${error?.message || error}`;
      }
    }

    async _selectProvider(providerId) {
      this._selectedProviderId = providerId;
      this._state.providerId = providerId;
      this._saveState();
      this._modelPanelOpen = false;
      this._render();
      await this._setConversationModel(providerId);
    }

    async _removeProvider(providerId, event) {
      event.stopPropagation();
      try {
        await this._callService("remove_model_provider", { id: providerId });
        if (this._selectedProviderId === providerId) {
          this._selectedProviderId = null;
          this._state.providerId = null;
          this._saveState();
        }
        await this._loadProviders();
      } catch (error) {
        this._error = `Modell konnte nicht entfernt werden: ${error?.message || error}`;
        this._render();
      }
    }

    _openAddForm() {
      this._addFormOpen = true;
      this._editingProviderId = null;
      this._addFormError = "";
      this._addForm = { name: "", type: "ollama", model: "", url: "", apiKey: "" };
      this._render();
    }

    _openEditForm(providerId, event) {
      event?.stopPropagation();
      const provider = this._providers.find((p) => p.id === providerId);
      if (!provider) {
        return;
      }
      this._addFormOpen = true;
      this._editingProviderId = providerId;
      this._addFormError = "";
      // Der API-Key startet leer: das Backend liefert ihn nie aus (to_safe_dict), und
      // ein leer gelassenes Feld laesst den gespeicherten Key unverändert.
      this._addForm = {
        name: provider.name || "",
        type: provider.type || "ollama",
        model: provider.model || "",
        url: provider.url || "",
        apiKey: "",
      };
      this._render();
    }

    async _submitAddForm() {
      const form = this._addForm;
      const editingId = this._editingProviderId;
      const editing = Boolean(editingId);
      if (!form.name.trim() || !form.model.trim()) {
        this._addFormError = "Name und Modell sind Pflichtfelder.";
        this._render();
        return;
      }
      const typeInfo = PROVIDER_TYPES.find((t) => t.value === form.type);
      // Beim Bearbeiten darf das Key-Feld leer bleiben, sofern schon einer gespeichert
      // ist - verlangt wird er nur, wenn wirklich noch keiner hinterlegt wurde (z.B.
      // nach einem Wechsel von "ollama" auf einen Cloud-Typ).
      const storedKey = editing
        ? Boolean(this._providers.find((p) => p.id === editingId)?.has_api_key)
        : false;
      if (typeInfo?.needsKey && !form.apiKey.trim() && !storedKey) {
        this._addFormError = `Für ${typeInfo.label} wird ein API-Key benötigt.`;
        this._render();
        return;
      }

      this._addFormBusy = true;
      this._addFormError = "";
      this._render();
      try {
        const payload = {
          name: form.name.trim(),
          type: form.type,
          model: form.model.trim(),
          url: form.url.trim(),
          api_key: form.apiKey.trim(),
        };
        let response;
        if (editing) {
          response = await this._callService(
            "update_model_provider",
            { id: editingId, ...payload },
            true
          );
        } else {
          response = await this._callService(
            "add_model_provider",
            { ...payload, make_default: this._providers.length === 0 },
            true
          );
        }
        this._addFormOpen = false;
        this._editingProviderId = null;
        await this._loadProviders();
        const provider = response?.provider;
        // Nur beim Anlegen automatisch umschalten - beim Bearbeiten soll eine Korrektur
        // an einem gerade nicht genutzten Modell die laufende Modellwahl nicht kapern.
        if (provider && !editing) {
          await this._selectProvider(provider.id);
        }
      } catch (error) {
        this._addFormError = error?.message || String(error);
      } finally {
        this._addFormBusy = false;
        this._render();
      }
    }

    _createRecognition() {
      const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
      if (!SpeechRecognition) {
        this._error = "Spracheingabe wird von diesem Browser nicht unterstützt.";
        this._render();
        return null;
      }

      const recognition = new SpeechRecognition();
      recognition.lang = "de-DE";
      recognition.continuous = false;
      recognition.interimResults = true;

      recognition.onstart = () => {
        this._listening = true;
        this._error = "";
        this._render();
      };

      recognition.onresult = (event) => {
        let interim = "";
        let finalText = "";
        for (let index = event.resultIndex; index < event.results.length; index += 1) {
          const transcript = event.results[index][0].transcript.trim();
          if (event.results[index].isFinal) {
            finalText += `${transcript} `;
          } else {
            interim += `${transcript} `;
          }
        }

        this._setInput((finalText || interim).trim());
        if (finalText.trim()) {
          this._sendText(finalText.trim());
        }
      };

      recognition.onerror = (event) => {
        this._listening = false;
        this._error = event.error ? `Mikrofonfehler: ${event.error}` : "Mikrofonfehler.";
        this._render();
      };

      recognition.onend = () => {
        this._listening = false;
        this._render();
      };

      return recognition;
    }

    _startListening() {
      if (this._listening) {
        return;
      }
      this._recognition = this._createRecognition();
      this._recognition?.start();
    }

    _stopListening() {
      if (this._recognition && this._listening) {
        this._recognition.stop();
      }
      this._listening = false;
    }

    _scrollToEnd() {
      requestAnimationFrame(() => {
        const log = this.shadowRoot.getElementById("sha-log");
        if (log) {
          log.scrollTop = log.scrollHeight;
        }
      });
    }

    _currentModelLabel() {
      const selected = this._providers.find((p) => p.id === this._selectedProviderId);
      if (selected) {
        return selected.name;
      }
      return this._providersLoaded ? "Kein Modell" : "Modell";
    }

    _render() {
      const supportsSpeech = Boolean(window.SpeechRecognition || window.webkitSpeechRecognition);
      const open = Boolean(this._state.open);

      this.shadowRoot.innerHTML = `
        <style>
          :host {
            bottom: calc(18px + env(safe-area-inset-bottom));
            box-sizing: border-box;
            display: block;
            font-family: var(--paper-font-body1_-_font-family, Roboto, Arial, sans-serif);
            pointer-events: none;
            position: fixed;
            right: calc(18px + env(safe-area-inset-right));
            z-index: 2147483640;
          }
          * {
            box-sizing: border-box;
          }
          .launcher,
          .window {
            pointer-events: auto;
          }
          .launcher {
            align-items: center;
            background: var(--primary-color);
            border: 0;
            border-radius: 999px;
            box-shadow: 0 8px 28px rgba(0, 0, 0, 0.28);
            color: var(--text-primary-color, #fff);
            cursor: pointer;
            display: ${open ? "none" : "inline-flex"};
            height: 56px;
            justify-content: center;
            width: 56px;
          }
          .window {
            background: var(--card-background-color, #fff);
            border: 1px solid var(--divider-color, rgba(0, 0, 0, 0.12));
            border-radius: 8px;
            box-shadow: 0 14px 42px rgba(0, 0, 0, 0.32);
            color: var(--primary-text-color, #212121);
            display: ${open ? "grid" : "none"};
            grid-template-rows: auto minmax(0, 1fr) auto;
            height: min(620px, calc(100vh - 36px - env(safe-area-inset-bottom)));
            overflow: hidden;
            position: relative;
            width: min(420px, calc(100vw - 36px));
          }
          .header {
            align-items: center;
            border-bottom: 1px solid var(--divider-color, rgba(0, 0, 0, 0.12));
            display: flex;
            gap: 8px;
            min-width: 0;
            padding: 10px 10px 10px 14px;
          }
          .title {
            font-size: 16px;
            font-weight: 600;
            line-height: 22px;
            min-width: 0;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
          }
          .spacer {
            flex: 1;
          }
          .icon-button {
            align-items: center;
            background: transparent;
            border: 0;
            border-radius: 50%;
            color: var(--primary-text-color, #212121);
            cursor: pointer;
            display: inline-flex;
            height: 40px;
            justify-content: center;
            padding: 0;
            width: 40px;
          }
          .icon-button:hover {
            background: var(--secondary-background-color, rgba(0, 0, 0, 0.06));
          }
          .model-switch {
            align-items: center;
            background: var(--secondary-background-color, rgba(0, 0, 0, 0.06));
            border: 0;
            border-radius: 999px;
            color: var(--primary-text-color, #212121);
            cursor: pointer;
            display: inline-flex;
            flex-shrink: 1;
            font: inherit;
            font-size: 12px;
            gap: 2px;
            max-width: 140px;
            overflow: hidden;
            padding: 6px 8px 6px 10px;
          }
          .model-switch span {
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
          }
          .model-switch ha-icon {
            --mdc-icon-size: 18px;
            flex-shrink: 0;
          }
          .model-panel {
            background: var(--card-background-color, #fff);
            border: 1px solid var(--divider-color, rgba(0, 0, 0, 0.12));
            border-radius: 8px;
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.28);
            max-height: min(360px, 70vh);
            overflow-y: auto;
            position: absolute;
            right: 10px;
            top: 58px;
            width: min(280px, calc(100% - 20px));
            z-index: 5;
          }
          .model-row {
            align-items: center;
            border-bottom: 1px solid var(--divider-color, rgba(0, 0, 0, 0.08));
            cursor: pointer;
            display: flex;
            gap: 8px;
            padding: 10px 12px;
          }
          .model-row:hover {
            background: var(--secondary-background-color, rgba(0, 0, 0, 0.06));
          }
          .model-row.selected {
            background: var(--secondary-background-color, rgba(0, 0, 0, 0.06));
            font-weight: 600;
          }
          .model-row-info {
            flex: 1;
            min-width: 0;
          }
          .model-row-name {
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
          }
          .model-row-type {
            color: var(--secondary-text-color, #727272);
            font-size: 12px;
          }
          .model-row-remove,
          .model-row-edit {
            --mdc-icon-size: 18px;
            background: transparent;
            border: 0;
            border-radius: 50%;
            color: var(--secondary-text-color, #727272);
            cursor: pointer;
            flex-shrink: 0;
            height: 28px;
            padding: 0;
            width: 28px;
          }
          .model-row-edit:hover {
            background: var(--secondary-background-color, rgba(0, 0, 0, 0.1));
            color: var(--primary-color);
          }
          .model-row-remove:hover {
            background: var(--secondary-background-color, rgba(0, 0, 0, 0.1));
            color: var(--error-color, #db4437);
          }
          .model-form-title {
            font-size: 13px;
            font-weight: 600;
            padding-bottom: 2px;
          }
          .model-empty {
            color: var(--secondary-text-color, #727272);
            font-size: 13px;
            padding: 14px 12px;
          }
          .model-add-toggle {
            align-items: center;
            background: transparent;
            border: 0;
            color: var(--primary-color);
            cursor: pointer;
            display: flex;
            font: inherit;
            font-weight: 600;
            gap: 6px;
            padding: 10px 12px;
            width: 100%;
          }
          .model-add-toggle:hover {
            background: var(--secondary-background-color, rgba(0, 0, 0, 0.06));
          }
          .model-add-form {
            display: grid;
            gap: 8px;
            padding: 12px;
          }
          .model-add-form label {
            display: grid;
            font-size: 12px;
            gap: 4px;
          }
          .model-add-form input,
          .model-add-form select {
            background: var(--secondary-background-color, rgba(0, 0, 0, 0.06));
            border: 1px solid var(--divider-color, rgba(0, 0, 0, 0.12));
            border-radius: 6px;
            color: var(--primary-text-color, #212121);
            font: inherit;
            padding: 8px;
          }
          .model-add-form .hint {
            color: var(--secondary-text-color, #727272);
            font-size: 11px;
          }
          .model-add-error {
            color: var(--error-color, #db4437);
            font-size: 12px;
          }
          .model-add-actions {
            display: flex;
            gap: 8px;
            justify-content: flex-end;
          }
          .model-add-actions button {
            border: 0;
            border-radius: 6px;
            cursor: pointer;
            font: inherit;
            padding: 8px 12px;
          }
          .model-add-cancel {
            background: transparent;
            color: var(--primary-text-color, #212121);
          }
          .model-add-save {
            background: var(--primary-color);
            color: var(--text-primary-color, #fff);
          }
          .model-add-save:disabled {
            cursor: not-allowed;
            opacity: 0.6;
          }
          .log {
            display: grid;
            gap: 10px;
            overflow-y: auto;
            padding: 14px;
          }
          .message {
            border-radius: 8px;
            line-height: 1.45;
            max-width: 92%;
            overflow-wrap: anywhere;
            padding: 10px 12px;
            white-space: pre-wrap;
          }
          .message.assistant {
            align-self: start;
            background: var(--secondary-background-color, rgba(0, 0, 0, 0.06));
          }
          .message.user {
            align-self: end;
            background: var(--primary-color);
            color: var(--text-primary-color, #fff);
          }
          .error {
            color: var(--error-color, #db4437);
            font-size: 13px;
            padding: 0 14px 8px;
          }
          .composer {
            border-top: 1px solid var(--divider-color, rgba(0, 0, 0, 0.12));
            display: grid;
            gap: 8px;
            grid-template-columns: 1fr auto auto;
            padding: 10px;
          }
          textarea {
            background: var(--secondary-background-color, rgba(0, 0, 0, 0.06));
            border: 1px solid var(--divider-color, rgba(0, 0, 0, 0.12));
            border-radius: 8px;
            color: var(--primary-text-color, #212121);
            font: inherit;
            line-height: 20px;
            max-height: 120px;
            min-height: 42px;
            outline: none;
            padding: 10px 12px;
            resize: vertical;
            width: 100%;
          }
          .send {
            background: var(--primary-color);
            color: var(--text-primary-color, #fff);
          }
          button:disabled {
            cursor: not-allowed;
            opacity: 0.48;
          }
          @media (max-width: 520px) {
            :host {
              bottom: 0;
              left: 0;
              right: 0;
            }
            .launcher {
              bottom: calc(16px + env(safe-area-inset-bottom));
              position: fixed;
              right: calc(16px + env(safe-area-inset-right));
            }
            .window {
              border-bottom-left-radius: 0;
              border-bottom-right-radius: 0;
              height: min(74vh, 680px);
              width: 100vw;
            }
          }
        </style>

        <button class="launcher" type="button" title="Smart Homeassistant öffnen" aria-label="Smart Homeassistant öffnen">
          <ha-icon icon="mdi:robot"></ha-icon>
        </button>

        <section class="window" aria-label="Smart Homeassistant">
          <div class="header">
            <ha-icon icon="mdi:robot"></ha-icon>
            <div class="title">Smart Homeassistant</div>
            <div class="spacer"></div>
            <button class="model-switch" id="sha-model-toggle" type="button" title="Modell wechseln" aria-label="Modell wechseln">
              <span>${this._escapeHtml(this._currentModelLabel())}</span>
              <ha-icon icon="mdi:chevron-down"></ha-icon>
            </button>
            <button class="icon-button" id="sha-clear" type="button" title="Verlauf leeren" aria-label="Verlauf leeren">
              <ha-icon icon="mdi:delete-outline"></ha-icon>
            </button>
            <button class="icon-button" id="sha-close" type="button" title="Minimieren" aria-label="Minimieren">
              <ha-icon icon="mdi:chevron-down"></ha-icon>
            </button>
          </div>
          ${this._modelPanelOpen ? this._renderModelPanel() : ""}
          <div class="log" id="sha-log">
            ${this._messages.map((message) => this._renderMessage(message)).join("")}
          </div>
          ${this._error ? `<div class="error">${this._escapeHtml(this._error)}</div>` : ""}
          <form class="composer" id="sha-form">
            <textarea id="sha-input" rows="1" placeholder="Nachricht eingeben" ${this._busy ? "disabled" : ""}>${this._escapeHtml(this._input)}</textarea>
            <button class="icon-button" id="sha-mic" type="button" title="Sprechen" aria-label="Sprechen" ${supportsSpeech && !this._busy ? "" : "disabled"}>
              <ha-icon icon="${this._listening ? "mdi:stop" : "mdi:microphone"}"></ha-icon>
            </button>
            <button class="icon-button send" type="submit" title="Senden" aria-label="Senden" ${this._busy ? "disabled" : ""}>
              <ha-icon icon="${this._busy ? "mdi:dots-horizontal" : "mdi:send"}"></ha-icon>
            </button>
          </form>
        </section>
      `;

      this.shadowRoot.querySelector(".launcher")?.addEventListener("click", () => {
        this._setOpen(true);
        requestAnimationFrame(() => this.shadowRoot.getElementById("sha-input")?.focus());
      });
      this.shadowRoot.getElementById("sha-close")?.addEventListener("click", () => {
        this._setOpen(false);
      });
      this.shadowRoot.getElementById("sha-clear")?.addEventListener("click", () => {
        this._clearConversation();
      });
      this.shadowRoot.getElementById("sha-mic")?.addEventListener("click", () => {
        if (this._listening) {
          this._stopListening();
          this._render();
        } else {
          this._startListening();
        }
      });
      this.shadowRoot.getElementById("sha-input")?.addEventListener("input", (event) => {
        this._input = event.target.value;
      });
      this.shadowRoot.getElementById("sha-input")?.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && !event.shiftKey) {
          event.preventDefault();
          this._sendText(event.target.value);
        }
      });
      this.shadowRoot.getElementById("sha-form")?.addEventListener("submit", (event) => {
        event.preventDefault();
        this._sendText(this.shadowRoot.getElementById("sha-input")?.value || "");
      });
      this.shadowRoot.getElementById("sha-model-toggle")?.addEventListener("click", () => {
        this._toggleModelPanel();
      });
      this._wireModelPanelEvents();

      this._scrollToEnd();
    }

    _renderModelPanel() {
      if (this._addFormOpen) {
        return `<div class="model-panel">${this._renderAddForm()}</div>`;
      }

      const rows = this._providers
        .map((provider) => {
          const selected = provider.id === this._selectedProviderId;
          return `
            <div class="model-row${selected ? " selected" : ""}" data-provider-id="${this._escapeHtml(provider.id)}">
              <div class="model-row-info">
                <div class="model-row-name">${this._escapeHtml(provider.name)}</div>
                <div class="model-row-type">${this._escapeHtml(providerTypeLabel(provider.type))} · ${this._escapeHtml(provider.model)}</div>
              </div>
              <button class="model-row-edit" type="button" data-edit-id="${this._escapeHtml(provider.id)}" title="Bearbeiten" aria-label="Bearbeiten">
                <ha-icon icon="mdi:pencil"></ha-icon>
              </button>
              <button class="model-row-remove" type="button" data-remove-id="${this._escapeHtml(provider.id)}" title="Entfernen" aria-label="Entfernen">
                <ha-icon icon="mdi:close"></ha-icon>
              </button>
            </div>
          `;
        })
        .join("");

      return `
        <div class="model-panel">
          ${rows || '<div class="model-empty">Noch kein Modell konfiguriert.</div>'}
          <button class="model-add-toggle" id="sha-model-add-toggle" type="button">
            <ha-icon icon="mdi:plus"></ha-icon>
            <span>Modell hinzufügen</span>
          </button>
        </div>
      `;
    }

    _renderAddForm() {
      const form = this._addForm;
      const editing = Boolean(this._editingProviderId);
      const storedKey =
        editing &&
        Boolean(this._providers.find((p) => p.id === this._editingProviderId)?.has_api_key);
      const typeInfo = PROVIDER_TYPES.find((t) => t.value === form.type) || PROVIDER_TYPES[0];
      return `
        <div class="model-add-form">
          <div class="model-form-title">${editing ? "Modell bearbeiten" : "Modell hinzufügen"}</div>
          <label>
            Name
            <input type="text" id="sha-add-name" value="${this._escapeHtml(form.name)}" placeholder="z.B. Lokales Ollama" />
          </label>
          <label>
            Typ
            <select id="sha-add-type">
              ${PROVIDER_TYPES.map(
                (t) =>
                  `<option value="${t.value}" ${t.value === form.type ? "selected" : ""}>${this._escapeHtml(t.label)}</option>`
              ).join("")}
            </select>
          </label>
          <label>
            Modell
            <input type="text" id="sha-add-model" value="${this._escapeHtml(form.model)}" placeholder="z.B. llama3.1:8b" />
          </label>
          <label>
            URL (optional)
            <input type="text" id="sha-add-url" value="${this._escapeHtml(form.url)}" placeholder="${this._escapeHtml(typeInfo.urlHint)}" />
          </label>
          ${
            typeInfo.needsKey
              ? `<label>
                  API-Key
                  <input type="password" id="sha-add-key" value="${this._escapeHtml(form.apiKey)}" placeholder="${storedKey ? "leer lassen = unverändert" : "API-Key"}" />
                </label>`
              : ""
          }
          ${this._addFormError ? `<div class="model-add-error">${this._escapeHtml(this._addFormError)}</div>` : ""}
          <div class="model-add-actions">
            <button class="model-add-cancel" type="button" id="sha-add-cancel">Abbrechen</button>
            <button class="model-add-save" type="button" id="sha-add-save" ${this._addFormBusy ? "disabled" : ""}>
              ${this._addFormBusy ? "Speichert ..." : "Speichern"}
            </button>
          </div>
        </div>
      `;
    }

    _wireModelPanelEvents() {
      if (!this._modelPanelOpen) {
        return;
      }

      if (this._addFormOpen) {
        const nameInput = this.shadowRoot.getElementById("sha-add-name");
        nameInput?.addEventListener("input", (e) => {
          this._addForm.name = e.target.value;
        });
        this.shadowRoot.getElementById("sha-add-type")?.addEventListener("change", (e) => {
          this._addForm.type = e.target.value;
          this._render();
        });
        this.shadowRoot.getElementById("sha-add-model")?.addEventListener("input", (e) => {
          this._addForm.model = e.target.value;
        });
        this.shadowRoot.getElementById("sha-add-url")?.addEventListener("input", (e) => {
          this._addForm.url = e.target.value;
        });
        this.shadowRoot.getElementById("sha-add-key")?.addEventListener("input", (e) => {
          this._addForm.apiKey = e.target.value;
        });
        this.shadowRoot.getElementById("sha-add-cancel")?.addEventListener("click", () => {
          this._addFormOpen = false;
          this._editingProviderId = null;
          this._addFormError = "";
          this._render();
        });
        this.shadowRoot.getElementById("sha-add-save")?.addEventListener("click", () => {
          this._submitAddForm();
        });
        nameInput?.focus();
        return;
      }

      this.shadowRoot.getElementById("sha-model-add-toggle")?.addEventListener("click", () => {
        this._openAddForm();
      });
      this.shadowRoot.querySelectorAll(".model-row").forEach((row) => {
        row.addEventListener("click", () => {
          this._selectProvider(row.dataset.providerId);
        });
      });
      this.shadowRoot.querySelectorAll(".model-row-edit").forEach((button) => {
        button.addEventListener("click", (event) => {
          this._openEditForm(button.dataset.editId, event);
        });
      });
      this.shadowRoot.querySelectorAll(".model-row-remove").forEach((button) => {
        button.addEventListener("click", (event) => {
          this._removeProvider(button.dataset.removeId, event);
        });
      });
    }

    _renderMessage(message) {
      return `<div class="message ${message.role}">${this._escapeHtml(message.text)}</div>`;
    }

    _escapeHtml(value) {
      return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
    }
  }

  customElements.define(ELEMENT_NAME, SmartHomeassistantFloatingWindow);

  // Das Wurzelelement des HA-Frontends traegt "hass" direkt. Die Referenz wird gecacht,
  // weil die fruehere Loesung (rekursive Suche durch den kompletten DOM inklusive aller
  // Shadow Roots) bei JEDER DOM-Änderung im Frontend erneut lief - und das HA-Frontend
  // ändert seinen DOM praktisch ununterbrochen.
  let hassProvider = null;

  function findHassProvider() {
    if (hassProvider?.isConnected && hassProvider.hass) {
      return hassProvider;
    }
    const root = document.querySelector("home-assistant");
    hassProvider = root?.hass ? root : null;
    return hassProvider;
  }

  function ensureOverlay() {
    let overlay = document.querySelector(ELEMENT_NAME);
    if (!overlay) {
      overlay = document.createElement(ELEMENT_NAME);
      document.body.appendChild(overlay);
    }
    const hass = findHassProvider()?.hass;
    if (hass && overlay.hass !== hass) {
      overlay.hass = hass;
    }
  }

  ensureOverlay();
  // Ein Intervall genuegt: das Overlay haengt an document.body und muss nur wieder
  // eingehaengt werden, falls das Frontend es entfernt. Der fruehere MutationObserver
  // über den gesamten Dokumentbaum hat dafür tausende Male pro Sekunde gefeuert.
  window.setInterval(ensureOverlay, 1000);
})();
