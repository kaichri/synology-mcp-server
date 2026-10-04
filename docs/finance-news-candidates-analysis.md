# Finance-Kandidaten und MCP-Annotations: Analyse

Stand: 2026-10-04. Basis: 0b9d68b, Branch main. Keine Finance-Logik geändert, keine Live-API-Requests, keine NAS-Änderung.

## Empfehlung: KEEP + FIX

Das Tool erzeugt aus mehreren Finance-Interests eine kompakte, strukturierte Kandidatenliste für Markt-/News-Digests. Allgemeine Webtools liefern Such- und Seiteninhalte, übernehmen aber nicht die Interest-Filter, Zuordnung, Lookback, Deduplizierung und Prioritätsauswahl. Sie sind daher kein vollständiger Ersatz. Das Tool sollte erhalten bleiben; die unten aufgeführten Korrekturen benötigen eine separate Freigabe.

## Tatsächlicher Datenfluss

1. Parameter werden auf 1–10 Kandidaten und 6–72 Stunden begrenzt. JSON akzeptiert eine Liste oder ein Objekt mit `interests`. Ungültiges JSON bzw. nicht-listiges Ergebnis liefert ein Fehlerobjekt mit leerer Kandidatenliste.
2. Akzeptiert werden Dictionaries mit exakt `category=finance` und `news_alert != off` (Default normal). Priority und Topics filtern nicht; sie beeinflussen Suchanfrage und spätere Reihenfolge. Ein leerer Name wird übersprungen, aber trotzdem in `searched_interests` gezählt. Die Zahl der Interests ist nicht begrenzt.
3. Namen werden normalisiert; die ersten fünf normalisierten Topics werden in Anführungszeichen ergänzt. Suchanfrage: `"<name>" "<topic1>" ... latest news finance`. Pro benanntem Interest wird `web_search(query, 6)` aufgerufen. Kein Datum-/Lookback-Filter wird an Exa übergeben. Der bestehende Provider-Router übernimmt Direct/Remote-Auswahl.
4. `_parse_exa_results` akzeptiert JSON-Listen bzw. Listen unter results/items/data, danach Title/URL/Published/Text-Blöcke und zuletzt reine URLs. Nicht-Dictionary-Listenelemente werden übersprungen.
5. `_extract_news_item`: title/name, url/link, publishedDate/published_date/date, text/snippet/description/highlights. Source ist Hostname ohne www.; Topics werden per case-insensitive Substring im ursprünglichen Titel/Snippet erkannt. Snippet wird auf 700 Zeichen begrenzt. Ein fehlender URL-Treffer wird verworfen.
6. Erste Deduplizierung: komplette URL lowercase und ohne abschließenden Slash. Erstes Interest gewinnt die restlichen Felder; weitere Treffer vereinigen nur matched_topics. Publikationsdaten werden bereits hier auf Kalenderdatum reduziert.
7. Fehlt Titel, Snippet oder Datum, erfolgt ein zusätzlicher Direct-HTTP-Fetch pro eindeutiger URL: höchstens drei gleichzeitig, 12 Sekunden Timeout, höchstens 2 MB gelesen. Kein Exa-Contents-Aufruf. Es werden auch später als zu alt erkannte Resultate vorher angereichert.
8. Direct Parsing: HTML-Titel/OG, Published-Meta oder JSON-LD, Beschreibung oder bereinigter Seitentext, sonst Datumssuche im Text (neuester gefundener Datumswert). Source bleibt URL-Hostname. Topics kommen aus dem über Interest-ID wiedergefundenen Interest. Angereicherte nichtleere Felder überschreiben auch vorhandene Suchfelder; nicht nur fehlende Felder.
9. Lookback: erkannte Kalenderdaten werden als UTC-Mitternacht mit `now - lookback_hours` verglichen. Undatierte oder nicht interpretierbare Datumswerte bleiben ausdrücklich erhalten (`undated_kept`). Es gibt keinen Ausschluss zukünftiger Daten.
10. Zweite Deduplizierung wiederholt den gleichen Schlüssel nach dem Lookback. `duplicates_removed` misst nur diese zweite Stufe; vorher beseitigte Duplikate erscheinen nicht im Zähler.
11. Ranking erfolgt zweimal mit demselben aufsteigenden Key: critical, high, medium, low, unbekannt; Topic-Match vor keinem Match; published aufsteigend. Undatierte Werte erhalten 0000-00-00 und stehen innerhalb derselben Gruppe zuerst. Anschließend wird auf max_candidates gekürzt.
12. Rückgabe ist ein JSON-String mit lookback_hours, searched_interests, raw_results, urls_found, fetched, fetch_failed, undated_kept, duplicates_removed, candidate_count und candidates. Jeder Kandidat enthält asset, interest_id, category, priority, matched_topics, title, url, source, published, snippet und news_key.

## Nutzung und vorhandene Tests

- Keine interne Produktionskomponente und kein versionierter Cron/Scheduler ruft `finance_news_candidates` auf. Es ist MCP-exposed; README und Schema-Snapshot dokumentieren/prüfen seine Existenz.
- `_extract_news_item` und `_fetch_news_candidate_direct` werden intern vom Finance-Tool verwendet. `_parse_exa_results` wird im versionierten Produktionscode nur dort verwendet.
- `tests/test_business_regression.py::test_news_parsing_and_invalid_input` prüft Textparser und ungültiges JSON. OAuth-/Registry- und Schema-Tests prüfen Exposition, Auth-Metadaten und unveränderte Ein-/Ausgabeschemas, keine umfassende Finance-Logik.
- Keine belastbaren Runtime-Nutzungsdaten wurden für diese Analyse erhoben. Fehlende interne Call-Sites beweisen keine fehlende externe Nutzung. Der vom Benutzer berichtete Live-Test belegt grundsätzliche Funktion für dessen Aufruf, aber keine allgemeine Nutzungshäufigkeit.

## Bestätigte Schwachstellen; noch nicht behoben

- Undatierte Treffer bleiben bewusst erhalten. Das schützt Recall, bestätigt aber keinen aktuellen News-Bezug; ein altes undatiertes PDF kann den Lookback passieren. Für Latest-News ist eine separat entscheidbare Policy sinnvoll (ausschließen oder ausdrücklich unbestätigt markieren).
- Direct-Fetch dekodiert alle Antworten als Text und verwirft den Content-Type in `_fetch_news_candidate_direct`; weder application/pdf noch `%PDF-` werden geprüft. Eine synthetische PDF-Antwort wird als Snippet übernommen. Minimaler späterer Fix: binäre/PDF-Antworten vor dem HTML-Parsing überspringen und Suchmetadaten behalten; keine PDF-Dependency nötig.
- Datums-Ranking ist ein Bug: ältere datierte Treffer stehen bei gleicher Priority/Topic-Gruppe vor neueren. Zweiter Sort ist redundant. Minimaler späterer Fix: ein Sort mit unverändertem Priority/Topic-Key und absteigendem validiertem Datumswert; undatierte Treffer nach expliziter Policy behandeln.
- Zeitinformation geht verloren: ein Treffer von 11:00 wird bei now=12:00 und 6h Lookback als Tagesbeginn interpretiert und verworfen. Auch längere Lookbacks sind an Tagesgrenzen ungenau. Später vollständige UTC-Timestamps erhalten; reine Datumswerte als ungenau behandeln.
- Deduplizierungszähler zeigt trotz doppeltem Suchtreffer 0. URL-Lowercasing verschmilzt auch verschiedene case-sensitive Pfade; das zuerst auftauchende Interest kann eine niedrigere Priority festhalten.
- Fehlende/gleiche Interest-IDs können bei der HTTP-Anreicherung Topics dem falschen Interest zuordnen (Dictionary last-wins). Ungültige Topic-Typen, z. B. eine Zahl, können statt Fehlerobjekt AttributeError auslösen; Validierung ist unvollständig.
- Suchfehler werden übersprungen bzw. strukturierte Provider-Fehler können wie leere Ergebnisse wirken; `searched_interests` zählt gefilterte, nicht tatsächlich erfolgreich gesuchte Interests.
- Statische Security-Auffälligkeit: der Finance-Direct-Fetch über urllib prüft öffentliche DNS/IPs und Redirect-Ziele nicht explizit. Suchresultat-URLs fließen direkt hinein. Eine SSRF-Grenzprüfung wäre separat nötig; es wurden keine internen URLs live abgerufen.
- Eine unbenutzte lokale Funktion one_fetch bleibt neben one_fetch2 stehen. Keine Bereinigung vorgenommen.

## Deterministische Probes

16 Offline-Probes mit Mocks bestanden, 0 externe Requests: mehrere Interests; Priority/Topics; Nicht-Finance/off; ungültiges JSON; leere Resultate; doppelte URLs; undatierte Resultate; fehlendes Snippet/HTTP-Anreicherung; PDF-Binärsnippet; alte datierte Treffer; Topic/no-topic-Reihenfolge; ältere-vor-neueren-Ranking; Timestamp-Verlust; fehlender Name im Zähler; first-interest-wins; case-sensitive Pfadkollision; ungültige Topic-Typen. Die Bug-Probes bestätigen den aktuellen Fehlerzustand, sie sind keine Zusicherung korrekter News-Semantik. Das Diagnose-Skript liegt lokal unter der ignorierten .venv und ist kein Produktions-/Git-Artefakt.

## ToolAnnotations

Alle 13 exponierten Tools erhalten die vier expliziten Boolean-Hints. Nur current_time arbeitet ohne offene externe Ressource. yt-dlp nutzt --no-download / --skip-download und liest Metadaten, Kommentare bzw. Untertitel; subprocess allein macht keine destruktive Operation. Die X-Caches sind interne Implementierung: wiederholte Leseaufrufe erzeugen keine zusätzliche externe Schreibwirkung. Live-Antworten dürfen sich ändern, ohne idempotentHint zu widersprechen.

| Tool | readOnly | destructive | idempotent | openWorld | Begründung |
|---|---|---|---|---|---|
| current_time | true | false | true | false | Lokale Systemzeit |
| web_search | true | false | true | true | Exa / öffentliche Webseiten |
| web_fetch | true | false | true | true | Exa / öffentliche Webseiten |
| web_deep_search | true | false | true | true | Exa / öffentliche Webseiten |
| web_search_and_fetch | true | false | true | true | Exa / öffentliche Webseiten |
| finance_news_candidates | true | false | true | true | Exa und HTTP |
| youtube_metadata | true | false | true | true | Öffentliche YouTube-Daten |
| youtube_transcript | true | false | true | true | Öffentliche YouTube-Daten |
| youtube_comments | true | false | true | true | Öffentliche YouTube-Daten |
| x_read_post | true | false | true | true | Öffentliche X-Daten; optional lokaler Cache |
| x_read_thread | true | false | true | true | Öffentliche X-Daten; optional lokaler Cache |
| x_search | true | false | true | true | Öffentliche X-Daten; optional lokaler Cache |
| x_search_user | true | false | true | true | Öffentliche X-Daten; optional lokaler Cache |

Referenz: https://modelcontextprotocol.io/specification/2025-11-25/schema#toolannotations

Produktiv-Diff: ausschließlich openWorldHint=False bei current_time und openWorldHint=True bei den zwölf übrigen @mcp.tool-Dekoratoren. Keine Tool-Namen, Beschreibungen, Schemas oder Finance-Logik geändert. Zwei parametrisierte Tests prüfen den realen tools/list-Response auf LAN und externem Listener: alle vier Hints vorhanden, echte Booleans, richtige Semantik.

## Abschlussprüfung

Vorher: 294 Tests bestanden (34,39 s). Nach Änderung: 296 Tests bestanden (36,19 s), einschließlich der zwei Listener-Annotation-Fälle. 16 zusätzliche lokale Offline-Probes bestanden, 0 externe Requests. git diff --check erfolgreich. AST-Vergleich: Produktivcode identisch nach Entfernen ausschließlich der neuen openWorldHint-Keywords. Prüfung vorhandener Secret-Werte gegen geänderte Dateien, Analyse und lokalen Diff ohne Treffer. Keine Secrets oder Konfiguration geändert. Kein Commit, Push, PR oder Deployment. Commit der Annotations, Tests und dieses Berichts empfohlen; Finance-Fixes separat entscheiden.
