"""Erzeugt aus den aktuell offenen Themen ein Podcast-Manuskript und legt eine
neue Episode in "episoden" an.

Die Moderator-Persona (Ton, Zielgruppe, Stil) wird aus "agenten_konfiguration"
geholt (rolle="moderator") und bildet die Hauptgrundlage für den
Gemini-Prompt.

Eine unabhängig aufrufbare Funktion:

1. erstelle_episode(zusatz_anweisung=None)
   Holt die aktive Moderator-Persona sowie alle Themen mit Status "neu" oder
   "in Verfolgung" samt ihren bisherigen Updates aus "themen_updates", lässt
   Gemini daraus im Ton dieser Persona ein Manuskript schreiben, speichert es
   in "episoden" und markiert die verwendeten Themen als "gesendet", damit sie
   nicht in der nächsten Episode erneut auftauchen.
"""
import os
import sys
from datetime import datetime, timezone

import google.generativeai as genai
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

CHAT_MODEL = "gemini-3.6-flash"
OFFENE_STATUS = ("neu", "in Verfolgung")


def hole_supabase_client():
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def hole_chat_model():
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
    return genai.GenerativeModel(CHAT_MODEL)


def hole_moderator_persona(supabase) -> str:
    treffer = (
        supabase.table("agenten_konfiguration")
        .select("fokus_beschreibung")
        .eq("rolle", "moderator")
        .eq("aktiv", True)
        .limit(1)
        .execute()
        .data
    )
    if not treffer:
        raise RuntimeError(
            'Kein aktiver Agent mit rolle="moderator" in agenten_konfiguration gefunden.'
        )
    return treffer[0]["fokus_beschreibung"]


def hole_offene_themen(supabase) -> list[dict]:
    themen = (
        supabase.table("themen")
        .select("id, titel, zusammenfassung, status")
        .in_("status", OFFENE_STATUS)
        .execute()
        .data
    )
    if not themen:
        return []

    thema_ids = [t["id"] for t in themen]
    updates = (
        supabase.table("themen_updates")
        .select("thema_id, was_neu, datum")
        .in_("thema_id", thema_ids)
        .order("datum")
        .execute()
        .data
    )
    updates_nach_thema: dict[str, list[str]] = {}
    for u in updates:
        updates_nach_thema.setdefault(u["thema_id"], []).append(u["was_neu"])

    return [{**t, "updates": updates_nach_thema.get(t["id"], [])} for t in themen]


def baue_themen_block(themen: list[dict]) -> str:
    bloecke = []
    for t in themen:
        block = f'Thema: {t["titel"]}\nStand: {t["zusammenfassung"] or ""}'
        if t["updates"]:
            block += "\nNeue Fakten seitdem:\n" + "\n".join(f"- {u}" for u in t["updates"])
        bloecke.append(block)
    return "\n\n".join(bloecke)


def baue_manuskript_prompt(persona: str, themen_block: str, zusatz_anweisung: str | None) -> str:
    prompt = (
        f"{persona}\n\n"
        "Du schreibst das Manuskript für die nächste Folge deines Podcasts. "
        "Hier sind die Themen für diese Episode:\n\n"
        f"{themen_block}\n\n"
        "Schreibe daraus ein vollständiges Manuskript zum Vorlesen, in deiner oben "
        "beschriebenen Rolle und deinem Stil. Kurze Begrüßung am Anfang, kurzer "
        "Abschluss am Ende.\n\n"
        "Gib NUR den reinen Manuskripttext zurück, ohne Regieanweisungen, "
        "Kapitelüberschriften oder Markdown-Formatierung."
    )
    if zusatz_anweisung:
        prompt += f"\n\n--- Zusätzliche Anweisung für diesen Durchlauf ---\n{zusatz_anweisung}"
    return prompt


def erstelle_manuskript(chat_model, persona: str, themen_block: str, zusatz_anweisung: str | None) -> str:
    prompt = baue_manuskript_prompt(persona, themen_block, zusatz_anweisung)
    antwort = chat_model.generate_content(prompt)
    return antwort.text.strip()


def erstelle_episode(zusatz_anweisung: str | None = None) -> dict | None:
    supabase = hole_supabase_client()
    chat_model = hole_chat_model()

    persona = hole_moderator_persona(supabase)

    themen = hole_offene_themen(supabase)
    if not themen:
        print(f"Keine offenen Themen (Status {OFFENE_STATUS}) gefunden.")
        return None

    print(f"{len(themen)} offene(s) Thema/Themen gefunden:")
    for t in themen:
        print(f'  - "{t["titel"]}" (Status: {t["status"]}, {len(t["updates"])} Update(s))')
    print()

    themen_block = baue_themen_block(themen)
    print("Erzeuge Manuskript...")
    manuskripttext = erstelle_manuskript(chat_model, persona, themen_block, zusatz_anweisung)
    print(f"-> Manuskript erzeugt ({len(manuskripttext)} Zeichen).\n")

    jetzt = datetime.now(timezone.utc).isoformat()
    episode = (
        supabase.table("episoden")
        .insert({"datum": jetzt, "manuskripttext": manuskripttext})
        .execute()
        .data[0]
    )
    print(f'-> Episode gespeichert (id={episode["id"]}).')

    thema_ids = [t["id"] for t in themen]
    supabase.table("themen").update({"status": "gesendet"}).in_("id", thema_ids).execute()
    print(f"-> {len(thema_ids)} Thema/Themen als 'gesendet' markiert.\n")

    return episode


if __name__ == "__main__":
    zusatz_anweisung = sys.argv[1] if len(sys.argv) > 1 else None
    erstelle_episode(zusatz_anweisung)
