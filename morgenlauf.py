"""Führt die komplette Podcast-Pipeline in einem Rutsch aus (manueller Start).

Reihenfolge:
1. rss_einlesen.main()                                    - neue Rohnachrichten holen
2. recherche_und_redaktion.fuehre_recherche_agenten_aus()  - Vorschläge sammeln
3. recherche_und_redaktion.fuehre_redaktion_aus()          - Vorschläge/Updates entscheiden
4. recherche_und_redaktion.verarbeite_akzeptierte_entscheidungen() - Themen anlegen/updaten
5. generiere_episode.erstelle_episode()                    - Manuskript schreiben
6. generiere_audio.text_zu_audio()                         - Audio erzeugen (Deepgram)

Jeder Schritt läuft in einem eigenen try/except: schlägt einer fehl (Exception,
API-Fehler wie 429 o.ä.), wird das deutlich geloggt, aber die Kette läuft mit
dem nächsten Schritt weiter. Schritt 6 wird nur versucht, wenn Schritt 5
tatsächlich eine Episode mit Manuskripttext geliefert hat - sonst als
"übersprungen" markiert.

Am Ende wird eine Zusammenfassung ausgegeben und ein Protokoll-Eintrag
(Gesamtlaufzeit, Erfolgsstatus, Fehlerdetails) in der Tabelle
"lauf_protokoll" gespeichert (Migration 20260824170000_lauf_protokoll.sql
muss angewendet sein).

Start ausschließlich manuell: `python morgenlauf.py`. Kein Scheduler/Cron.
"""
import os
import sys
import time
import traceback
from datetime import datetime, timezone

from dotenv import load_dotenv
from supabase import create_client

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import generiere_audio
import generiere_episode
import recherche_und_redaktion
import rss_einlesen

load_dotenv()

AUDIO_ANBIETER = "deepgram"
AUDIO_ORDNER = "output"


def hole_supabase_client():
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def jetzt_lesbar() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def fuehre_schritt_aus(name: str, funktion, ergebnis_formatierer=None) -> dict:
    """Führt `funktion` aus, loggt Start/Ende/Dauer/Ergebnis und fängt Exceptions ab."""
    print(f"\n{'=' * 70}")
    print(f"[{name}] Start: {jetzt_lesbar()}")
    print("=" * 70)

    start = time.monotonic()
    try:
        rueckgabewert = funktion()
        dauer = time.monotonic() - start
        zusammenfassung = ergebnis_formatierer(rueckgabewert) if ergebnis_formatierer else "OK"
        print(f"[{name}] ERFOLGREICH - Dauer: {dauer:.1f}s - {zusammenfassung}")
        return {
            "name": name,
            "status": "erfolgreich",
            "dauer": dauer,
            "fehler": None,
            "ergebnis": rueckgabewert,
        }
    except Exception as e:
        dauer = time.monotonic() - start
        fehlertext = f"{type(e).__name__}: {e}"
        print(f"\n{'!' * 70}")
        print(f"[{name}] FEHLGESCHLAGEN nach {dauer:.1f}s")
        print(fehlertext)
        traceback.print_exc()
        print("!" * 70)
        return {
            "name": name,
            "status": "fehlgeschlagen",
            "dauer": dauer,
            "fehler": fehlertext,
            "ergebnis": None,
        }


def markiere_uebersprungen(name: str, grund: str) -> dict:
    print(f"\n{'=' * 70}")
    print(f"[{name}] ÜBERSPRUNGEN: {grund}")
    print("=" * 70)
    return {
        "name": name,
        "status": "uebersprungen",
        "dauer": 0.0,
        "fehler": grund,
        "ergebnis": None,
    }


def erzeuge_audio_fuer_episode(episode: dict) -> str:
    episode_id = episode["id"]
    manuskripttext = episode["manuskripttext"]

    os.makedirs(AUDIO_ORDNER, exist_ok=True)
    dateipfad = f"{AUDIO_ORDNER}/episode_{episode_id}.mp3"

    generiere_audio.text_zu_audio(manuskripttext, dateipfad, anbieter=AUDIO_ANBIETER)

    supabase = hole_supabase_client()
    supabase.table("episoden").update({"audio_pfad": dateipfad}).eq("id", episode_id).execute()

    return dateipfad


def formatiere_verarbeitung(ergebnisse: list[dict] | None) -> str:
    if not ergebnisse:
        return "keine akzeptierten Entscheidungen zu verarbeiten"
    neu = sum(1 for r in ergebnisse if r["art"] == "neu")
    updates = sum(1 for r in ergebnisse if r["art"] == "update")
    duplikate = sum(1 for r in ergebnisse if r["art"] == "duplikat")
    return f"{len(ergebnisse)} verarbeitet ({neu} neu, {updates} Updates, {duplikate} Duplikate)"


def formatiere_episode(episode: dict | None) -> str:
    if episode is None:
        return "keine offenen Themen, keine Episode erzeugt"
    laenge = len(episode.get("manuskripttext") or "")
    return f'Episode {episode["id"]} erzeugt ({laenge} Zeichen Manuskript)'


def formatiere_audio(dateipfad: str) -> str:
    return f"Audio gespeichert: {dateipfad}"


def schreibe_lauf_protokoll(dauer_sekunden: int, erfolgreich: bool, fehler_details: str | None) -> None:
    try:
        supabase = hole_supabase_client()
        supabase.table("lauf_protokoll").insert(
            {
                "dauer_sekunden": dauer_sekunden,
                "erfolgreich": erfolgreich,
                "fehler_details": fehler_details,
            }
        ).execute()
        print("Lauf-Protokoll gespeichert.")
    except Exception as e:
        print(f"WARNUNG: Konnte Lauf-Protokoll nicht speichern: {type(e).__name__}: {e}")


def drucke_zusammenfassung(schritte: list[dict], gesamt_dauer: float) -> None:
    print(f"\n{'#' * 70}")
    print("GESAMT-ZUSAMMENFASSUNG")
    print("#" * 70)

    symbole = {"erfolgreich": "OK ", "fehlgeschlagen": "FEHLER ", "uebersprungen": "SKIP "}
    for schritt in schritte:
        symbol = symbole[schritt["status"]]
        zeile = f"  [{symbol}] {schritt['name']} - {schritt['dauer']:.1f}s"
        if schritt["fehler"]:
            zeile += f" - {schritt['fehler']}"
        print(zeile)

    anzahl_erfolgreich = sum(1 for s in schritte if s["status"] == "erfolgreich")
    anzahl_fehlgeschlagen = sum(1 for s in schritte if s["status"] == "fehlgeschlagen")
    anzahl_uebersprungen = sum(1 for s in schritte if s["status"] == "uebersprungen")

    print(
        f"\n{anzahl_erfolgreich}/{len(schritte)} Schritte erfolgreich, "
        f"{anzahl_fehlgeschlagen} fehlgeschlagen, {anzahl_uebersprungen} übersprungen."
    )
    print(f"Gesamtlaufzeit: {gesamt_dauer:.1f}s")
    print("#" * 70)


def main() -> None:
    gesamt_start = time.monotonic()
    schritte: list[dict] = []

    schritte.append(fuehre_schritt_aus("1/6 RSS-Feeds einlesen", rss_einlesen.main))

    schritte.append(
        fuehre_schritt_aus(
            "2/6 Recherche-Agenten ausführen",
            recherche_und_redaktion.fuehre_recherche_agenten_aus,
        )
    )

    schritte.append(
        fuehre_schritt_aus(
            "3/6 Redaktion ausführen",
            recherche_und_redaktion.fuehre_redaktion_aus,
        )
    )

    schritte.append(
        fuehre_schritt_aus(
            "4/6 Akzeptierte Entscheidungen verarbeiten",
            recherche_und_redaktion.verarbeite_akzeptierte_entscheidungen,
            formatiere_verarbeitung,
        )
    )

    schritt_5 = fuehre_schritt_aus(
        "5/6 Manuskript erzeugen",
        generiere_episode.erstelle_episode,
        formatiere_episode,
    )
    schritte.append(schritt_5)

    episode = schritt_5["ergebnis"]
    if schritt_5["status"] != "erfolgreich":
        schritte.append(markiere_uebersprungen("6/6 Audio erzeugen", "Schritt 5 ist fehlgeschlagen"))
    elif episode is None:
        schritte.append(markiere_uebersprungen("6/6 Audio erzeugen", "kein Manuskript erzeugt (keine offenen Themen)"))
    else:
        schritte.append(
            fuehre_schritt_aus(
                "6/6 Audio erzeugen",
                lambda: erzeuge_audio_fuer_episode(episode),
                formatiere_audio,
            )
        )

    gesamt_dauer = time.monotonic() - gesamt_start
    drucke_zusammenfassung(schritte, gesamt_dauer)

    fehlgeschlagene_oder_uebersprungene = [s for s in schritte if s["status"] != "erfolgreich"]
    erfolgreich = not any(s["status"] == "fehlgeschlagen" for s in schritte)
    fehler_details = (
        "\n".join(f"{s['name']}: {s['fehler']}" for s in fehlgeschlagene_oder_uebersprungene)
        if fehlgeschlagene_oder_uebersprungene
        else None
    )

    schreibe_lauf_protokoll(round(gesamt_dauer), erfolgreich, fehler_details)


if __name__ == "__main__":
    main()
