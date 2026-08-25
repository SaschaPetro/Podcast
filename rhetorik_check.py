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

   WICHTIG: Reine Analyse/Empfehlung - ändert nichts automatisch am
   Manuskript-Prompt oder an bestehenden Episoden.

   Gibt IMMER ein dict zurück (nie None), damit der Aufrufer (morgenlauf.py)
   sowohl im durchgeführten als auch im übersprungenen Fall genug Info für
   eine Zusammenfassung hat:
   - {"status": "durchgefuehrt", "id", "zeitstempel", "episode_ids",
      "gesamteinschaetzung", "konkrete_probleme"}
   - {"status": "uebersprungen", "fehlende_episoden": int}

Voraussetzung: Migration 20260825194458_rhetorik_bewertungen.sql muss
angewendet sein (Tabelle "rhetorik_bewertungen", rolle="rhetorik" in
agenten_konfiguration), sowie ein aktiver Agent mit rolle="rhetorik" in
"agenten_konfiguration".
"""
import json
import os
import sys

import google.generativeai as genai
from dotenv import load_dotenv
from supabase import create_client

import kosten_tracking

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()

CHAT_MODEL = os.environ["GEMINI_MODEL_NAME"]
MINDEST_EPISODEN = 4


def hole_supabase_client():
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def hole_chat_model():
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
    return genai.GenerativeModel(CHAT_MODEL)


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
        f"Hier sind die letzten {len(episoden)} Episoden-Manuskripte in chronologischer "
        f"Reihenfolge (älteste zuerst):\n\n"
        f"{episoden_block}\n\n"
        "Antworte NUR mit JSON in diesem Format: "
        '{"gesamteinschaetzung": string, "konkrete_probleme": '
        '[{"problem": string, "beispiel_zitat": string, "vorschlag": string}]}\n'
        'Die Liste "konkrete_probleme" darf leer sein, wenn die Folgen wirklich gut sind - '
        "nicht jede Prüfung muss Kritikpunkte finden."
    )


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

    return {"status": "durchgefuehrt", **zeile}


if __name__ == "__main__":
    ergebnis = pruefe_rhetorik()
    print(json.dumps(ergebnis, indent=2, ensure_ascii=False, default=str))
