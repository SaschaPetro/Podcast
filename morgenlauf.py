"""Führt Takt 2 der Podcast-Pipeline aus: der zeitkritische Morgenlauf
(manueller Start).

Setzt auf den von sammellauf.py (Takt 1) vorbereiteten offenen Themen auf
(Status "neu"/"in Verfolgung") - führt selbst KEINE eigene Recherche/
Redaktion mehr durch. sammellauf.py muss vorher gelaufen sein, damit
tatsächlich offene Themen vorliegen; ist das nicht der Fall, erzeugt Schritt
1 einfach keine Episode (kein Fehler, siehe unten).

Reihenfolge:
1. generiere_episode.erstelle_episode()  - Manuskript schreiben
2. generiere_episode.pruefe_manuskript() - Faktencheck gegen Original-Quellen
3. generiere_audio.text_zu_audio()       - Audio erzeugen (Deepgram)
4. rhetorik_check.pruefe_rhetorik()      - Rhetorik-Prüfung alle 4 Episoden

Jeder Schritt läuft in einem eigenen try/except (siehe pipeline_utils.py):
schlägt einer fehl (Exception, API-Fehler wie 429 o.ä.), wird das deutlich
geloggt, aber die Kette läuft mit dem nächsten Schritt weiter. Schritt 2
(Faktencheck) läuft nur, wenn Schritt 1 tatsächlich eine Episode mit
Manuskripttext geliefert hat. Schritt 3 (Audio) läuft nur, wenn Schritt 2
erfolgreich war UND keinen Widerspruch gefunden hat - sonst wird die
Episode NICHT automatisch vertont (Status in "episoden" bleibt
"ungeprueft"/schlägt fehl bzw. wird auf "pruefung_fehlgeschlagen" gesetzt,
siehe Migration 20260825080000_episoden_faktencheck.sql). Schritt 4
(Rhetorik-Prüfung) läuft UNABHÄNGIG vom Erfolg der Schritte 1-3: er prüft
die bereits gespeicherten letzten 4 Episoden insgesamt (nicht nur die aus
diesem Lauf) und wird nur alle 4 Episoden tatsächlich ausgeführt, sonst
übersprungen (siehe rhetorik_check.py). Anders als der Faktencheck in
Schritt 2 prüft er rhetorische Qualität (Wiederholungen, Struktur,
Einstieg), keine Fakten - reine Analyse/Empfehlung, ändert nichts
automatisch.

Am Ende wird eine Zusammenfassung ausgegeben (inkl. Rhetorik-Ergebnis, falls
eine Prüfung stattfand) und ein Protokoll-Eintrag (Gesamtlaufzeit,
Erfolgsstatus, Fehlerdetails, lauftyp="morgenlauf") in der Tabelle
"lauf_protokoll" gespeichert. Voraussetzung: Migration
20260826073502_lauf_protokoll_lauftyp.sql muss angewendet sein.

Start ausschließlich manuell: `python morgenlauf.py`. Kein Scheduler/Cron.
"""
import os
import sys
import time

from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import generiere_audio
import generiere_episode
import kosten_tracking
import pipeline_utils
import rhetorik_check

load_dotenv()

AUDIO_ANBIETER = "deepgram"
AUDIO_ORDNER = "output"


def faktencheck_blockiert_audio(ergebnis: dict) -> bool:
    return ergebnis.get("widerspruch", 0) > 0 or ergebnis.get("nicht_belegt", 0) > 0


def erzeuge_audio_fuer_episode(episode: dict, lauf_id: str | None = None) -> dict:
    episode_id = episode["id"]
    manuskripttext = episode["manuskripttext"]

    os.makedirs(AUDIO_ORDNER, exist_ok=True)
    dateipfad = f"{AUDIO_ORDNER}/episode_{episode_id}.mp3"

    audio_url = generiere_audio.text_zu_audio(
        manuskripttext, dateipfad, anbieter=AUDIO_ANBIETER, lauf_id=lauf_id, episode_id=episode_id
    )

    supabase = pipeline_utils.hole_supabase_client()
    aktualisierung = {"audio_pfad": dateipfad}
    if audio_url:
        aktualisierung["audio_url"] = audio_url
    supabase.table("episoden").update(aktualisierung).eq("id", episode_id).execute()

    return {"dateipfad": dateipfad, "audio_url": audio_url}


def formatiere_episode(episode: dict | None) -> str:
    if episode is None:
        return "keine offenen Themen, keine Episode erzeugt"
    laenge = len(episode.get("manuskripttext") or "")
    return f'Episode {episode["id"]} erzeugt ({laenge} Zeichen Manuskript)'


def formatiere_faktencheck(ergebnis: dict | None) -> str:
    if not ergebnis:
        return "kein Ergebnis"
    return (
        f'{ergebnis["bestaetigt"]} bestätigt, {ergebnis["widerspruch"]} '
        f'Widerspruch/Widersprüche, {ergebnis["nicht_belegt"]} nicht belegt'
    )


def formatiere_audio(ergebnis: dict) -> str:
    if ergebnis["audio_url"]:
        return f'Audio gespeichert: {ergebnis["dateipfad"]} (öffentlich: {ergebnis["audio_url"]})'
    return f'Audio gespeichert: {ergebnis["dateipfad"]} (Supabase-Upload fehlgeschlagen/übersprungen)'


def formatiere_rhetorik(ergebnis: dict) -> str:
    if ergebnis["status"] == "uebersprungen":
        return f'übersprungen - noch {ergebnis["fehlende_episoden"]} Episode(n) bis zur nächsten Prüfung'
    anzahl_probleme = len(ergebnis["konkrete_probleme"])
    return f'{len(ergebnis["episode_ids"])} Episode(n) geprüft, {anzahl_probleme} konkrete(s) Problem(e) gefunden'


def drucke_rhetorik_block(rhetorik_ergebnis: dict) -> None:
    print(f"\n{'-' * 70}")
    if rhetorik_ergebnis["status"] == "durchgefuehrt":
        print("RHETORIK-PRÜFUNG:")
        print(f'  Gesamteinschätzung: {rhetorik_ergebnis["gesamteinschaetzung"]}')
        probleme = rhetorik_ergebnis["konkrete_probleme"][:3]
        if probleme:
            print("  Wichtigste Punkte:")
            for p in probleme:
                print(f'  - {p.get("problem")}')
                print(f'    Beispiel: "{p.get("beispiel_zitat")}"')
                print(f'    Vorschlag: {p.get("vorschlag")}')
            print()
            print(
                f'  Rhetorik-Kritik gefunden - führe manuell '
                f'passe_manuskript_prompt_an(bewertung_id="{rhetorik_ergebnis["id"]}") aus, '
                "um eine Korrektur vorzuschlagen (Diff wird zum Gegenlesen angezeigt, bevor "
                "sie aktiv wird)."
            )
        else:
            print("  Keine konkreten Kritikpunkte - Folgen wurden als stark bewertet.")
    else:
        print(
            f'RHETORIK-PRÜFUNG: übersprungen - noch {rhetorik_ergebnis["fehlende_episoden"]} '
            "Episode(n) bis zur nächsten Prüfung."
        )
    print("-" * 70)


def main() -> None:
    gesamt_start = time.monotonic()
    schritte: list[dict] = []

    lauf_id = pipeline_utils.starte_lauf_protokoll(lauftyp="morgenlauf")

    schritt_manuskript = pipeline_utils.fuehre_schritt_aus(
        "1/4 Manuskript erzeugen",
        lambda: generiere_episode.erstelle_episode(lauf_id=lauf_id),
        formatiere_episode,
    )
    schritte.append(schritt_manuskript)

    episode = schritt_manuskript["ergebnis"]
    if schritt_manuskript["status"] != "erfolgreich":
        schritt_faktencheck = pipeline_utils.markiere_uebersprungen(
            "2/4 Faktencheck", "Schritt 1 ist fehlgeschlagen"
        )
    elif episode is None:
        schritt_faktencheck = pipeline_utils.markiere_uebersprungen(
            "2/4 Faktencheck", "kein Manuskript erzeugt (keine offenen Themen)"
        )
    else:
        schritt_faktencheck = pipeline_utils.fuehre_schritt_aus(
            "2/4 Faktencheck",
            lambda: generiere_episode.pruefe_manuskript(
                episode["id"], episode["manuskripttext"], episode["verwendete_themen"], lauf_id=lauf_id
            ),
            formatiere_faktencheck,
        )
    schritte.append(schritt_faktencheck)

    faktencheck_ergebnis = schritt_faktencheck["ergebnis"]
    if schritt_manuskript["status"] != "erfolgreich":
        schritte.append(pipeline_utils.markiere_uebersprungen("3/4 Audio erzeugen", "Schritt 1 ist fehlgeschlagen"))
    elif episode is None:
        schritte.append(
            pipeline_utils.markiere_uebersprungen("3/4 Audio erzeugen", "kein Manuskript erzeugt (keine offenen Themen)")
        )
    elif schritt_faktencheck["status"] != "erfolgreich":
        schritte.append(pipeline_utils.markiere_uebersprungen("3/4 Audio erzeugen", "Faktencheck fehlgeschlagen"))
    elif faktencheck_blockiert_audio(faktencheck_ergebnis):
        schritte.append(
            pipeline_utils.markiere_uebersprungen(
                "3/4 Audio erzeugen",
                f'Faktencheck hat {faktencheck_ergebnis["widerspruch"]} Widerspruch/Widersprüche und '
                f'{faktencheck_ergebnis["nicht_belegt"]} unbelegte Behauptung(en) gefunden - '
                "Episode nicht freigegeben",
            )
        )
    else:
        schritte.append(
            pipeline_utils.fuehre_schritt_aus(
                "3/4 Audio erzeugen",
                lambda: erzeuge_audio_fuer_episode(episode, lauf_id=lauf_id),
                formatiere_audio,
            )
        )

    schritt_rhetorik = pipeline_utils.fuehre_schritt_aus(
        "4/4 Rhetorik-Prüfung",
        lambda: rhetorik_check.pruefe_rhetorik(lauf_id=lauf_id),
        formatiere_rhetorik,
    )
    schritte.append(schritt_rhetorik)
    rhetorik_ergebnis = schritt_rhetorik["ergebnis"]

    gesamt_dauer = time.monotonic() - gesamt_start

    gesamtkosten_usd = None
    try:
        supabase = pipeline_utils.hole_supabase_client()
        gesamtkosten_usd = kosten_tracking.hole_kosten_summe(supabase, lauf_id=lauf_id)
        if episode is not None:
            episode_kosten_usd = kosten_tracking.hole_kosten_summe(supabase, episode_id=episode["id"])
            supabase.table("episoden").update({"kosten": episode_kosten_usd}).eq("id", episode["id"]).execute()
    except Exception as e:
        print(f"WARNUNG: Konnte API-Kosten nicht aggregieren: {type(e).__name__}: {e}")

    pipeline_utils.drucke_zusammenfassung(schritte, gesamt_dauer, gesamtkosten_usd)
    if rhetorik_ergebnis is not None:
        drucke_rhetorik_block(rhetorik_ergebnis)

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
