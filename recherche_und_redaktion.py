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
   Entscheidungen in "redaktion_entscheidungen" an. Prüft danach zusätzlich
   alle neuen Einträge aus "themen_updates", deren zugehöriges Thema bereits
   den Status "gesendet" hat: Gemini entscheidet, ob das Update wichtig genug
   ist, um das Thema erneut aufzugreifen. Falls ja, wird der Status des
   Themas zurück auf "in Verfolgung" gesetzt und die Entscheidung in
   "redaktion_update_entscheidungen" dokumentiert.

4. verarbeite_akzeptierte_entscheidungen()
   Holt alle akzeptierten, noch nicht verknüpften Entscheidungen aus
   "redaktion_entscheidungen" (thema_id IS NULL), lässt die Rohnachricht
   über die bestehende Logik aus verarbeite_rohnachricht.py einem Thema
   zuordnen (neues Thema, Update zu bestehendem Thema, oder Duplikat) und
   trägt die entstandene/gefundene thema_id zur Dokumentation zurück in
   "redaktion_entscheidungen" ein.

Voraussetzung: Migration 20260824120000_agenten_konfiguration_rolle.sql
muss angewendet sein (Spalte "rolle" in agenten_konfiguration), sowie
Migration 20260824160000_redaktion_update_entscheidungen.sql (Tabelle
"redaktion_update_entscheidungen").
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import google.generativeai as genai
from dotenv import load_dotenv
from supabase import create_client

import verarbeite_rohnachricht

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()

CHAT_MODEL = os.environ["GEMINI_MODEL_NAME"]
MAX_ALTER_TAGE = 3
MAX_ZURUECKSTELLUNG_TAGE = 3
GUELTIGE_STATUS = {"akzeptiert", "abgelehnt", "zurueckgestellt"}


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

    entscheidungen = (
        supabase.table("redaktion_entscheidungen")
        .select("id, vorschlag_id, status, erste_zurueckstellung_am")
        .execute()
        .data
    )
    entscheidung_nach_vorschlag = {e["vorschlag_id"]: e for e in entscheidungen}

    jetzt = datetime.now(timezone.utc)
    offene = []
    for v in vorschlaege:
        bisherige = entscheidung_nach_vorschlag.get(v["id"])
        if bisherige is None:
            offene.append(
                {
                    **v,
                    "entscheidung_id": None,
                    "zurueckgestellt_bisher": False,
                    "erste_zurueckstellung_am": None,
                    "frist_abgelaufen": False,
                }
            )
        elif bisherige["status"] == "zurueckgestellt":
            erste_am = bisherige.get("erste_zurueckstellung_am")
            frist_abgelaufen = False
            if erste_am:
                seit = jetzt - datetime.fromisoformat(erste_am.replace("Z", "+00:00"))
                frist_abgelaufen = seit > timedelta(days=MAX_ZURUECKSTELLUNG_TAGE)
            offene.append(
                {
                    **v,
                    "entscheidung_id": bisherige["id"],
                    "zurueckgestellt_bisher": True,
                    "erste_zurueckstellung_am": erste_am,
                    "frist_abgelaufen": frist_abgelaufen,
                }
            )
        # status akzeptiert/abgelehnt -> endgueltig entschieden, bleibt draussen

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
                "entscheidung_id": v["entscheidung_id"],
                "zurueckgestellt_bisher": v["zurueckgestellt_bisher"],
                "erste_zurueckstellung_am": v["erste_zurueckstellung_am"],
                "frist_abgelaufen": v["frist_abgelaufen"],
            }
        )
    return ergebnis


def baue_vorschlag_block(i: int, v: dict) -> str:
    hinweise = []
    if v["zurueckgestellt_bisher"]:
        hinweise.append(
            "HINWEIS: Dieser Vorschlag wurde bereits zurückgestellt, prüfe besonders "
            "sorgfältig, ob er jetzt aufgenommen werden sollte."
        )
        if v["frist_abgelaufen"]:
            hinweise.append(
                f"WICHTIG: Seit der ersten Zurückstellung sind mehr als "
                f"{MAX_ZURUECKSTELLUNG_TAGE} Tage vergangen. Dieser Vorschlag darf NICHT "
                "nochmal zurückgestellt werden - entscheide dich für 'akzeptiert' oder 'abgelehnt'."
            )
    hinweis_text = ("\n" + "\n".join(hinweise)) if hinweise else ""
    return (
        f'[{i}] Vorschlag von Recherche-Agent "{v["agent_name"]}":\n'
        f'Titel: {v["rohnachricht_titel"]}\n'
        f'Begründung des Recherche-Agenten: {v["begruendung"]}\n'
        f'Text: {v["rohnachricht_text"]}{hinweis_text}'
    )


def entscheide_ueber_vorschlaege(chat_model, systemkontext: str, vorschlaege: list[dict]) -> list[dict]:
    vorschlaege_block = "\n\n".join(baue_vorschlag_block(i, v) for i, v in enumerate(vorschlaege))

    prompt = (
        f"{systemkontext}\n\n"
        "Hier sind alle offenen Themen-Vorschläge der Recherche-Agenten für die nächste Episode. "
        "Wähle die 4-6 wichtigsten aus. Gib für JEDEN Vorschlag eine Entscheidung ab "
        "(auch für die nicht ausgewählten), jeweils mit Begründung.\n\n"
        "Für jeden Vorschlag gibt es drei mögliche Status:\n"
        "- 'akzeptiert': kommt in die Themen-Pipeline\n"
        "- 'zurueckgestellt': gut und relevant, aber heute kein Platz - wird morgen erneut geprüft\n"
        "- 'abgelehnt': nicht relevant genug, endgültig raus\n\n"
        f"{vorschlaege_block}\n\n"
        "Antworte NUR mit JSON in diesem Format: "
        '[{"index": int, "status": "akzeptiert" | "abgelehnt" | "zurueckgestellt", "begruendung": string}]'
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
        vorschlag = vorschlaege[index]
        status = eintrag.get("status")
        if status not in GUELTIGE_STATUS:
            print(
                f'-> Warnung: ungültiger Status "{status}" für "{vorschlag["rohnachricht_titel"]}", '
                "wird übersprungen."
            )
            continue
        if status == "zurueckgestellt" and vorschlag["frist_abgelaufen"]:
            print(
                f'-> Warnung: "{vorschlag["rohnachricht_titel"]}" ist seit über '
                f'{MAX_ZURUECKSTELLUNG_TAGE} Tagen zurückgestellt, erzwinge "abgelehnt".'
            )
            status = "abgelehnt"
        ergebnis.append(
            {
                "vorschlag_id": vorschlag["id"],
                "entscheidung_id": vorschlag["entscheidung_id"],
                "rohnachricht_titel": vorschlag["rohnachricht_titel"],
                "status": status,
                "begruendung": eintrag.get("begruendung", ""),
                "erste_zurueckstellung_am_bisher": vorschlag["erste_zurueckstellung_am"],
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
    zaehler = {"akzeptiert": 0, "abgelehnt": 0, "zurueckgestellt": 0}
    for entscheidung in entscheidungen:
        status = entscheidung["status"]
        daten = {
            "vorschlag_id": entscheidung["vorschlag_id"],
            "status": status,
            "akzeptiert": status == "akzeptiert",
            "begruendung": entscheidung["begruendung"],
            "entschieden_am": jetzt,
        }
        if status == "zurueckgestellt":
            daten["erste_zurueckstellung_am"] = entscheidung["erste_zurueckstellung_am_bisher"] or jetzt

        if entscheidung["entscheidung_id"]:
            supabase.table("redaktion_entscheidungen").update(daten).eq(
                "id", entscheidung["entscheidung_id"]
            ).execute()
        else:
            supabase.table("redaktion_entscheidungen").insert(daten).execute()

        print(f'-> {status}: "{entscheidung["rohnachricht_titel"]}"')
        zaehler[status] += 1

    print(
        f"-> {len(entscheidungen)} Entscheidungen gespeichert "
        f"({zaehler['akzeptiert']} akzeptiert, {zaehler['zurueckgestellt']} zurückgestellt, "
        f"{zaehler['abgelehnt']} abgelehnt).\n"
    )
    return len(entscheidungen)


def hole_offene_updates(supabase) -> list[dict]:
    updates = (
        supabase.table("themen_updates").select("id, thema_id, was_neu, datum").execute().data
    )
    if not updates:
        return []

    entscheidungen = (
        supabase.table("redaktion_update_entscheidungen").select("update_id").execute().data
    )
    bereits_entschieden = {e["update_id"] for e in entscheidungen}

    offene = [u for u in updates if u["id"] not in bereits_entschieden]
    if not offene:
        return []

    thema_ids = list({u["thema_id"] for u in offene})
    themen = (
        supabase.table("themen")
        .select("id, titel, zusammenfassung, status")
        .in_("id", thema_ids)
        .execute()
        .data
    )
    themen_nach_id = {t["id"]: t for t in themen}

    ergebnis = []
    for u in offene:
        thema = themen_nach_id.get(u["thema_id"])
        if thema is None or thema["status"] != "gesendet":
            continue
        ergebnis.append(
            {
                "id": u["id"],
                "thema_id": u["thema_id"],
                "thema_titel": thema["titel"],
                "thema_zusammenfassung": thema.get("zusammenfassung") or "",
                "was_neu": u["was_neu"],
            }
        )
    return ergebnis


def baue_update_block(i: int, u: dict) -> str:
    return (
        f'[{i}] Bereits gesendetes Thema: {u["thema_titel"]}\n'
        f'Bisheriger Stand: {u["thema_zusammenfassung"]}\n'
        f'Neues Update: {u["was_neu"]}'
    )


def entscheide_ueber_updates(chat_model, systemkontext: str, updates: list[dict]) -> list[dict]:
    updates_block = "\n\n".join(baue_update_block(i, u) for i, u in enumerate(updates))

    prompt = (
        f"{systemkontext}\n\n"
        "Die folgenden Themen wurden bereits in einer Episode gesendet, es gibt aber "
        "seitdem ein neues Update dazu. Entscheide für JEDES Update, ob es wichtig genug "
        'ist, um das Thema erneut aufzugreifen (z.B. "der Fall wurde jetzt final '
        'entschieden" ist wichtig, "kleine Verzögerung um zwei Tage" eher nicht). Gib für '
        "JEDES Update eine Entscheidung ab (auch für die nicht wieder aufgenommenen), "
        "jeweils mit Begründung.\n\n"
        f"{updates_block}\n\n"
        "Antworte NUR mit JSON in diesem Format: "
        '[{"index": int, "wieder_aufnehmen": bool, "begruendung": string}]'
    )

    antwort = chat_model.generate_content(
        prompt,
        generation_config={"response_mime_type": "application/json"},
    )
    entscheidungen = json.loads(antwort.text)

    ergebnis = []
    for eintrag in entscheidungen:
        index = eintrag.get("index")
        if index is None or not (0 <= index < len(updates)):
            print(f"-> Warnung: Gemini-Entscheidung mit ungültigem Index {index} wird übersprungen.")
            continue
        update = updates[index]
        ergebnis.append(
            {
                "update_id": update["id"],
                "thema_id": update["thema_id"],
                "thema_titel": update["thema_titel"],
                "wieder_aufgenommen": bool(eintrag.get("wieder_aufnehmen")),
                "begruendung": eintrag.get("begruendung", ""),
            }
        )
    return ergebnis


def pruefe_updates_zu_gesendeten_themen(
    supabase, chat_model, agent: dict, zusatz_anweisung: str | None
) -> int:
    print("Prüfe Updates zu bereits gesendeten Themen...")

    offene_updates = hole_offene_updates(supabase)
    if not offene_updates:
        print("-> Keine neuen Updates zu gesendeten Themen.\n")
        return 0

    systemkontext = baue_systemkontext(agent.get("fokus_beschreibung") or "", zusatz_anweisung)
    entscheidungen = entscheide_ueber_updates(chat_model, systemkontext, offene_updates)

    if not entscheidungen:
        print("-> Gemini hat keine verwertbaren Entscheidungen geliefert.\n")
        return 0

    jetzt = datetime.now(timezone.utc).isoformat()
    wieder_aufgenommen_anzahl = 0
    for entscheidung in entscheidungen:
        supabase.table("redaktion_update_entscheidungen").insert(
            {
                "update_id": entscheidung["update_id"],
                "thema_id": entscheidung["thema_id"],
                "wieder_aufgenommen": entscheidung["wieder_aufgenommen"],
                "begruendung": entscheidung["begruendung"],
                "entschieden_am": jetzt,
            }
        ).execute()

        if entscheidung["wieder_aufgenommen"]:
            supabase.table("themen").update({"status": "in Verfolgung"}).eq(
                "id", entscheidung["thema_id"]
            ).execute()
            wieder_aufgenommen_anzahl += 1
            print(f'-> wieder aufgenommen: "{entscheidung["thema_titel"]}"')
        else:
            print(f'-> nicht wieder aufgenommen: "{entscheidung["thema_titel"]}"')

    print(
        f"-> {len(entscheidungen)} Update-Entscheidung(en) gespeichert "
        f"({wieder_aufgenommen_anzahl} Thema/Themen wieder aufgenommen).\n"
    )
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

    agent = agenten[0]
    fuehre_redaktion_fuer_agenten_aus(supabase, chat_model, agent, zusatz_anweisung)
    pruefe_updates_zu_gesendeten_themen(supabase, chat_model, agent, zusatz_anweisung)


def hole_akzeptierte_offene_entscheidungen(supabase) -> list[dict]:
    entscheidungen = (
        supabase.table("redaktion_entscheidungen")
        .select("id, vorschlag_id, begruendung")
        .eq("akzeptiert", True)
        .is_("thema_id", "null")
        .execute()
        .data
    )
    if not entscheidungen:
        return []

    vorschlag_ids = list({e["vorschlag_id"] for e in entscheidungen})
    vorschlaege = (
        supabase.table("agent_vorschlaege")
        .select("id, rohnachricht_id")
        .in_("id", vorschlag_ids)
        .execute()
        .data
    )
    rohnachricht_id_nach_vorschlag = {v["id"]: v["rohnachricht_id"] for v in vorschlaege}

    rohnachricht_ids = [rid for rid in set(rohnachricht_id_nach_vorschlag.values()) if rid]
    rohnachrichten = (
        supabase.table("rohnachrichten").select("id, titel, text").in_("id", rohnachricht_ids).execute().data
        if rohnachricht_ids
        else []
    )
    rohnachrichten_nach_id = {r["id"]: r for r in rohnachrichten}

    ergebnis = []
    for e in entscheidungen:
        rohnachricht_id = rohnachricht_id_nach_vorschlag.get(e["vorschlag_id"])
        rohnachricht = rohnachrichten_nach_id.get(rohnachricht_id) if rohnachricht_id else None
        if rohnachricht is None:
            print(f'-> Warnung: Zu Entscheidung {e["id"]} keine Rohnachricht gefunden, wird übersprungen.')
            continue
        ergebnis.append(
            {
                "entscheidung_id": e["id"],
                "rohnachricht_titel": rohnachricht["titel"] or "",
                "rohnachricht_text": rohnachricht["text"] or "",
            }
        )
    return ergebnis


def verarbeite_akzeptierte_entscheidungen() -> list[dict]:
    """Ordnet akzeptierte, noch nicht verknüpfte Entscheidungen einem Thema zu.

    Holt alle Entscheidungen mit akzeptiert=true und thema_id IS NULL, lässt
    Titel+Text der zugehörigen Rohnachricht über die bestehende Logik aus
    verarbeite_rohnachricht.py verarbeiten (Embedding, Ähnlichkeitssuche,
    neues Thema oder Update) und trägt die entstandene/gefundene thema_id
    zurück in redaktion_entscheidungen ein.
    """
    supabase = hole_supabase_client()

    offene = hole_akzeptierte_offene_entscheidungen(supabase)
    if not offene:
        print("Keine akzeptierten, noch nicht verknüpften Entscheidungen gefunden.")
        return []

    print(f"{len(offene)} akzeptierte Entscheidung(en) ohne Thema-Verknüpfung gefunden.\n")

    ergebnisse = []
    for eintrag in offene:
        text = f'{eintrag["rohnachricht_titel"]}\n{eintrag["rohnachricht_text"]}'.strip()

        verarbeitung = verarbeite_rohnachricht.verarbeite_text(text)

        supabase.table("redaktion_entscheidungen").update({"thema_id": verarbeitung["thema_id"]}).eq(
            "id", eintrag["entscheidung_id"]
        ).execute()

        ergebnisse.append(
            {
                "rohnachricht_titel": eintrag["rohnachricht_titel"],
                "art": verarbeitung["art"],
                "thema_titel": verarbeitung["titel"],
                "thema_id": verarbeitung["thema_id"],
            }
        )
        print(f'-> thema_id in redaktion_entscheidungen eingetragen ({verarbeitung["art"]}).\n')

    neu = [r for r in ergebnisse if r["art"] == "neu"]
    updates = [r for r in ergebnisse if r["art"] == "update"]
    duplikate = [r for r in ergebnisse if r["art"] == "duplikat"]

    print(
        f"Fertig. {len(neu)} neue Themen, {len(updates)} Updates zu bestehenden Themen, "
        f"{len(duplikate)} Duplikate.\n"
    )
    if neu:
        print("Neue Themen:")
        for r in neu:
            print(f'  - "{r["thema_titel"]}" (aus: "{r["rohnachricht_titel"]}")')
    if updates:
        print("Updates zu bestehenden Themen:")
        for r in updates:
            print(f'  - "{r["thema_titel"]}" (aus: "{r["rohnachricht_titel"]}")')
    if duplikate:
        print("Duplikate (kein neuer Fakt, nur verknüpft):")
        for r in duplikate:
            print(f'  - "{r["thema_titel"]}" (aus: "{r["rohnachricht_titel"]}")')

    return ergebnisse


if __name__ == "__main__":
    befehl = sys.argv[1] if len(sys.argv) > 1 else "recherche"

    if befehl == "recherche":
        fuehre_recherche_agenten_aus()
    elif befehl == "redaktion":
        fuehre_redaktion_aus()
    elif befehl == "verarbeite":
        verarbeite_akzeptierte_entscheidungen()
    elif befehl == "agent":
        if len(sys.argv) < 3:
            print('Nutzung: python recherche_und_redaktion.py agent "<Agent-Name>" ["<Zusatz-Anweisung>"]')
        else:
            fuehre_einzelnen_agenten_aus(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
    else:
        print(f'Unbekannter Befehl: "{befehl}". Nutze "recherche", "redaktion", "verarbeite" oder "agent".')
