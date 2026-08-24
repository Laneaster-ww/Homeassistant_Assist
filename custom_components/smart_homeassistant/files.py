"""Blockierende Text-Datei-Helfer, gemeinsam genutzt von automations.py und dashboard.py.

Beide brauchen dieselben zwei Operationen fuer ihre Rollback-Absicherung (Original
lesen, im Fehlerfall zurueckschreiben). Gehoeren in den Executor
(hass.async_add_executor_job), nicht in den Event Loop.
"""

from __future__ import annotations

import os
import tempfile


def read_text(path: str) -> str:
    """Liest eine Datei als Text (fuer die Rollback-Sicherung vor einem Schreibvorgang).

    Liefert "" statt eines Fehlers, wenn die Datei (noch) nicht existiert -
    automations.yaml gibt es z.B. auf einer frischen HA-Installation erst,
    nachdem die erste Automation angelegt wurde.
    """

    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as f:
        return f.read()


def write_text(path: str, content: str) -> None:
    """Schreibt eine Datei atomar als Text.

    Erst in eine Temporaerdatei im selben Verzeichnis, dann per ``os.replace`` an den
    Zielpfad. Bricht Home Assistant mitten im Schreibvorgang ab, bleibt die alte Datei
    unveraendert stehen, statt halb geschrieben zurueckzubleiben - das Rollback in
    automations.py/dashboard.py greift nur bei einem fehlgeschlagenen Reload, nicht bei
    einem Absturz waehrend des Schreibens. ``os.replace`` ist auf demselben Dateisystem
    atomar, deshalb liegt die Temporaerdatei bewusst im Zielverzeichnis und nicht in
    /tmp.
    """

    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".smart_ha_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise
