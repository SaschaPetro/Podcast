"""Führt die komplette Podcast-Pipeline in einem Rutsch aus (manueller Start).

Reihenfolge:
1. rss_einlesen.main()                                    - neue Rohnachrichten holen
2. recherche_und_redaktion.fuehre_recherche_agenten_aus()  - Vorschläge sammeln
3. recherche_und_redaktion.fuehre_redaktion_aus()          - Vorschläge entscheiden
4. recherche_und_redaktion.verarbeite_akzeptierte_entscheidungen() - Themen anlegen/updaten
5. recherche_und_redaktion.pruefe_update_reaktivierung()   - Updates zu gesendeten Themen prüfen
6. generiere_episode.erstelle_episode()                    - Manuskript schreiben
7. generiere_episode.pruefe_manuskript()                   - Faktencheck gegen Original-Quellen
8. generiere_audio.text_zu_audio()                         - Audio erzeugen (Deepgram)
9. rhetorik_check.pruefe_rhetorik()                        - Rhetorik-Prüfung alle 4 Episoden

Jeder Schritt läuft in einem eigenen try/except: schlägt einer fehl (Exception,
API-Fehler wie 429 o.ä.), wird das deutlich geloggt, aber die Kette läuft mit
dem nächsten Schritt weiter. Schritt 5 (Update-Reaktivierung) läuft bewusst
NACH Schritt 4, damit auch Updates berücksichtigt werden, die im selben Lauf
durch Schritt 4 selbst entstehen. Schritt 7 (Faktencheck) läuft nur, wenn
Schritt 6 tatsächlich eine Episode mit Manuskripttext geliefert hat. Schritt 8
(Audio) läuft nur, wenn Schritt 7 erfolgreich war UND keinen Widerspruch
gefunden hat - sonst wird die Episode NICHT automatisch vertont (Status in
"episoden" bleibt "ungeprueft"/schlägt fehl bzw. wird auf
"pruefung_fehlgeschlagen" gesetzt, siehe Migration
20260825080000_episoden_faktencheck.sql). Schritt 9 (Rhetorik-Prüfung) läuft
UNABHÄNGIG vom Erfolg der Schritte 6-8: er prüft die bereits gespeicherten
letzten 4 Episoden insgesamt (nicht nur die aus diesem Lauf) und wird nur
alle 4 Episoden tatsächlich ausgeführt, sonst übersprungen (siehe
rhetorik_check.py). Anders als der Faktencheck in Schritt 7 prüft er
rhetorische Qualität (Wiederholungen, Struktur, Einstieg), keine Fakten -
reine Analyse/Empfehlung, ändert nichts automatisch.

Am Ende wird eine Zusammenfassung ausgegeben (inkl. Rhetorik-Ergebnis, falls
eine Prüfung stattfand) und ein Protokoll-Eintrag (Gesamtlaufzeit,
Erfolgsstatus, Fehlerdetails) in der Tabelle "lauf_protokoll" gespeichert
(Migration 20260824170000_lauf_protokoll.sql muss angewendet sein).

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
import kosten_tracking
import recherche_und_redaktion
import rhetorik_check
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


def erzeuge_audio_fuer_episode(episode: dict, lauf_id: str | None = None) -> dict:
    episode_id = episode["id"]
    manuskripttext = episode["manuskripttext"]

    os.makedirs(AUDIO_ORDNER, exist_ok=True)
    dateipfad = f"{AUDIO_ORDNER}/episode_{episode_id}.mp3"

    audio_url = generiere_audio.text_zu_audio(
        manuskripttext, dateipfad, anbieter=AUDIO_ANBIETER, lauf_id=lauf_id, episode_id=episode_id
    )

    supabase = hole_supabase_client()
    aktualisierung = {"audio_pfad": dateipfad}
    if audio_url:
        aktualisierung["audio_url"] = audio_url
    supabase.table("episoden").update(aktualisierung).eq("id", episode_id).execute()

    return {"dateipfad": dateipfad, "audio_url": audio_url}


def formatiere_verarbeitung(ergebnisse: list[dict] | None) -> str:
    if not ergebnisse:
        return "keine akzeptierten Entscheidungen zu verarbeiten"
    neu = sum(1 for r in ergebnisse if r["art"] == "neu")
    updates = sum(1 for r in ergebnisse if r["art"] == "update")
    duplikate = sum(1 for r in ergebnisse if r["art"] == "duplikat")
    return f"{len(ergebnisse)} verarbeitet ({neu} neu, {updates} Updates, {duplikate} Duplikate)"


def formatiere_update_reaktivierung(anzahl: int | None) -> str:
    if not anzahl:
        return "keine neuen Updates zu bereits gesendeten Themen"
    return f"{anzahl} Update-Entscheidung(en) geprüft"


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


def starte_lauf_protokoll() -> str | None:
    """Legt den lauf_protokoll-Eintrag VOR dem eigentlichen Lauf an (mit
    Platzhalterwerten) und gibt seine id zurück. Diese id wird als lauf_id an
    alle Schritte durchgereicht, damit api_kosten-Einträge während des Laufs
    darauf verweisen können (Fremdschlüssel-Constraint verlangt eine bereits
    existierende Zeile). Wird am Ende über aktualisiere_lauf_protokoll() mit
    den echten Werten befüllt."""
    try:
        supabase = hole_supabase_client()
        eintrag = (
            supabase.table("lauf_protokoll")
            .insert({"dauer_sekunden": 0, "erfolgreich": False})
            .execute()
            .data[0]
        )
        return eintrag["id"]
    except Exception as e:
        print(f"WARNUNG: Konnte Lauf-Protokoll nicht anlegen: {type(e).__name__}: {e}")
        return None


def aktualisiere_lauf_protokoll(
    lauf_id: str | None, dauer_sekunden: int, erfolgreich: bool, fehler_details: str | None
) -> None:
    if lauf_id is None:
        print("WARNUNG: Kein Lauf-Protokoll-Eintrag vorhanden, Abschluss wird nicht gespeichert.")
        return
    try:
        supabase = hole_supabase_client()
        supabase.table("lauf_protokoll").update(
            {
                "dauer_sekunden": dauer_sekunden,
                "erfolgreich": erfolgreich,
                "fehler_details": fehler_details,
            }
        ).eq("id", lauf_id).execute()
        print("Lauf-Protokoll aktualisiert.")
    except Exception as e:
        print(f"WARNUNG: Konnte Lauf-Protokoll nicht aktualisieren: {type(e).__name__}: {e}")


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


def drucke_zusammenfassung(
    schritte: list[dict],
    gesamt_dauer: float,
    gesamtkosten_usd: float | None = None,
    rhetorik_ergebnis: dict | None = None,
) -> None:
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
    if gesamtkosten_usd is not None:
        print(f"Geschätzte API-Kosten dieses Laufs: ${gesamtkosten_usd:.4f}")

    if rhetorik_ergebnis is not None:
        drucke_rhetorik_block(rhetorik_ergebnis)

    print("#" * 70)


def main() -> None:
    gesamt_start = time.monotonic()
    schritte: list[dict] = []

    lauf_id = starte_lauf_protokoll()

    schritte.append(fuehre_schritt_aus("1/9 RSS-Feeds einlesen", rss_einlesen.main))

    schritte.append(
        fuehre_schritt_aus(
            "2/9 Recherche-Agenten ausführen",
            lambda: recherche_und_redaktion.fuehre_recherche_agenten_aus(lauf_id=lauf_id),
        )
    )

    schritte.append(
        fuehre_schritt_aus(
            "3/9 Redaktion ausführen",
            lambda: recherche_und_redaktion.fuehre_redaktion_aus(lauf_id=lauf_id),
        )
    )

    schritte.append(
        fuehre_schritt_aus(
            "4/9 Akzeptierte Entscheidungen verarbeiten",
            lambda: recherche_und_redaktion.verarbeite_akzeptierte_entscheidungen(lauf_id=lauf_id),
            formatiere_verarbeitung,
        )
    )

    schritte.append(
        fuehre_schritt_aus(
            "5/9 Update-Reaktivierung prüfen",
            lambda: recherche_und_redaktion.pruefe_update_reaktivierung(lauf_id=lauf_id),
            formatiere_update_reaktivierung,
        )
    )

    schritt_manuskript = fuehre_schritt_aus(
        "6/9 Manuskript erzeugen",
        lambda: generiere_episode.erstelle_episode(lauf_id=lauf_id),
        formatiere_episode,
    )
    schritte.append(schritt_manuskript)

    episode = schritt_manuskript["ergebnis"]
    if schritt_manuskript["status"] != "erfolgreich":
        schritt_faktencheck = markiere_uebersprungen("7/9 Faktencheck", "Schritt 6 ist fehlgeschlagen")
    elif episode is None:
        schritt_faktencheck = markiere_uebersprungen(
            "7/9 Faktencheck", "kein Manuskript erzeugt (keine offenen Themen)"
        )
    else:
        schritt_faktencheck = fuehre_schritt_aus(
            "7/9 Faktencheck",
            lambda: generiere_episode.pruefe_manuskript(
                episode["id"], episode["manuskripttext"], episode["verwendete_themen"], lauf_id=lauf_id
            ),
            formatiere_faktencheck,
        )
    schritte.append(schritt_faktencheck)

    faktencheck_ergebnis = schritt_faktencheck["ergebnis"]
    if schritt_manuskript["status"] != "erfolgreich":
        schritte.append(markiere_uebersprungen("8/9 Audio erzeugen", "Schritt 6 ist fehlgeschlagen"))
    elif episode is None:
        schritte.append(markiere_uebersprungen("8/9 Audio erzeugen", "kein Manuskript erzeugt (keine offenen Themen)"))
    elif schritt_faktencheck["status"] != "erfolgreich":
        schritte.append(markiere_uebersprungen("8/9 Audio erzeugen", "Faktencheck fehlgeschlagen"))
    elif faktencheck_ergebnis["widerspruch"] > 0:
        schritte.append(
            markiere_uebersprungen(
                "8/9 Audio erzeugen",
                f'Faktencheck hat {faktencheck_ergebnis["widerspruch"]} Widerspruch/Widersprüche '
                "gefunden - Episode nicht freigegeben",
            )
        )
    else:
        schritte.append(
            fuehre_schritt_aus(
                "8/9 Audio erzeugen",
                lambda: erzeuge_audio_fuer_episode(episode, lauf_id=lauf_id),
                formatiere_audio,
            )
        )

    schritt_rhetorik = fuehre_schritt_aus(
        "9/9 Rhetorik-Prüfung",
        lambda: rhetorik_check.pruefe_rhetorik(lauf_id=lauf_id),
        formatiere_rhetorik,
    )
    schritte.append(schritt_rhetorik)
    rhetorik_ergebnis = schritt_rhetorik["ergebnis"]

    gesamt_dauer = time.monotonic() - gesamt_start

    gesamtkosten_usd = None
    try:
        supabase = hole_supabase_client()
        gesamtkosten_usd = kosten_tracking.hole_kosten_summe(supabase, lauf_id=lauf_id)
        if episode is not None:
            episode_kosten_usd = kosten_tracking.hole_kosten_summe(supabase, episode_id=episode["id"])
            supabase.table("episoden").update({"kosten": episode_kosten_usd}).eq("id", episode["id"]).execute()
    except Exception as e:
        print(f"WARNUNG: Konnte API-Kosten nicht aggregieren: {type(e).__name__}: {e}")

    drucke_zusammenfassung(schritte, gesamt_dauer, gesamtkosten_usd, rhetorik_ergebnis)

    fehlgeschlagene_oder_uebersprungene = [s for s in schritte if s["status"] != "erfolgreich"]
    erfolgreich = not any(s["status"] == "fehlgeschlagen" for s in schritte)
    fehler_details = (
        "\n".join(f"{s['name']}: {s['fehler']}" for s in fehlgeschlagene_oder_uebersprungene)
        if fehlgeschlagene_oder_uebersprungene
        else None
    )

    aktualisiere_lauf_protokoll(lauf_id, round(gesamt_dauer), erfolgreich, fehler_details)


if __name__ == "__main__":
    main()
