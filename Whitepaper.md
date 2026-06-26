Hier ist der formelle Bauplan für deine API. Dieses Konzept (Whitepaper) dient dir als exakte Vorlage für die Programmierung deines Python-Skripts und später als Beschreibungstext (Dokumentation) für deine Kunden auf RapidAPI.

Der Fokus liegt auf maximaler technischer Klarheit und absoluter rechtlicher Absicherung für dich als Betreiber.

---

# Whitepaper & API-Spezifikation: Global Trademark Aggregation API

## 1. Executive Summary (Zusammenfassung)

Die *Global Trademark Aggregation API* ist eine hochperformante REST-Schnittstelle, die es Entwicklern, E-Commerce-Händlern und Content-Creatorn ermöglicht, automatisierte Markenrechtsprüfungen (Wortmarken) in Echtzeit durchzuführen. Anstatt verschiedene unübersichtliche Behörden-Datenbanken manuell abzufragen, konsolidiert die API die Daten der wichtigsten internationalen Patent- und Markenämter in einem sauberen, maschinenlesbaren JSON-Format.

**Die Philosophie (Rechtliche Absicherung):** Diese API ist ein rein technisches Daten-Übermittlungstool. Sie führt **keine** rechtlichen Bewertungen durch, spricht keine Warnungen aus und gibt keine "Safe/Unsafe"-Empfehlungen. Sie spiegelt ausschließlich den deskriptiven Ist-Zustand öffentlicher Register wider. Jegliche rechtliche Interpretation der zurückgegebenen Daten obliegt dem Endnutzer.

---

## 2. Abgedeckte Register (Data Sources)

Um den westlichen E-Commerce- und Print-on-Demand-Markt vollständig abzudecken, fragt das System die folgenden Datenbanken ab:

* **USPTO (United States Patent and Trademark Office):** Primärquelle für den US-amerikanischen und globalen Markt.
* **EUIPO (European Union Intellectual Property Office):** Abdeckung aller eingetragenen Unionsmarken (gültig in allen EU-Mitgliedsstaaten).
* **DPMA (Deutsches Patent- und Markenamt):** Spezifische Abdeckung für den DACH-Raum und lokale Händler.
* **UKIPO (United Kingdom Intellectual Property Office):** *[Optional/Pro-Tier]* Abdeckung für den britischen Markt nach dem Brexit.

---

## 3. Kernfunktionen & Filter-Parameter

Um Server-Ressourcen zu schonen und dem Nutzer die Datenaufbereitung abzunehmen, unterstützt der Endpoint `/v1/check-trademark` folgende Query-Parameter:

| Parameter | Typ | Beschreibung | Pflichtfeld |
| --- | --- | --- | --- |
| `keyword` | String | Der zu prüfende Begriff oder Satz. | **Ja** |
| `match_type` | String | Suchlogik. Erlaubte Werte: <br>

<br>`exact` (Sucht nur die isolierte Wortkombination).<br>

<br>`phrase` (Sucht, ob der Begriff Teil einer eingetragenen Phrase ist). | Nein (Default: `exact`) |
| `nice_class` | Integer | Filtert die Ergebnisse nach einer spezifischen Nizza-Klasse (z. B. `25` für Bekleidung). Ignoriert Treffer in anderen Klassen. | Nein |
| `office` | String | Beschränkt die Suche auf ein bestimmtes Amt (z. B. `USPTO`). Wird der Parameter weggelassen, werden alle Ämter durchsucht. | Nein |

---

## 4. Datenarchitektur (JSON Spezifikation)

Das Design der JSON-Antwort ist so aufgebaut, dass Entwickler mit minimalem Code-Aufwand Entscheidungen in ihren eigenen Systemen treffen können (z. B. Automatisierung stoppen, falls `match_found: true`).

### Beispiel-Request (Eingabe)

Ein Entwickler möchte ein T-Shirt-Design prüfen und sucht gezielt nach Einträgen für Bekleidung (Klasse 25) in den USA und Europa.

`GET /v1/check-trademark?keyword=wall%20art%20design&match_type=exact&nice_class=25`

### Beispiel-Response (Ausgabe)

Die Antwort ist streng deskriptiv. Status-Codes wie `active` oder `dead` werden direkt von den Behörden übernommen.

```json
{
  "meta": {
    "timestamp": "2026-06-26T14:00:02Z",
    "status_code": 200,
    "legal_disclaimer": "This API provides aggregated data from public registries "as is" and does not constitute legal advice."
  },
  "query_parameters": {
    "keyword": "wall art design",
    "match_type": "exact",
    "nice_class": 25,
    "office_filter": "all"
  },
  "result_summary": {
    "match_found": true,
    "total_active_registrations": 1
  },
  "details": [
    {
      "office": "USPTO",
      "registry_status": "active",
      "registration_number": "12345678",
      "registration_date": "2024-08-15",
      "owner": "Design Corp LLC",
      "nice_classes": [25, 35],
      "goods_and_services_description": "Clothing, namely, t-shirts, hoodies, and hats."
    },
    {
      "office": "EUIPO",
      "registry_status": "no_match_found",
      "registration_number": null,
      "registration_date": null,
      "owner": null,
      "nice_classes": [],
      "goods_and_services_description": null
    },
    {
      "office": "DPMA",
      "registry_status": "dead",
      "registration_number": "87654321",
      "registration_date": "2015-02-10",
      "owner": "Max Mustermann",
      "nice_classes": [25],
      "goods_and_services_description": "Bekleidungsstücke."
    }
  ]
}

```

---

## 5. Rechtliche Leitplanken für den Aufbau

Um dich maximal abzusichern, müssen beim Coden folgende Regeln strikt eingehalten werden:

1. **Keine Interpretation:** Das Skript darf nicht berechnen, ob zwei Wörter "ähnlich" sind (z. B. Puma vs. Poma), es sei denn, die Behörden-API liefert dies explizit als Fuzzy-Search-Ergebnis. Du lieferst nur Strings zurück.
2. **Disclaimer erzwingen:** Der `legal_disclaimer` muss in jedem einzelnen JSON-Response im Header oder Meta-Block mitgeschickt werden.
3. **Caching & Aktualität:** Wenn du die Daten zwischenspeicherst (um die Ämter nicht zu überlasten), musst du in einem Meta-Feld angeben, wie alt der Datenstand ist (z. B. `"last_database_sync": "2026-06-25"`), damit niemand dich verklagen kann, wenn eine Marke gestern frisch eingetragen wurde.