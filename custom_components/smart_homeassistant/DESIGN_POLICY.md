# Design-Policy: Spatial Glass für SMART-HOMEASSISTANT

Diese Policy ist die für dieses Projekt angepasste Fassung eines generischen
"Spatial Glass"-Regelwerks (visionOS-artige Glasoptik). Sie ist hier bewusst
**keine** wörtliche Übernahme: das Original ist für eine selbst gebaute
Web-App geschrieben (eigenes CSS, eigene Komponenten, Container Queries).
Home-Assistant-Dashboards laufen dagegen durch ein fixes, von uns nicht
änderbares Frontend. Diese Datei sagt daher pro Regel, **welche Schicht sie
umsetzt** — und nennt ehrlich, was gar nicht geht.

## Warum diese Datei existiert

Das Sprachmodell (`ollama_client.py`) entscheidet **nie** über Optik — nur
darüber, welche Entities in welcher Gruppe mit welchem Titel landen (siehe
Docstring von `dashboard_design.py`). Die Optik kommt vollständig aus
`dashboard_design.py`, also aus deterministischem Python-Code. Genau deshalb
kommt dort nicht "irgendwas" heraus: Diese Datei beschreibt die Regeln, an die
sich dieser Code hält, damit sie nicht nur implizit im Quelltext stehen,
sondern nachlesbar und pruefbar sind.

## Die zwei Schichten

| Schicht | Wo | Wer setzt sie | Kontrolliert |
|---|---|---|---|
| **Theme** | `config/themes/visionos/*.yaml` (HACS, `Nezz/homeassistant-visionos-theme`) | Wird einmalig installiert/gewählt, nicht von uns generiert | Glas-Material (Blur, Transparenz, Schatten), Eckenradius, Grundfarben, Hell/Dunkel |
| **Struktur** | `dashboard_design.py` | Wird bei jeder Dashboard-Erstellung neu ausgeführt | Welche Kartentypen, Gruppierung, Breiten/Zeilen, welche Bedienelemente, welcher Text |

Die zwei Schichten reden nur über **Theme-Variablen** miteinander (z. B.
`color: "accent"` in einer Tile-Karte löst zu `var(--accent-color)` auf, das
`dashboard_design.py` nie selbst als Hex-Wert kennt). Das ist beabsichtigt:
ändert der Nutzer das Theme, folgen alle generierten Dashboards automatisch,
ohne dass Python-Code angefasst werden muss.

## Aktuell aktive Theme-Werte (Referenz, nicht von uns gepflegt)

Aus `themes/visionos/visionos.yaml` (Stand: Installation über HACS):

```
ha-card-border-radius:   20px
ha-card-backdrop-filter: blur(20px)
ha-card-box-shadow:      0.5px 0.5px 1px rgba(255,255,255,.40) inset,
                          -0.5px -0.5px 1px rgba(255,255,255,.10) inset,
                          0px 1px 2px rgba(0,0,0,.10)
accent-color:             var(--primary-color)   /* = orange in diesem Theme */
divider-color:            rgba(152,152,157,0.3)
```

Wichtige Nuance: `state-icon-active-color` (Standardfarbe für "aktive" Icons,
z. B. eine Kamera-Badge) ist im Theme separat auf Gelb gesetzt und **nicht**
identisch mit `accent-color`. Unsere "ein Akzent"-Regel unten gilt für alles,
was `dashboard_design.py` selbst mit `color:` belegt (Tile-Karten) - nicht für
Elemente, die HA ohne unser Zutun einfärbt (z. B. Badges im "aktiven" Zustand).
Das ist eine Theme-Entscheidung, keine unsere.

## 1. Farbe — genau EIN Akzent

**Regel:** Jede von uns erzeugte Tile-Karte bekommt `"color": "accent"`
(Konstante `_ACCENT_COLOR` in `dashboard_design.py`). Keine Farbe pro
Geräteart mehr (früher: Licht=amber, Schloss=rot, Energie=grün, …).

**Warum:** Spatial-Glass-Prinzip "ruhiges Licht, ein einziger Akzent, Kontrast
durch Helligkeit statt durch Buntheit". Unterscheidung zwischen Geräten kommt
über Icon (`_DOMAIN_ICON`, `_TITLE_ICONS`) und Text, nicht über Farbe.

**Warum "accent" statt eines Hex-Werts:** `"accent"` ist ein von Home
Assistant reserviertes Schlüsselwort (`THEME_COLORS` in
`compute-color.ts` des Frontends) und löst zu `var(--accent-color)` des
*gerade aktiven* Themes auf. Ein hartkodierter Hex-Wert würde bei Hell/Dunkel
oder einem Theme-Wechsel falsch aussehen; das Schlüsselwort bleibt immer
synchron.

**Ausnahme, bewusst:** Messwerte (`sensor`-Karten) bekommen **keine**
Farbbewertung (kein Ampel-Rot/Gelb/Grün), auch nicht den Akzent - siehe
Regel 3. Das ist keine zweite Akzentfarbe, sondern die neutrale
Verlaufsdarstellung selbst.

## 2. Form & Geometrie

Eckenradien, Blur und Schatten kommen **ausschließlich aus dem Theme** (siehe
Tabelle oben) - `dashboard_design.py` setzt hier nichts. Was wir *nicht* tun,
weil es nicht geht: eigene `border-radius`/`box-shadow`-Werte generieren, wie
es das Original-Regelwerk für eigenes CSS vorschreibt. Lovelace-Karten haben
keine YAML-Option dafür; das ist reine Theme-Sache.

## 3. Keine wertenden Farbskalen

**Regel:** Kein Ampel-Rot/Gelb/Grün auf einem Messwert, keine willkürliche
Gauge-Skala. Ein `sensor`-Wert bekommt stattdessen groß den aktuellen Wert
plus eine neutrale 24h-Verlaufskurve (`_card_for_entity`, `type: sensor` mit
`graph: line`).

**Warum:** Eine Skala behauptet eine fachliche Norm, die wir nicht kennen
(0 W PV-Leistung ist nachts normal, nicht "kritisch"). Siehe Modul-Docstring
von `dashboard_design.py`.

## 4. Kein aggregierter Status-Text

**Regel:** Kein Satz wie "3 von 5 Lichtern an" oder "1 von 3 Jalousien offen"
irgendwo im Dashboard - egal ob als Kopfzeilen-Text, Badge-Beschriftung oder
in einer neuen Karte. Das gilt dauerhaft, nicht nur rueckblickend fuer die
inzwischen entfernte Begruessungs-Kopfzeile (`_header_card`, war eine Markdown-
Karte mit genau solchen Saetzen).

**Warum:**
- *Inhalt zuerst:* eine Aggregat-Zahl wie "3 von 5" ist selbst eine
  Deko-Information - sie fasst zusammen, was die einzelnen Kacheln direkt
  darunter schon zeigen, ohne dass man dafuer rechnen oder tippen koennte.
  Wenn ein Element keine neue Information traegt, wird es entfernt.
- *Single-Page-Ziel* (Abschnitt 5): jede zusaetzliche Zeile Kopfbereich
  erhoeht das Risiko, dass das Dashboard nicht mehr ohne Scrollen passt.
- Ein Jinja-Ausdruck wie `is_state(...)` pro Entity in einem Markdown-Text ist
  ausserdem gegenueber Umbenennungen/Loeschungen von Entities zerbrechlich -
  die Tile-Karten selbst zeigen denselben Zustand robuster an.

**Falls doch mal ein Kurzueberblick gewuenscht ist:** dafuer gibt es Badges
(Abschnitt 5) - die zeigen den Zustand EINER konkreten, ausgewaehlten Entity
(Name + Zustand + Icon), keine ausgerechnete Aggregat-Zahl ueber mehrere
Entities hinweg.

## 5. Layout & Raster

**Regeln, durchgesetzt in Code:**

- Kacheln sind halbbreit (`_HALF_WIDTH`, 6 von 12 Spalten), Diagramme/Kameras
  volle Breite (`_FULL_WIDTH`, 12 Spalten) - Karten bekommen die Größe, die
  ihr Inhalt braucht.
- Kachelbreiten bleiben unveraendert (kein nachtraegliches Aufblasen einer
  allein verbleibenden halbbreiten Kachel auf volle Breite - das erzeugte in
  einer frueheren Version grosse, halb leere Karten). Kompaktheit kommt
  stattdessen aus kleinen Kartenrastern plus `dense_section_placement`, das
  Home Assistant automatisch *zwischen* Sections anwendet.
- 2 bis 3 Gruppen pro Dashboard (`_MAX_GROUPS`), höchstens eine
  Ein-Element-Gruppe, keine doppelten oder nichtssagenden Titel
  (`check_layout_quality`, `_MIN_ENTITIES_FOR_GROUPING`). Absichtlich klein
  gehalten fürs Single-Page-Ziel: bei `max_columns` Spalten passen so viele
  Sections nebeneinander in eine Zeile, statt auf eine zweite umzubrechen.
- Keine eigene Begrüßungs-Karte im Header (siehe Abschnitt 4) - Titel und
  Badges zeigt Home Assistant automatisch als kompakte Kopfzeile ohne Karte,
  wenn `header.card` fehlt.
- Höchstens `_MAX_BADGES` (3) Badges in der Kopfzeile, priorisiert nach
  Aussagekraft (`_BADGE_DOMAIN_PRIORITY`) - ebenfalls fürs Single-Page-Ziel
  klein gehalten.

**Nicht anwendbar:** Container Queries, eigene Breakpoints, eigenes
`grid-template-columns` - die Sections-View von Home Assistant übernimmt das
responsive Verhalten selbst (`max_columns`, siehe `render_designed_view`).

## 6. Bedienelemente — kein Feintuning auf der Kachel

**Regel:** Lichter schalten nur ein/aus (kein Helligkeits-Schieberegler),
Jalousien fahren nur rauf/runter/stop (kein Prozent-Schieberegler). Siehe
Kommentar in `_card_for_entity` direkt über der Kachel-Konstruktion.

**Warum:** Feinstufige Kontrollen laden zu ungewollten Wischgesten auf einem
Dashboard ein, das oft nur überflogen wird. Feinjustierung bleibt über den
Detaildialog (Antippen des Namens/Icons statt der Kachelfläche) erreichbar.

## 7. Text — kurz, kein Hersteller-Präfix

**Regel:** Jede Tile-Karte bekommt einen expliziten `name` über
`_short_name()`. Diese Funktion bevorzugt den kurzen Registry-Namen und
entfernt zusätzlich einen redundanten Hersteller-Präfix (z. B. "Aqara Lampe
Wohnzimmer" → "Lampe Wohnzimmer"), ermittelt über das Geräteregister - nicht
hartkodiert auf eine Marke.

**Warum:** Home Assistants Tile-Karte bricht lange Namen nicht um, sondern
schneidet sie hart mit "…" ab (`white-space: nowrap` + `text-overflow:
ellipsis`, fest in `ha-tile-info.ts` des Frontends, nicht konfigurierbar).
Ein kürzerer Name ist die einzige verlässliche Gegenmaßnahme.

## 8. Was aus dem Original-Regelwerk NICHT gilt (mit Begründung)

| Abschnitt im Original | Warum nicht anwendbar |
|---|---|
| `src/styles/tokens.css`, `base.css` | Es gibt kein eigenes Frontend-Build; Lovelace-Karten sind fertige, von HA kompilierte Komponenten. |
| Container Queries pro Widget | Lovelace-Karten haben keine Konfigurationsoption dafür; die Sections-View regelt Responsivität selbst über `max_columns`. |
| Eigene Button-/Input-/Nav-Komponenten | Es gibt nur die Kartentypen, die Home Assistant mitbringt (`tile`, `sensor`, `picture-entity`, `heading`, `grid`, `sections`, `markdown`). |
| CSS-Hintergrund-Layer (Layer A/B/C, Muster, `feTurbulence`) | Der Dashboard-Hintergrund ist Theme-Sache (`background-image`/`lovelace-background` in der Theme-YAML), nicht etwas, das pro generiertem Dashboard gesetzt wird. |
| Bewegungs-Kurven, `prefers-reduced-motion` | Animationen liegen vollständig im HA-Frontend; wir erzeugen nur YAML, keine Interaktionslogik. |
| `card-mod`-Spezifika (in der Theme-Datei als optionale Erweiterung vorhanden) | Bewusst nicht als Abhängigkeit installiert - siehe Entscheidung weiter oben im Chat-Verlauf: zusätzliche, fragile Abhängigkeit für einen lokalen HA-Server. Das Theme funktioniert auch ohne card-mod (die `ha-card-*`-Variablen sind normale, native Theme-Variablen). |

## 9. Checkliste für Änderungen an `dashboard_design.py`

- [ ] Keine Farbe außer `_ACCENT_COLOR` auf einer Tile-Karte
- [ ] Keine Ampel-/Gauge-Farbbewertung auf einem Messwert
- [ ] Neue Kartentypen mit `grid_options.columns` versehen (`_HALF_WIDTH`
      oder `_FULL_WIDTH`), damit sie sich korrekt ins Sektionsraster einfuegen
- [ ] Neue Bedien-Features prüfen: zeigen sie einen Schieberegler? Wenn ja,
      bewusst entscheiden, ob das zur Regel 5 passt
- [ ] Lange Namen über `_short_name()` kürzen, nicht den rohen `friendly_name`
      verwenden
- [ ] Änderung gegen `check_layout_quality` laufen lassen, falls sie die
      Gruppierungs-Regeln berührt
