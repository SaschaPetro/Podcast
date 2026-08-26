"""Periodische Rhetorik-Prüfung über mehrere Episoden hinweg.

pruefe_rhetorik(lauf_id=None)
   Prüft alle MINDEST_EPISODEN (4) Episoden, ob die Manuskripte noch
   rhetorisch stark sind - unabhängig vom Faktencheck in
   generiere_episode.pruefe_manuskript(), der nur Fakten prüft, nicht Stil.

   Zählt, wie viele Episoden (mit Manuskripttext) seit dem letzten Eintrag in
   "rhetorik_bewertungen" entstanden sind (oder insgesamt, falls noch kein
   Eintrag existiert). Sind es weniger als MINDEST_EPISODEN, wird die Prüfung
   übersprungen (Konsolen-Hinweis, wie viele Episoden noch fehlen). Sonst
   gehen die MINDEST_EPISODEN neuesten Episoden-Manuskripte (chronologisch
   sortiert) gebündelt an Gemini, zusammen mit der fokus_beschreibung des
   aktiven Agenten mit rolle="rhetorik" aus "agenten_konfiguration". Das
   Ergebnis (Gesamteinschätzung + Liste konkreter Probleme mit Zitat und
   Verbesserungsvorschlag) wird in "rhetorik_bewertungen" gespeichert.

   Findet die Prüfung mindestens ein konkretes Problem, wird das NICHT
   automatisch weiterverarbeitet - es erscheint nur ein deutlicher
   Konsolen-Hinweis (auch in der Lauf-Zusammenfassung von morgenlauf.py),
   passe_manuskript_prompt_an(bewertung_id=...) manuell auszuführen. Diese
   bewusste Entkopplung ist Absicht: ob und wann eine Prompt-Korrektur
   vorgeschlagen wird, entscheidet ihr pro Fund selbst - nicht unbeaufsichtigt
   im Cron-Lauf.

   Gibt IMMER ein dict zurück (nie None), damit der Aufrufer (morgenlauf.py)
   sowohl im durchgeführten als auch im übersprungenen Fall genug Info für
   eine Zusammenfassung hat:
   - {"status": "durchgefuehrt", "id", "zeitstempel", "episode_ids",
      "gesamteinschaetzung", "konkrete_probleme"}
   - {"status": "uebersprungen", "fehlende_episoden": int}

passe_manuskript_prompt_an(bewertung_id, lauf_id=None)
   NUR manuell auszuführen (siehe oben) - nie automatisch aus pruefe_rhetorik().
   Lässt Gemini den aktuell aktiven Manuskript-Prompt (aus
   "manuskript_prompt_versionen", siehe generiere_episode.py) anhand der
   konkrete_probleme der Bewertung `bewertung_id` gezielt korrigieren.
   Speichert das Ergebnis IMMER als neue, INAKTIVE Prompt-Version
   (ist_aktiv=false, erstellt_von="rhetorik_agent", ausloesende_bewertung_id
   = bewertung_id) und druckt den Diff zur aktuell aktiven Version auf die
   Konsole, zum Gegenlesen - aktiviert wird NICHTS automatisch. Fehlen im
   korrigierten Text Pflicht-Platzhalter (generiere_episode.PFLICHT_PLATZHALTER),
   erscheint eine deutliche Warnung im Diff-Ausdruck. Übernommen wird die
   neue Version erst per manuellem Aufruf von
   generiere_episode.aktiviere_prompt_version(<neue_version_nummer>) - derselbe
   Weg dient auch dem Rücksprung auf eine ältere Version.

Voraussetzung: Migration 20260825194458_rhetorik_bewertungen.sql muss
angewendet sein (Tabelle "rhetorik_bewertungen", rolle="rhetorik" in
agenten_konfiguration) sowie 20260826064911_manuskript_prompt_versionen.sql
(Tabelle "manuskript_prompt_versionen"), sowie ein aktiver Agent mit
rolle="rhetorik" in "agenten_konfiguration".
"""
import difflib
import json
import os
import sys

from dotenv import load_dotenv
from supabase import create_client

import generiere_episode
import kosten_tracking
from gemini_client import GeminiModell
import modelle

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()

CHAT_MODEL = modelle.modell_fuer("rhetorik_pruefung")
MINDEST_EPISODEN = 4


def hole_supabase_client():
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def hole_chat_model():
    return GeminiModell(CHAT_MODEL)


def hole_rhetorik_agent(supabase) -> dict:
    treffer = (
        supabase.table("agenten_konfiguration")
        .select("fokus_beschreibung")
        .eq("rolle", "rhetorik")
        .eq("aktiv", True)
        .limit(1)
        .execute()
        .data
    )
    if not treffer:
        raise RuntimeError('Kein aktiver Agent mit rolle="rhetorik" in agenten_konfiguration gefunden.')
    return treffer[0]


def hole_letzte_rhetorik_bewertung(supabase) -> dict | None:
    treffer = (
        supabase.table("rhetorik_bewertungen")
        .select("zeitstempel")
        .order("zeitstempel", desc=True)
        .limit(1)
        .execute()
        .data
    )
    return treffer[0] if treffer else None


def hole_episoden_seit(supabase, seit_iso: str | None) -> list[dict]:
    """Episoden mit Manuskripttext, neueste zuerst - optional nur die, deren
    "datum" nach `seit_iso` liegt."""
    query = (
        supabase.table("episoden")
        .select("id, datum, manuskripttext")
        .not_.is_("manuskripttext", "null")
        .order("datum", desc=True)
    )
    if seit_iso:
        query = query.gt("datum", seit_iso)
    return query.execute().data


def baue_rhetorik_prompt(fokus_beschreibung: str, episoden: list[dict]) -> str:
    episoden_block = "\n\n".join(
        f'=== Episode vom {str(e["datum"])[:10]} (id: {e["id"]}) ===\n{e["manuskripttext"]}' for e in episoden
    )
    return (
        f"{fokus_beschreibung}\n\n"
        "Bewerte besonders streng, ob die Folgen weiterhin als Nachrichtensendung "
        "erkennbar sind. Der Nachrichtenkern jedes Themas muss innerhalb der ersten "
        "zwei Sätze stehen. Markiere als konkretes Problem: erfundene Figuren oder "
        "Dialoge, ausgedehnte Alltagsszenen, künstliche Spannungskurven, atmosphärische "
        "Einstiege vor der eigentlichen Meldung und Beispiele, die länger sind als die "
        "Nachricht selbst. Storytelling ist nur dann gelungen, wenn es erst nach dem "
        "Nachrichtenkern eingesetzt wird, einen komplexen Zusammenhang kurz verständlich "
        "macht und sofort zu belegten Fakten zurückführt. Beanstande Storytelling auch "
        "dann, wenn ein Abschnitt ohne dieses Stilmittel genauso verständlich wäre. Prüfe "
        "außerdem, ob die Folge ungefähr 1.300 bis 1.450 Wörter erreicht.\n\n"
        f"Hier sind die letzten {len(episoden)} Episoden-Manuskripte in chronologischer "
        f"Reihenfolge (älteste zuerst):\n\n"
        f"{episoden_block}\n\n"
        "Antworte NUR mit JSON in diesem Format: "
        '{"gesamteinschaetzung": string, "konkrete_probleme": '
        '[{"problem": string, "beispiel_zitat": string, "vorschlag": string}]}\n'
        'Die Liste "konkrete_probleme" darf leer sein, wenn die Folgen wirklich gut sind - '
        "nicht jede Prüfung muss Kritikpunkte finden."
    )


def baue_prompt_revision_prompt(aktueller_prompt_text: str, konkrete_probleme: list[dict]) -> str:
    probleme_block = "\n\n".join(
        f'Problem: {p.get("problem")}\n'
        f'Beispiel-Zitat: {p.get("beispiel_zitat")}\n'
        f'Vorschlag: {p.get("vorschlag")}'
        for p in konkrete_probleme
    )
    platzhalter_liste = ", ".join(generiere_episode.PFLICHT_PLATZHALTER)
    return (
        "Hier ist der vollständige, aktuell verwendete Manuskript-Prompt:\n\n"
        f"{aktueller_prompt_text}\n\n"
        "Hier sind konkrete Kritikpunkte eines Rhetorik-Checks:\n\n"
        f"{probleme_block}\n\n"
        "Gib den VOLLSTÄNDIGEN, korrigierten Prompt-Text zurück, der NUR die genannten "
        "Probleme behebt. Ändere sonst NICHTS - erhalte alle Platzhalter "
        f"({platzhalter_liste}) exakt und unverändert (Schreibweise, Groß-/Kleinschreibung, "
        "geschweifte Klammern), entferne keine bestehenden Regeln, die nicht kritisiert "
        "wurden, und füge keine neuen Beispiel-Formulierungen hinzu, die selbst wieder zu "
        "Wiederholungsmustern werden könnten - beschreibe Vielfalt abstrakt statt mit "
        "konkreten Mustersätzen. Der bestehende Prompt ist durchgehend in der Du-Form "
        "geschrieben (nicht Sie). Behalte diese Anrede konsequent in deiner Korrektur "
        "bei, wechsle nicht ins Sie. Prüfe vor der Ausgabe, ob deine Änderungen an "
        "verschiedenen Stellen inhaltlich überlappen oder sich wiederholen - "
        "konsolidiere redundante Anweisungen zu einer einzigen, klaren Stelle statt sie "
        "zu duplizieren.\n\n"
        "Antworte NUR mit JSON: "
        '{"korrigierter_prompt_text": string, "kurze_begruendung": string}'
    )


def drucke_prompt_diff(alter_text: str, neuer_text: str) -> None:
    diff = difflib.unified_diff(
        alter_text.splitlines(keepends=True),
        neuer_text.splitlines(keepends=True),
        fromfile="aktuell aktive Version",
        tofile="Vorschlag",
    )
    diff_text = "".join(diff)
    print(f"\n{'-' * 70}")
    print("DIFF (zum Gegenlesen, NICHT automatisch aktiv):")
    print("-" * 70)
    print(diff_text if diff_text else "(keine Änderungen)")
    print("-" * 70)


def passe_manuskript_prompt_an(bewertung_id: str, lauf_id: str | None = None) -> dict | None:
    """NUR manuell auszuführen - lässt Gemini den aktuell aktiven Manuskript-Prompt
    anhand der konkrete_probleme der Rhetorik-Bewertung `bewertung_id` gezielt
    korrigieren. Speichert das Ergebnis IMMER als neue, INAKTIVE Prompt-Version und
    zeigt den Diff zur aktuellen Version zum Gegenlesen an - aktiviert NICHTS
    automatisch. Übernommen wird die neue Version erst per
    generiere_episode.aktiviere_prompt_version(<neue_version_nummer>)."""
    supabase = hole_supabase_client()
    chat_model = hole_chat_model()

    treffer = (
        supabase.table("rhetorik_bewertungen").select("*").eq("id", bewertung_id).limit(1).execute().data
    )
    if not treffer:
        raise ValueError(f"Keine rhetorik_bewertungen-Zeile mit id={bewertung_id} gefunden.")
    bewertung = treffer[0]

    if not bewertung["konkrete_probleme"]:
        print(f"Bewertung {bewertung_id} enthält keine konkreten Probleme - nichts zu korrigieren.")
        return None

    aktuelle_version = generiere_episode.hole_aktive_prompt_version(supabase)
    prompt = baue_prompt_revision_prompt(aktuelle_version["prompt_text"], bewertung["konkrete_probleme"])

    antwort = chat_model.generate_content(
        prompt, generation_config={"response_mime_type": "application/json"}
    )

    kosten_tracking.logge_api_kosten(
        supabase,
        dienst="gemini",
        modell=CHAT_MODEL,
        schritt="prompt_revision",
        einheit_typ="tokens",
        menge_input=antwort.usage_metadata.prompt_token_count,
        menge_output=antwort.usage_metadata.candidates_token_count,
        lauf_id=lauf_id,
    )

    ergebnis = json.loads(antwort.text)
    neuer_text = ergebnis.get("korrigierter_prompt_text")
    begruendung = ergebnis.get("kurze_begruendung") or "Vorschlag des Rhetorik-Agenten."

    if not neuer_text or not neuer_text.strip():
        print("WARNUNG: Gemini hat keinen korrigierten Prompt-Text zurückgegeben.")
        return None

    fehlende_platzhalter = [p for p in generiere_episode.PFLICHT_PLATZHALTER if p not in neuer_text]

    # Höchste bisherige version_nummer + 1 - NICHT aktuelle_version + 1, sonst kollidiert
    # das mit einer bereits existierenden, aber inaktiven (z.B. per Revert übersprungenen
    # oder nie aktivierten) Versionsnummer.
    hoechste = (
        supabase.table("manuskript_prompt_versionen")
        .select("version_nummer")
        .order("version_nummer", desc=True)
        .limit(1)
        .execute()
        .data
    )
    neue_version_nummer = (hoechste[0]["version_nummer"] if hoechste else 0) + 1

    neue_zeile = (
        supabase.table("manuskript_prompt_versionen")
        .insert(
            {
                "version_nummer": neue_version_nummer,
                "prompt_text": neuer_text,
                "ist_aktiv": False,
                "erstellt_von": "rhetorik_agent",
                "begruendung": begruendung,
                "ausloesende_bewertung_id": bewertung_id,
            }
        )
        .execute()
        .data[0]
    )

    print(f'-> Vorschlag gespeichert als Prompt-Version {neue_version_nummer} (NICHT aktiv): "{begruendung}"')
    if fehlende_platzhalter:
        print(
            f"WARNUNG: Diesem Vorschlag fehlen Platzhalter {fehlende_platzhalter} - "
            "vor dem Aktivieren unbedingt beheben oder verwerfen!"
        )

    drucke_prompt_diff(aktuelle_version["prompt_text"], neuer_text)

    print(
        f"\nZum Übernehmen: from generiere_episode import aktiviere_prompt_version; "
        f"aktiviere_prompt_version({neue_version_nummer})"
    )

    return {
        "version_nummer": neue_version_nummer,
        "id": neue_zeile["id"],
        "fehlende_platzhalter": fehlende_platzhalter,
        "begruendung": begruendung,
    }


def pruefe_rhetorik(lauf_id: str | None = None) -> dict:
    supabase = hole_supabase_client()

    letzte_pruefung = hole_letzte_rhetorik_bewertung(supabase)
    seit_iso = letzte_pruefung["zeitstempel"] if letzte_pruefung else None

    neue_episoden = hole_episoden_seit(supabase, seit_iso)
    if len(neue_episoden) < MINDEST_EPISODEN:
        fehlend = MINDEST_EPISODEN - len(neue_episoden)
        print(
            f"Noch {fehlend} Episode(n) bis zur nächsten Rhetorik-Prüfung "
            f"({len(neue_episoden)}/{MINDEST_EPISODEN} seit letzter Prüfung)."
        )
        return {"status": "uebersprungen", "fehlende_episoden": fehlend}

    zu_pruefende = list(reversed(neue_episoden[:MINDEST_EPISODEN]))
    print(f"{len(zu_pruefende)} Episode(n) für Rhetorik-Prüfung ausgewählt:")
    for e in zu_pruefende:
        print(f'  - {str(e["datum"])[:10]} (id: {e["id"]})')

    agent = hole_rhetorik_agent(supabase)
    chat_model = hole_chat_model()
    prompt = baue_rhetorik_prompt(agent["fokus_beschreibung"], zu_pruefende)

    antwort = chat_model.generate_content(
        prompt, generation_config={"response_mime_type": "application/json"}
    )

    kosten_tracking.logge_api_kosten(
        supabase,
        dienst="gemini",
        modell=CHAT_MODEL,
        schritt="rhetorik_pruefung",
        einheit_typ="tokens",
        menge_input=antwort.usage_metadata.prompt_token_count,
        menge_output=antwort.usage_metadata.candidates_token_count,
        lauf_id=lauf_id,
    )

    ergebnis = json.loads(antwort.text)

    zeile = (
        supabase.table("rhetorik_bewertungen")
        .insert(
            {
                "episode_ids": [e["id"] for e in zu_pruefende],
                "gesamteinschaetzung": ergebnis.get("gesamteinschaetzung"),
                "konkrete_probleme": ergebnis.get("konkrete_probleme") or [],
            }
        )
        .execute()
        .data[0]
    )

    print(
        f"-> Rhetorik-Prüfung über {len(zu_pruefende)} Episode(n) abgeschlossen: "
        f"{len(zeile['konkrete_probleme'])} konkrete(s) Problem(e) gefunden.\n"
    )

    if zeile["konkrete_probleme"]:
        print(
            f'Hinweis: Rhetorik-Kritik gefunden - führe manuell '
            f'passe_manuskript_prompt_an(bewertung_id="{zeile["id"]}") aus, um eine '
            "Korrektur vorzuschlagen (Diff wird zum Gegenlesen angezeigt, bevor sie "
            "aktiv wird).\n"
        )

    return {"status": "durchgefuehrt", **zeile}


if __name__ == "__main__":
    ergebnis = pruefe_rhetorik()
    print(json.dumps(ergebnis, indent=2, ensure_ascii=False, default=str))
