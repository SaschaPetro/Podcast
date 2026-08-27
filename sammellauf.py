"""Führt Takt 1 der Podcast-Pipeline aus: Sammeln & Aufbereiten (manueller Start).

Reihenfolge:
1. rss_einlesen.main()                                    - neue Rohnachrichten holen
2. recherche_und_redaktion.fuehre_recherche_agenten_aus()  - Vorschläge sammeln
3. recherche_und_redaktion.fuehre_redaktion_aus()          - Vorschläge entscheiden
4. recherche_und_redaktion.fuehre_notfall_auffuellung_aus() - Sicherheitsnetz bei zu wenig Themen
5. recherche_und_redaktion.verarbeite_akzeptierte_entscheidungen() - Themen anlegen/updaten
6. recherche_und_redaktion.pruefe_update_reaktivierung()   - Updates zu gesendeten Themen prüfen

Erzeugt/aktualisiert nur "rohnachrichten", "agent_vorschlaege",
"redaktion_entscheidungen" sowie "themen"/"themen_updates" - schreibt NIE in
"episoden". Das übernimmt der zeitkritische Takt 2 (morgenlauf.py), der aus
den hier vorbereiteten offenen Themen (Status "neu"/"in Verfolgung") das
Manuskript erzeugt. Architektonisch getrennt, damit Takt 1 (kann früher/
unabhängig laufen) und Takt 2 (zeitkritisch, muss vor der Veröffentlichungs-
Deadline fertig sein) getrennt getaktet und überwacht werden können.

Jeder Schritt läuft in einem eigenen try/except (siehe pipeline_utils.py):
schlägt einer fehl (Exception, API-Fehler wie 429 o.ä.), wird das deutlich
geloggt, aber die Kette läuft mit dem nächsten Schritt weiter. Schritt 4
(Notfall-Auffüllung) läuft bewusst NACH der normalen Redaktion (3.) und VOR
der Verarbeitung (5.): reicht die normale Redaktion allein nicht auf
MIN_THEMEN_FUER_EPISODE offene Themen, werden zunächst zurückgestellte, dann
notfalls auch bereits abgelehnte Vorschläge mit gelockertem Relevanz-Maßstab
erneut geprüft und bei Eignung akzeptiert - Duplikate, Gerüchte und
faktisch unbelegte Meldungen bleiben dabei weiterhin ausgeschlossen (siehe
recherche_und_redaktion.py, Punkt 6 im Modul-Docstring). Ist bereits genug
Material vorhanden, tut Schritt 4 nichts. Schritt 6 (Update-Reaktivierung)
läuft bewusst NACH Schritt 5, damit auch Updates berücksichtigt werden, die
im selben Lauf durch Schritt 5 selbst entstehen.

Am Ende wird eine Zusammenfassung ausgegeben und ein Protokoll-Eintrag
(Gesamtlaufzeit, Erfolgsstatus, Fehlerdetails, lauftyp="sammellauf") in der
Tabelle "lauf_protokoll" gespeichert. Voraussetzung: Migration
20260826073502_lauf_protokoll_lauftyp.sql muss angewendet sein.

Start ausschließlich manuell: `python sammellauf.py`. Kein Scheduler/Cron
(siehe README Abschnitt 10 zur geplanten Automatisierung).
"""
import sys
import time

from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import kosten_tracking
import pipeline_utils
import recherche_und_redaktion
import rss_einlesen

load_dotenv()


def formatiere_verarbeitung(ergebnisse: list[dict] | None) -> str:
    if not ergebnisse:
        return "keine akzeptierten Entscheidungen zu verarbeiten"
    neu = sum(1 for r in ergebnisse if r["art"] == "neu")
    updates = sum(1 for r in ergebnisse if r["art"] == "update")
    duplikate = sum(1 for r in ergebnisse if r["art"] == "duplikat")
    return f"{len(ergebnisse)} verarbeitet ({neu} neu, {updates} Updates, {duplikate} Duplikate)"


def formatiere_notfall_auffuellung(anzahl: int | None) -> str:
    if not anzahl:
        return "genug Themen vorhanden, keine Auffüllung nötig"
    return f"{anzahl} Thema/Themen per Notfall-Auffüllung akzeptiert"


def formatiere_update_reaktivierung(anzahl: int | None) -> str:
    if not anzahl:
        return "keine neuen Updates zu bereits gesendeten Themen"
    return f"{anzahl} Update-Entscheidung(en) geprüft"


def main() -> None:
    gesamt_start = time.monotonic()
    schritte: list[dict] = []

    lauf_id = pipeline_utils.starte_lauf_protokoll(lauftyp="sammellauf")

    schritte.append(pipeline_utils.fuehre_schritt_aus("1/6 RSS-Feeds einlesen", rss_einlesen.main))

    schritte.append(
        pipeline_utils.fuehre_schritt_aus(
            "2/6 Recherche-Agenten ausführen",
            lambda: recherche_und_redaktion.fuehre_recherche_agenten_aus(lauf_id=lauf_id),
        )
    )

    schritte.append(
        pipeline_utils.fuehre_schritt_aus(
            "3/6 Redaktion ausführen",
            lambda: recherche_und_redaktion.fuehre_redaktion_aus(lauf_id=lauf_id),
        )
    )

    schritte.append(
        pipeline_utils.fuehre_schritt_aus(
            "4/6 Notfall-Auffüllung bei zu wenig Themen",
            lambda: recherche_und_redaktion.fuehre_notfall_auffuellung_aus(lauf_id=lauf_id),
            formatiere_notfall_auffuellung,
        )
    )

    schritte.append(
        pipeline_utils.fuehre_schritt_aus(
            "5/6 Akzeptierte Entscheidungen verarbeiten",
            lambda: recherche_und_redaktion.verarbeite_akzeptierte_entscheidungen(lauf_id=lauf_id),
            formatiere_verarbeitung,
        )
    )

    schritte.append(
        pipeline_utils.fuehre_schritt_aus(
            "6/6 Update-Reaktivierung prüfen",
            lambda: recherche_und_redaktion.pruefe_update_reaktivierung(lauf_id=lauf_id),
            formatiere_update_reaktivierung,
        )
    )

    gesamt_dauer = time.monotonic() - gesamt_start

    gesamtkosten_usd = None
    try:
        supabase = pipeline_utils.hole_supabase_client()
        gesamtkosten_usd = kosten_tracking.hole_kosten_summe(supabase, lauf_id=lauf_id)
    except Exception as e:
        print(f"WARNUNG: Konnte API-Kosten nicht aggregieren: {type(e).__name__}: {e}")

    pipeline_utils.drucke_zusammenfassung(schritte, gesamt_dauer, gesamtkosten_usd)

    fehlgeschlagene_oder_uebersprungene = [s for s in schritte if s["status"] != "erfolgreich"]
    erfolgreich = not any(s["status"] == "fehlgeschlagen" for s in schritte)
    fehler_details = (
        "\n".join(f"{s['name']}: {s['fehler']}" for s in fehlgeschlagene_oder_uebersprungene)
        if fehlgeschlagene_oder_uebersprungene
        else None
    )

    pipeline_utils.aktualisiere_lauf_protokoll(lauf_id, round(gesamt_dauer), erfolgreich, fehler_details)

    if not erfolgreich:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
