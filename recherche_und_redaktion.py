"""Recherche- und Redaktions-Pipeline auf Basis von "agenten_konfiguration".

Drei unabhängig aufrufbare Funktionen:

1. fuehre_recherche_agenten_aus(zusatz_anweisung=None)
   Lässt ALLE aktiven Recherche-Agenten (rolle='recherche') über neue
   Rohnachrichten der letzten 3 Tage laufen und legt Vorschläge in
   "agent_vorschlaege" an.

2. fuehre_einzelnen_agenten_aus(agent_name, zusatz_anweisung=None)
   Wie oben, aber für genau einen Agenten (per Name identifiziert) -
   egal ob Recherche- oder Redaktions-Agent. Praktisch zum gezielten Testen.

3. fuehre_redaktion_aus(zusatz_anweisung=None)
   Lässt den aktiven Redaktions-Agenten (rolle='redaktion') über alle
   offenen Vorschläge aus "agent_vorschlaege" entscheiden und legt
   Entscheidungen in "redaktion_entscheidungen" an.

Voraussetzung: Migration 20260824120000_agenten_konfiguration_rolle.sql
muss angewendet sein (Spalte "rolle" in agenten_konfiguration).
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import google.generativeai as genai
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

CHAT_MODEL = "gemini-3.6-flash"
MAX_ALTER_TAGE = 3


def hole_supabase_client():
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def hole_chat_model():
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
    return genai.GenerativeModel(CHAT_MODEL)


def baue_systemkontext(fokus_beschreibung: str, zusatz_anweisung: str | None) -> str:
    kontext = fokus_beschreibung or ""
    if zusatz_anweisung:
        kontext += (
            "\n\n--- Zusätzliche Anweisung für diesen Durchlauf ---\n"
            f"{zusatz_anweisung}"
        )
    return kontext


def hole_aktive_agenten(supabase, rolle: str | None = None) -> list[dict]:
    query = supabase.table("agenten_konfiguration").select("*").eq("aktiv", True)
    if rolle:
        query = query.eq("rolle", rolle)
    return query.execute().data


def hole_unverarbeitete_rohnachrichten(supabase, agent_id: str) -> list[dict]:
    grenze = (datetime.now(timezone.utc) - timedelta(days=MAX_ALTER_TAGE)).isoformat()

    rohnachrichten = (
        supabase.table("rohnachrichten")
        .select("id, titel, text")
        .gte("abrufzeitpunkt", grenze)
        .execute()
        .data
    )
    if not rohnachrichten:
        return []

    bereits_vorgeschlagen = (
        supabase.table("agent_vorschlaege")
        .select("rohnachricht_id")
        .eq("agent_id", agent_id)
        .execute()
        .data
    )
    bereits_ids = {v["rohnachricht_id"] for v in bereits_vorgeschlagen}

    return [r for r in rohnachrichten if r["id"] not in bereits_ids]


def waehle_relevante_nachrichten(chat_model, systemkontext: str, rohnachrichten: list[dict]) -> list[dict]:
    nachrichten_block = "\n\n".join(
        f'Titel: {r["titel"]}\nText: {r["text"]}' for r in rohnachrichten
    )

    prompt = (
        f"{systemkontext}\n\n"
        "Wähle aus den folgenden Nachrichten die 3-5 relevantesten für deinen Fokus aus, "
        "jeweils mit kurzer Begründung.\n\n"
        f"{nachrichten_block}\n\n"
        "Antworte NUR mit JSON in diesem Format: "
        '[{"rohnachricht_titel": string, "begruendung": string}]'
    )

    antwort = chat_model.generate_content(
        prompt,
        generation_config={"response_mime_type": "application/json"},
    )
    auswahl = json.loads(antwort.text)

    nach_titel = {r["titel"]: r for r in rohnachrichten}
    ergebnis = []
    for eintrag in auswahl:
        titel = eintrag.get("rohnachricht_titel")
        rohnachricht = nach_titel.get(titel)
        if rohnachricht is None:
            print(f'-> Warnung: Gemini-Auswahl "{titel}" passt zu keiner Rohnachricht, wird übersprungen.')
            continue
        ergebnis.append({"rohnachricht": rohnachricht, "begruendung": eintrag.get("begruendung", "")})
    return ergebnis


def fuehre_recherche_fuer_agenten_aus(supabase, chat_model, agent: dict, zusatz_anweisung: str | None) -> int:
    name = agent["name"]
    print(f'Recherche-Agent "{name}": suche neue Rohnachrichten...')

    rohnachrichten = hole_unverarbeitete_rohnachrichten(supabase, agent["id"])
    if not rohnachrichten:
        print(f'-> Keine neuen Rohnachrichten der letzten {MAX_ALTER_TAGE} Tage für "{name}".\n')
        return 0

    systemkontext = baue_systemkontext(agent.get("fokus_beschreibung") or "", zusatz_anweisung)
    auswahl = waehle_relevante_nachrichten(chat_model, systemkontext, rohnachrichten)

    if not auswahl:
        print(f'-> Gemini hat für "{name}" keine relevanten Nachrichten ausgewählt.\n')
        return 0

    jetzt = datetime.now(timezone.utc).isoformat()
    for eintrag in auswahl:
        supabase.table("agent_vorschlaege").insert(
            {
                "agent_id": agent["id"],
                "rohnachricht_id": eintrag["rohnachricht"]["id"],
                "begruendung": eintrag["begruendung"],
                "vorgeschlagen_am": jetzt,
            }
        ).execute()
        print(f'-> Vorschlag gespeichert: "{eintrag["rohnachricht"]["titel"]}"')

    print(f'-> {len(auswahl)} Vorschläge für "{name}" gespeichert.\n')
    return len(auswahl)


def hole_offene_vorschlaege(supabase) -> list[dict]:
    vorschlaege = (
        supabase.table("agent_vorschlaege")
        .select("id, begruendung, agent_id, rohnachricht_id")
        .execute()
        .data
    )
    if not vorschlaege:
        return []

    entschieden = supabase.table("redaktion_entscheidungen").select("vorschlag_id").execute().data
    entschiedene_ids = {e["vorschlag_id"] for e in entschieden}

    offene = [v for v in vorschlaege if v["id"] not in entschiedene_ids]
    if not offene:
        return []

    agent_ids = list({v["agent_id"] for v in offene})
    agenten = (
        supabase.table("agenten_konfiguration").select("id, name").in_("id", agent_ids).execute().data
    )
    agent_namen = {a["id"]: a["name"] for a in agenten}

    rohnachricht_ids = list({v["rohnachricht_id"] for v in offene})
    rohnachrichten = (
        supabase.table("rohnachrichten").select("id, titel, text").in_("id", rohnachricht_ids).execute().data
    )
    rohnachrichten_nach_id = {r["id"]: r for r in rohnachrichten}

    ergebnis = []
    for v in offene:
        rohnachricht = rohnachrichten_nach_id.get(v["rohnachricht_id"])
        ergebnis.append(
            {
                "id": v["id"],
                "begruendung": v["begruendung"],
                "agent_name": agent_namen.get(v["agent_id"], "Unbekannt"),
                "rohnachricht_titel": rohnachricht["titel"] if rohnachricht else "(Rohnachricht gelöscht)",
                "rohnachricht_text": rohnachricht["text"] if rohnachricht else "",
            }
        )
    return ergebnis


def entscheide_ueber_vorschlaege(chat_model, systemkontext: str, vorschlaege: list[dict]) -> list[dict]:
    vorschlaege_block = "\n\n".join(
        f'[{i}] Vorschlag von Recherche-Agent "{v["agent_name"]}":\n'
        f'Titel: {v["rohnachricht_titel"]}\n'
        f'Begründung des Recherche-Agenten: {v["begruendung"]}\n'
        f'Text: {v["rohnachricht_text"]}'
        for i, v in enumerate(vorschlaege)
    )

    prompt = (
        f"{systemkontext}\n\n"
        "Hier sind alle offenen Themen-Vorschläge der Recherche-Agenten für die nächste Episode. "
        "Wähle die 4-6 wichtigsten aus. Gib für JEDEN Vorschlag eine Entscheidung ab "
        "(auch für die nicht ausgewählten), jeweils mit Begründung.\n\n"
        f"{vorschlaege_block}\n\n"
        "Antworte NUR mit JSON in diesem Format: "
        '[{"index": int, "akzeptiert": bool, "begruendung": string}]'
    )

    antwort = chat_model.generate_content(
        prompt,
        generation_config={"response_mime_type": "application/json"},
    )
    entscheidungen = json.loads(antwort.text)

    ergebnis = []
    for eintrag in entscheidungen:
        index = eintrag.get("index")
        if index is None or not (0 <= index < len(vorschlaege)):
            print(f"-> Warnung: Gemini-Entscheidung mit ungültigem Index {index} wird übersprungen.")
            continue
        ergebnis.append(
            {
                "vorschlag_id": vorschlaege[index]["id"],
                "rohnachricht_titel": vorschlaege[index]["rohnachricht_titel"],
                "akzeptiert": bool(eintrag.get("akzeptiert")),
                "begruendung": eintrag.get("begruendung", ""),
            }
        )
    return ergebnis


def fuehre_redaktion_fuer_agenten_aus(supabase, chat_model, agent: dict, zusatz_anweisung: str | None) -> int:
    name = agent["name"]
    print(f'Redaktions-Agent "{name}": hole offene Vorschläge...')

    offene_vorschlaege = hole_offene_vorschlaege(supabase)
    if not offene_vorschlaege:
        print("-> Keine offenen Vorschläge vorhanden.\n")
        return 0

    systemkontext = baue_systemkontext(agent.get("fokus_beschreibung") or "", zusatz_anweisung)
    entscheidungen = entscheide_ueber_vorschlaege(chat_model, systemkontext, offene_vorschlaege)

    if not entscheidungen:
        print("-> Gemini hat keine verwertbaren Entscheidungen geliefert.\n")
        return 0

    jetzt = datetime.now(timezone.utc).isoformat()
    akzeptiert_anzahl = 0
    for entscheidung in entscheidungen:
        supabase.table("redaktion_entscheidungen").insert(
            {
                "vorschlag_id": entscheidung["vorschlag_id"],
                "akzeptiert": entscheidung["akzeptiert"],
                "begruendung": entscheidung["begruendung"],
                "entschieden_am": jetzt,
            }
        ).execute()
        status = "akzeptiert" if entscheidung["akzeptiert"] else "abgelehnt"
        print(f'-> {status}: "{entscheidung["rohnachricht_titel"]}"')
        if entscheidung["akzeptiert"]:
            akzeptiert_anzahl += 1

    print(f"-> {len(entscheidungen)} Entscheidungen gespeichert ({akzeptiert_anzahl} akzeptiert).\n")
    return len(entscheidungen)


def fuehre_recherche_agenten_aus(zusatz_anweisung: str | None = None) -> None:
    """Lässt alle aktiven Recherche-Agenten über neue Rohnachrichten laufen."""
    supabase = hole_supabase_client()
    chat_model = hole_chat_model()

    agenten = hole_aktive_agenten(supabase, rolle="recherche")
    if not agenten:
        print("Keine aktiven Recherche-Agenten gefunden.")
        return

    print(f"{len(agenten)} aktive(r) Recherche-Agent(en) gefunden.\n")

    gesamt = sum(
        fuehre_recherche_fuer_agenten_aus(supabase, chat_model, agent, zusatz_anweisung)
        for agent in agenten
    )
    print(f"Fertig. Insgesamt {gesamt} neue Vorschläge gespeichert.")


def fuehre_einzelnen_agenten_aus(agent_name: str, zusatz_anweisung: str | None = None) -> None:
    """Führt genau einen Agenten (Recherche oder Redaktion) per Name aus."""
    supabase = hole_supabase_client()

    treffer = supabase.table("agenten_konfiguration").select("*").eq("name", agent_name).execute().data
    if not treffer:
        print(f'Fehler: Kein Agent mit dem Namen "{agent_name}" gefunden.')
        return

    agent = treffer[0]
    if not agent.get("aktiv"):
        print(f'Fehler: Agent "{agent_name}" ist nicht aktiv.')
        return

    chat_model = hole_chat_model()

    if agent.get("rolle") == "redaktion":
        fuehre_redaktion_fuer_agenten_aus(supabase, chat_model, agent, zusatz_anweisung)
    else:
        fuehre_recherche_fuer_agenten_aus(supabase, chat_model, agent, zusatz_anweisung)


def fuehre_redaktion_aus(zusatz_anweisung: str | None = None) -> None:
    """Lässt den aktiven Redaktions-Agenten über alle offenen Vorschläge entscheiden."""
    supabase = hole_supabase_client()
    chat_model = hole_chat_model()

    agenten = hole_aktive_agenten(supabase, rolle="redaktion")
    if not agenten:
        print("Kein aktiver Redaktions-Agent gefunden.")
        return
    if len(agenten) > 1:
        print(
            f'Warnung: {len(agenten)} aktive Redaktions-Agenten gefunden, '
            f'nutze den ersten: "{agenten[0]["name"]}".'
        )

    fuehre_redaktion_fuer_agenten_aus(supabase, chat_model, agenten[0], zusatz_anweisung)


if __name__ == "__main__":
    befehl = sys.argv[1] if len(sys.argv) > 1 else "recherche"

    if befehl == "recherche":
        fuehre_recherche_agenten_aus()
    elif befehl == "redaktion":
        fuehre_redaktion_aus()
    elif befehl == "agent":
        if len(sys.argv) < 3:
            print('Nutzung: python recherche_und_redaktion.py agent "<Agent-Name>" ["<Zusatz-Anweisung>"]')
        else:
            fuehre_einzelnen_agenten_aus(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
    else:
        print(f'Unbekannter Befehl: "{befehl}". Nutze "recherche", "redaktion" oder "agent".')
