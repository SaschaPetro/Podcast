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

4. pruefe_update_reaktivierung(zusatz_anweisung=None)
   Prüft mit demselben aktiven Redaktions-Agenten alle neuen Einträge aus
   "themen_updates", deren zugehöriges Thema bereits den Status "gesendet"
   hat: Gemini entscheidet, ob das Update wichtig genug ist, um das Thema
   erneut aufzugreifen. Falls ja, wird der Status des Themas zurück auf
   "in Verfolgung" gesetzt und die Entscheidung in
   "redaktion_update_entscheidungen" dokumentiert.

5. verarbeite_akzeptierte_entscheidungen()
   Holt alle akzeptierten, noch nicht verknüpften Entscheidungen aus
   "redaktion_entscheidungen" (thema_id IS NULL), lässt die Rohnachricht
   über die bestehende Logik aus verarbeite_rohnachricht.py einem Thema
   zuordnen (neues Thema, Update zu bestehendem Thema, oder Duplikat) und
   trägt die entstandene/gefundene thema_id zur Dokumentation zurück in
   "redaktion_entscheidungen" ein.

   Wird dabei ein NEUES Thema angelegt (nicht bei Update/Duplikat, um
   Kosten zu sparen), läuft zusätzlich eine Zweite-Quelle-Verifikation
   (Ausschreibungs-Kriterium 5, siehe pruefe_zweite_quelle()): gezielte
   Tavily-Suche mit dem Themen-Titel als Anfrage, bei leerem Ergebnis oder
   Fehler Exa als Fallback. Die Top-Treffer gehen zusammen mit dem
   Original-Rohnachrichtentext an Gemini, das Ergebnis (bestätigt/nicht
   bestätigt + Quelle + Einschätzung) wird direkt an der Themen-Zeile
   gespeichert. Liefert weder Tavily noch Exa Treffer, bleibt das Feld
   NULL ("nicht geprüft") und es gibt nur eine Konsolen-Notiz - kein
   Fehler, und ein Fehlschlag hier blockiert nie die Themen-Zuordnung
   selbst. Voraussetzung: Migration
   20260825140000_zweite_quelle_verifikation.sql muss angewendet sein.

6. fuehre_notfall_auffuellung_aus(zusatz_anweisung=None)
   Sicherheitsnetz gegen zu kurze Episoden (siehe morgenlauf.py: bricht ab,
   wenn das Manuskript unter der Mindestwortzahl bleibt - das passiert vor
   allem, wenn schlicht zu wenige offene Themen vorliegen). Läuft NACH der
   normalen Redaktion (3.) und VOR verarbeite_akzeptierte_entscheidungen()
   (5.): Zählt aktuell offene Themen + bereits akzeptierte, aber noch nicht
   verarbeitete Entscheidungen. Bleibt die Summe unter MIN_THEMEN_FUER_EPISODE,
   werden zunächst zurückgestellte, danach - falls immer noch zu wenig -
   abgelehnte Vorschläge erneut von Gemini geprüft: mit gelockertem
   Relevanz-Maßstab, aber weiterhin ohne Duplikate, Gerüchte oder faktisch
   unbelegte Meldungen. Geeignete Kandidaten werden auf Status "akzeptiert"
   gesetzt, damit Schritt 5 daraus echte Themen macht. Ist bereits genug
   Material vorhanden, tut die Funktion nichts.

Voraussetzung: Migration 20260824120000_agenten_konfiguration_rolle.sql
muss angewendet sein (Spalte "rolle" in agenten_konfiguration), sowie
Migration 20260824160000_redaktion_update_entscheidungen.sql (Tabelle
"redaktion_update_entscheidungen").
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from exa_py import Exa
from supabase import create_client
from tavily import TavilyClient

import kosten_tracking
from gemini_client import GeminiModell
import modelle
import verarbeite_rohnachricht

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()

CHAT_MODEL = modelle.modell_fuer("recherche_auswahl")
MAX_ALTER_TAGE = 3
MAX_ZURUECKSTELLUNG_TAGE = 3
GUELTIGE_STATUS = {"akzeptiert", "abgelehnt", "zurueckgestellt"}
ZWEITE_QUELLE_MAX_TREFFER = 5
# Deckt sich mit der "mindestens 5"-Vorgabe in den Recherche-/Redaktions-
# Prompts weiter unten - das ist die Zielgröße, für die diese Prompts bereits
# ausgelegt sind. Reicht die normale Redaktion allein nicht aus, greift
# fuehre_notfall_auffuellung_aus() als Sicherheitsnetz (siehe Docstring oben).
MIN_THEMEN_FUER_EPISODE = 5
# Muss mit generiere_episode.OFFENE_STATUS übereinstimmen - hier bewusst
# dupliziert statt importiert, damit die beiden Module unabhängig bleiben.
OFFENE_THEMEN_STATUS = ("neu", "in Verfolgung")


def hole_supabase_client():
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def hole_chat_model():
    return GeminiModell(CHAT_MODEL)


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


def waehle_relevante_nachrichten(
    supabase, chat_model, systemkontext: str, rohnachrichten: list[dict], lauf_id: str | None = None
) -> list[dict]:
    nachrichten_block = "\n\n".join(
        f'Titel: {r["titel"]}\nText: {r["text"]}' for r in rohnachrichten
    )

    prompt = (
        f"{systemkontext}\n\n"
        "Wähle aus den folgenden Nachrichten 5-7 geeignete Meldungen für deinen Fokus aus, "
        "jeweils mit kurzer Begründung. Priorisiere zuerst die besonders relevanten Meldungen. "
        "Wenn davon weniger als 5 vorhanden sind, ergänze auch Meldungen mittlerer Relevanz, "
        "sofern sie aktuell, sachlich belastbar und für kleine oder mittlere Unternehmen "
        "zumindest zur Einordnung nützlich sind. Nimm keine bloßen Gerüchte, Duplikate, "
        "veralteten Meldungen oder thematisch unpassenden Inhalte nur zum Auffüllen auf.\n\n"
        f"{nachrichten_block}\n\n"
        "Antworte NUR mit JSON in diesem Format: "
        '[{"rohnachricht_titel": string, "begruendung": string}]'
    )

    antwort = chat_model.generate_content(
        prompt,
        generation_config={"response_mime_type": "application/json"},
    )

    kosten_tracking.logge_api_kosten(
        supabase,
        dienst="gemini",
        modell=CHAT_MODEL,
        schritt="recherche_auswahl",
        einheit_typ="tokens",
        menge_input=antwort.usage_metadata.prompt_token_count,
        menge_output=antwort.usage_metadata.candidates_token_count,
        lauf_id=lauf_id,
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


def fuehre_recherche_fuer_agenten_aus(
    supabase, chat_model, agent: dict, zusatz_anweisung: str | None, lauf_id: str | None = None
) -> int:
    name = agent["name"]
    print(f'Recherche-Agent "{name}": suche neue Rohnachrichten...')

    rohnachrichten = hole_unverarbeitete_rohnachrichten(supabase, agent["id"])
    if not rohnachrichten:
        print(f'-> Keine neuen Rohnachrichten der letzten {MAX_ALTER_TAGE} Tage für "{name}".\n')
        return 0

    systemkontext = baue_systemkontext(agent.get("fokus_beschreibung") or "", zusatz_anweisung)
    auswahl = waehle_relevante_nachrichten(supabase, chat_model, systemkontext, rohnachrichten, lauf_id=lauf_id)

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


def entscheide_ueber_vorschlaege(
    supabase, chat_model, systemkontext: str, vorschlaege: list[dict], lauf_id: str | None = None
) -> list[dict]:
    vorschlaege_block = "\n\n".join(baue_vorschlag_block(i, v) for i, v in enumerate(vorschlaege))

    prompt = (
        f"{systemkontext}\n\n"
        "Hier sind alle offenen Themen-Vorschläge der Recherche-Agenten für die nächste Episode. "
        "Wähle möglichst 5-6 Themen aus. Gib für JEDEN Vorschlag eine Entscheidung ab "
        "(auch für die nicht ausgewählten), jeweils mit Begründung.\n\n"
        "Arbeite mit zwei Qualitätsstufen:\n"
        "- Kategorie A: hohe unmittelbare Relevanz für Geschäftsführer kleiner oder mittlerer Unternehmen.\n"
        "- Kategorie B: mittlere Relevanz, aber aktuell, sachlich belastbar und nützlich für "
        "Marktbeobachtung, strategische Einordnung oder eine spätere Entscheidung.\n"
        "Akzeptiere zuerst Kategorie A. Wenn dadurch weniger als 5 Themen zusammenkommen und "
        "mindestens 5 geeignete Vorschläge vorliegen, fülle mit den besten Kategorie-B-Themen "
        "bis auf mindestens 5 auf. Lehne weiterhin Duplikate, Gerüchte, veraltete oder völlig "
        "belanglose Meldungen ab; die Mindestzahl darf niemals durch schlechte oder unbelegte "
        "Inhalte erzwungen werden. Nenne in der Begründung die Kategorie A oder B.\n\n"
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

    kosten_tracking.logge_api_kosten(
        supabase,
        dienst="gemini",
        modell=CHAT_MODEL,
        schritt="redaktion_entscheidung",
        einheit_typ="tokens",
        menge_input=antwort.usage_metadata.prompt_token_count,
        menge_output=antwort.usage_metadata.candidates_token_count,
        lauf_id=lauf_id,
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


def fuehre_redaktion_fuer_agenten_aus(
    supabase, chat_model, agent: dict, zusatz_anweisung: str | None, lauf_id: str | None = None
) -> int:
    name = agent["name"]
    print(f'Redaktions-Agent "{name}": hole offene Vorschläge...')

    offene_vorschlaege = hole_offene_vorschlaege(supabase)
    if not offene_vorschlaege:
        print("-> Keine offenen Vorschläge vorhanden.\n")
        return 0

    systemkontext = baue_systemkontext(agent.get("fokus_beschreibung") or "", zusatz_anweisung)
    entscheidungen = entscheide_ueber_vorschlaege(
        supabase, chat_model, systemkontext, offene_vorschlaege, lauf_id=lauf_id
    )

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


def entscheide_ueber_updates(
    supabase, chat_model, systemkontext: str, updates: list[dict], lauf_id: str | None = None
) -> list[dict]:
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

    kosten_tracking.logge_api_kosten(
        supabase,
        dienst="gemini",
        modell=CHAT_MODEL,
        schritt="update_reaktivierung",
        einheit_typ="tokens",
        menge_input=antwort.usage_metadata.prompt_token_count,
        menge_output=antwort.usage_metadata.candidates_token_count,
        lauf_id=lauf_id,
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
    supabase, chat_model, agent: dict, zusatz_anweisung: str | None, lauf_id: str | None = None
) -> int:
    print("Prüfe Updates zu bereits gesendeten Themen...")

    offene_updates = hole_offene_updates(supabase)
    if not offene_updates:
        print("-> Keine neuen Updates zu gesendeten Themen.\n")
        return 0

    systemkontext = baue_systemkontext(agent.get("fokus_beschreibung") or "", zusatz_anweisung)
    entscheidungen = entscheide_ueber_updates(supabase, chat_model, systemkontext, offene_updates, lauf_id=lauf_id)

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


def fuehre_recherche_agenten_aus(zusatz_anweisung: str | None = None, lauf_id: str | None = None) -> None:
    """Lässt alle aktiven Recherche-Agenten über neue Rohnachrichten laufen.

    Schlägt ein Agent fehl (z.B. API-Kontingent), wird das geloggt und mit dem
    nächsten Agenten weitergemacht - bereits gespeicherte Vorschläge anderer
    Agenten (vor UND nach dem gescheiterten) bleiben unberührt."""
    supabase = hole_supabase_client()
    chat_model = hole_chat_model()

    agenten = hole_aktive_agenten(supabase, rolle="recherche")
    if not agenten:
        print("Keine aktiven Recherche-Agenten gefunden.")
        return

    print(f"{len(agenten)} aktive(r) Recherche-Agent(en) gefunden.\n")

    gesamt = 0
    fehlgeschlagen = []
    for agent in agenten:
        try:
            gesamt += fuehre_recherche_fuer_agenten_aus(
                supabase, chat_model, agent, zusatz_anweisung, lauf_id=lauf_id
            )
        except Exception as e:
            fehlgeschlagen.append(agent["name"])
            print(
                f'WARNUNG: Recherche-Agent "{agent["name"]}" fehlgeschlagen '
                f'({type(e).__name__}: {e}) - bereits gespeicherte Vorschläge anderer '
                "Agenten bleiben erhalten, weiter mit dem nächsten Agenten.\n"
            )

    zusammenfassung = f"Fertig. Insgesamt {gesamt} neue Vorschläge gespeichert."
    if fehlgeschlagen:
        zusammenfassung += f" ({len(fehlgeschlagen)} Agent(en) fehlgeschlagen: {', '.join(fehlgeschlagen)})"
    print(zusammenfassung)


def fuehre_einzelnen_agenten_aus(
    agent_name: str, zusatz_anweisung: str | None = None, lauf_id: str | None = None
) -> None:
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
        fuehre_redaktion_fuer_agenten_aus(supabase, chat_model, agent, zusatz_anweisung, lauf_id=lauf_id)
    else:
        fuehre_recherche_fuer_agenten_aus(supabase, chat_model, agent, zusatz_anweisung, lauf_id=lauf_id)


def hole_aktiven_redaktionsagenten(supabase) -> dict | None:
    """Findet den aktiven Redaktions-Agenten; warnt und nimmt den ersten, falls mehrere aktiv sind."""
    agenten = hole_aktive_agenten(supabase, rolle="redaktion")
    if not agenten:
        return None
    if len(agenten) > 1:
        print(
            f'Warnung: {len(agenten)} aktive Redaktions-Agenten gefunden, '
            f'nutze den ersten: "{agenten[0]["name"]}".'
        )
    return agenten[0]


def fuehre_redaktion_aus(zusatz_anweisung: str | None = None, lauf_id: str | None = None) -> None:
    """Lässt den aktiven Redaktions-Agenten über alle offenen Vorschläge entscheiden."""
    supabase = hole_supabase_client()
    chat_model = hole_chat_model()

    agent = hole_aktiven_redaktionsagenten(supabase)
    if agent is None:
        print("Kein aktiver Redaktions-Agent gefunden.")
        return

    fuehre_redaktion_fuer_agenten_aus(supabase, chat_model, agent, zusatz_anweisung, lauf_id=lauf_id)


def pruefe_update_reaktivierung(zusatz_anweisung: str | None = None, lauf_id: str | None = None) -> int:
    """Lässt den aktiven Redaktions-Agenten prüfen, ob neue Updates zu bereits gesendeten Themen wichtig genug sind, um das Thema erneut aufzugreifen."""
    supabase = hole_supabase_client()
    chat_model = hole_chat_model()

    agent = hole_aktiven_redaktionsagenten(supabase)
    if agent is None:
        print("Kein aktiver Redaktions-Agent gefunden.")
        return 0

    return pruefe_updates_zu_gesendeten_themen(supabase, chat_model, agent, zusatz_anweisung, lauf_id=lauf_id)


def hole_entscheidungen_ohne_thema(supabase, status: str) -> list[dict]:
    """Holt alle Entscheidungen mit gegebenem Status, die noch keinem Thema
    zugeordnet sind (thema_id IS NULL), samt Titel/Text der zugehörigen
    Rohnachricht. Gemeinsame Basis für hole_akzeptierte_offene_entscheidungen()
    und die Notfall-Auffüllung (die dieselbe Abfrage für "zurueckgestellt" und
    "abgelehnt" braucht)."""
    entscheidungen = (
        supabase.table("redaktion_entscheidungen")
        .select("id, vorschlag_id, begruendung")
        .eq("status", status)
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
                "begruendung": e.get("begruendung") or "",
                "rohnachricht_titel": rohnachricht["titel"] or "",
                "rohnachricht_text": rohnachricht["text"] or "",
            }
        )
    return ergebnis


def hole_akzeptierte_offene_entscheidungen(supabase) -> list[dict]:
    return hole_entscheidungen_ohne_thema(supabase, "akzeptiert")


def hole_tavily_treffer(query: str) -> list[dict]:
    client = TavilyClient(api_key=os.environ["TAVILY_API_KEY"])
    antwort = client.search(
        query, search_depth="basic", topic="news", max_results=ZWEITE_QUELLE_MAX_TREFFER
    )
    return [
        {"titel": r.get("title"), "url": r.get("url"), "ausschnitt": r.get("content") or ""}
        for r in antwort.get("results") or []
    ]


def hole_exa_treffer(query: str) -> list[dict]:
    client = Exa(api_key=os.environ["EXA_API_KEY"])
    antwort = client.search(
        query, num_results=ZWEITE_QUELLE_MAX_TREFFER, contents={"text": {"max_characters": 500}}
    )
    return [{"titel": r.title, "url": r.url, "ausschnitt": r.text or ""} for r in antwort.results]


def hole_zweite_quelle_treffer(thema_titel: str) -> list[dict]:
    """Sucht gezielt nach unabhängigen Quellen zum Themen-Titel: erst Tavily,
    bei Fehler oder leerem Ergebnis Exa als Fallback. Liefert beide nichts,
    kommt eine leere Liste zurück (kein Fehler)."""
    try:
        treffer = hole_tavily_treffer(thema_titel)
    except Exception as e:
        print(f"  Tavily-Fehler bei Zweite-Quelle-Suche ({type(e).__name__}: {e}), versuche Exa...")
        treffer = []

    if treffer:
        return treffer

    try:
        return hole_exa_treffer(thema_titel)
    except Exception as e:
        print(f"  Exa-Fehler bei Zweite-Quelle-Suche ({type(e).__name__}: {e}).")
        return []


def baue_zweite_quelle_prompt(rohnachricht_text: str, treffer: list[dict]) -> str:
    treffer_block = "\n\n".join(
        f'Quelle: {t["titel"]}\nURL: {t["url"]}\nAusschnitt: {t["ausschnitt"][:800]}' for t in treffer
    )
    return (
        "URSPRÜNGLICHER TEXT (Basis des Themas):\n"
        f"{rohnachricht_text}\n\n"
        "GEFUNDENE SUCHERGEBNISSE (unabhängige Quellen):\n"
        f"{treffer_block}\n\n"
        "Bestätigt eine dieser unabhängigen Quellen den Kernfakt des Themas? "
        'Antworte NUR mit JSON: {"bestaetigt": bool, "bestaetigende_quelle_url": string oder null, '
        '"kurze_einschaetzung": string}'
    )


def pruefe_zweite_quelle(
    supabase,
    chat_model,
    thema_id: str,
    thema_titel: str,
    rohnachricht_text: str,
    lauf_id: str | None = None,
) -> None:
    """Sucht gezielt nach einer unabhängigen zweiten Quelle für ein neu
    angelegtes Thema (Ausschreibungs-Kriterium 5) und speichert das
    Gemini-Ergebnis direkt an der Themen-Zeile. Findet sich keine Quelle,
    bleibt das Feld NULL - nur eine Konsolen-Notiz, kein Fehler."""
    treffer = hole_zweite_quelle_treffer(thema_titel)
    if not treffer:
        print(f'Thema "{thema_titel}": keine Suchergebnisse für Zweite-Quelle-Prüfung gefunden.')
        return

    prompt = baue_zweite_quelle_prompt(rohnachricht_text, treffer)
    antwort = chat_model.generate_content(
        prompt, generation_config={"response_mime_type": "application/json"}
    )

    kosten_tracking.logge_api_kosten(
        supabase,
        dienst="gemini",
        modell=CHAT_MODEL,
        schritt="zweite_quelle_pruefung",
        einheit_typ="tokens",
        menge_input=antwort.usage_metadata.prompt_token_count,
        menge_output=antwort.usage_metadata.candidates_token_count,
        lauf_id=lauf_id,
    )

    ergebnis = json.loads(antwort.text)
    bestaetigt = bool(ergebnis.get("bestaetigt"))
    url = ergebnis.get("bestaetigende_quelle_url") if bestaetigt else None
    einschaetzung = ergebnis.get("kurze_einschaetzung")

    supabase.table("themen").update(
        {
            "zweite_quelle_bestaetigt": bestaetigt,
            "zweite_quelle_url": url,
            "zweite_quelle_einschaetzung": einschaetzung,
        }
    ).eq("id", thema_id).execute()

    if bestaetigt:
        print(f'Thema "{thema_titel}": Zweite Quelle bestätigt ({url}).')
    else:
        print(f'Thema "{thema_titel}": Zweite Quelle NICHT bestätigt ({einschaetzung}).')


def verarbeite_akzeptierte_entscheidungen(lauf_id: str | None = None) -> list[dict]:
    """Ordnet akzeptierte, noch nicht verknüpfte Entscheidungen einem Thema zu.

    Holt alle Entscheidungen mit akzeptiert=true und thema_id IS NULL, lässt
    Titel+Text der zugehörigen Rohnachricht über die bestehende Logik aus
    verarbeite_rohnachricht.py verarbeiten (Embedding, Ähnlichkeitssuche,
    neues Thema oder Update) und trägt die entstandene/gefundene thema_id
    zurück in redaktion_entscheidungen ein.

    Entsteht dabei ein NEUES Thema, läuft zusätzlich pruefe_zweite_quelle()
    (Tavily/Exa-Suche + Gemini-Abgleich, siehe Modul-Docstring oben) - bei
    Update/Duplikat nicht, um Kosten zu sparen.
    """
    supabase = hole_supabase_client()
    chat_model = hole_chat_model()

    offene = hole_akzeptierte_offene_entscheidungen(supabase)
    if not offene:
        print("Keine akzeptierten, noch nicht verknüpften Entscheidungen gefunden.")
        return []

    print(f"{len(offene)} akzeptierte Entscheidung(en) ohne Thema-Verknüpfung gefunden.\n")

    ergebnisse = []
    for eintrag in offene:
        text = f'{eintrag["rohnachricht_titel"]}\n{eintrag["rohnachricht_text"]}'.strip()

        verarbeitung = verarbeite_rohnachricht.verarbeite_text(text, lauf_id=lauf_id)

        supabase.table("redaktion_entscheidungen").update({"thema_id": verarbeitung["thema_id"]}).eq(
            "id", eintrag["entscheidung_id"]
        ).execute()

        if verarbeitung["art"] == "neu":
            try:
                pruefe_zweite_quelle(
                    supabase,
                    chat_model,
                    verarbeitung["thema_id"],
                    verarbeitung["titel"],
                    eintrag["rohnachricht_text"],
                    lauf_id=lauf_id,
                )
            except Exception as e:
                print(
                    f'  WARNUNG: Zweite-Quelle-Prüfung für "{verarbeitung["titel"]}" '
                    f"fehlgeschlagen ({type(e).__name__}: {e}), wird übersprungen."
                )

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


def zaehle_offene_themen(supabase) -> int:
    return len(
        supabase.table("themen").select("id").in_("status", OFFENE_THEMEN_STATUS).execute().data
    )


def baue_notfall_kandidat_block(i: int, k: dict) -> str:
    return (
        f"[{i}] Titel: {k['rohnachricht_titel']}\n"
        f"Ursprüngliche Begründung (Ablehnung/Zurückstellung): {k['begruendung']}\n"
        f"Text: {k['rohnachricht_text']}"
    )


def waehle_notfall_kandidaten(
    supabase,
    chat_model,
    systemkontext: str,
    kandidaten: list[dict],
    anzahl: int,
    lauf_id: str | None = None,
) -> list[dict]:
    """Wählt aus bereits abgelehnten/zurückgestellten Vorschlägen die am
    wenigsten schlechten aus, wenn für die heutige Episode sonst zu wenig
    Themen zusammenkommen. Lockert NUR den Relevanz-Maßstab - Duplikate,
    Gerüchte und faktisch unbelegte Meldungen bleiben auch im Notfall
    ausgeschlossen, damit die Episode weiterhin sachlich korrekt bleibt."""
    kandidaten_block = "\n\n".join(baue_notfall_kandidat_block(i, k) for i, k in enumerate(kandidaten))

    prompt = (
        f"{systemkontext}\n\n"
        f"NOTFALL-AUFFÜLLUNG: Für die heutige Episode gibt es sonst zu wenige Themen "
        f"(Ziel: mindestens {MIN_THEMEN_FUER_EPISODE}). Die folgenden Vorschläge wurden "
        "zuvor abgelehnt oder zurückgestellt. Wähle davon bis zu "
        f"{anzahl} aus, die TROTZ geringerer Relevanz sachlich korrekt und aktuell sind - "
        "am Rande nützlich für kleine oder mittlere Unternehmen reicht als Maßstab. Wähle "
        "NIEMALS einen Vorschlag, der laut ursprünglicher Begründung ein Duplikat, ein "
        "Gerücht oder faktisch unbelegt ist - diese Kriterien werden NICHT gelockert. Gib "
        "für jede Auswahl eine kurze Begründung. Erfüllen weniger als "
        f"{anzahl} Vorschläge die Mindestanforderung, wähle entsprechend weniger aus - "
        "erzwinge die Anzahl nicht.\n\n"
        f"{kandidaten_block}\n\n"
        "Antworte NUR mit JSON in diesem Format: "
        '[{"index": int, "begruendung": string}]'
    )

    antwort = chat_model.generate_content(
        prompt,
        generation_config={"response_mime_type": "application/json"},
    )

    kosten_tracking.logge_api_kosten(
        supabase,
        dienst="gemini",
        modell=CHAT_MODEL,
        schritt="notfall_auffuellung",
        einheit_typ="tokens",
        menge_input=antwort.usage_metadata.prompt_token_count,
        menge_output=antwort.usage_metadata.candidates_token_count,
        lauf_id=lauf_id,
    )

    auswahl = json.loads(antwort.text)

    ergebnis = []
    for eintrag in auswahl:
        index = eintrag.get("index")
        if index is None or not (0 <= index < len(kandidaten)):
            print(f"-> Warnung: Notfall-Auswahl mit ungültigem Index {index} wird übersprungen.")
            continue
        ergebnis.append({**kandidaten[index], "notfall_begruendung": eintrag.get("begruendung", "")})
    return ergebnis[:anzahl]


def fuehre_notfall_auffuellung_aus(zusatz_anweisung: str | None = None, lauf_id: str | None = None) -> int:
    """Sicherheitsnetz: sorgt dafür, dass für die nächste Episode genug Themen
    vorhanden sind (siehe Modul-Docstring, Punkt 6, und generiere_episode.py -
    dort bricht die Manuskripterstellung ab, wenn zu wenig Stoff für die
    Mindestwortzahl vorliegt). Greift nur ein, wenn die normale Redaktion
    (Schritt 3) nicht auf MIN_THEMEN_FUER_EPISODE kommt; sonst No-Op."""
    supabase = hole_supabase_client()

    vorhanden = zaehle_offene_themen(supabase) + len(hole_akzeptierte_offene_entscheidungen(supabase))
    if vorhanden >= MIN_THEMEN_FUER_EPISODE:
        print(
            f"Notfall-Auffüllung: {vorhanden} Themen vorhanden (Minimum "
            f"{MIN_THEMEN_FUER_EPISODE}), keine Auffüllung nötig.\n"
        )
        return 0

    fehlende = MIN_THEMEN_FUER_EPISODE - vorhanden
    print(
        f"Notfall-Auffüllung: nur {vorhanden} Thema/Themen vorhanden (Minimum "
        f"{MIN_THEMEN_FUER_EPISODE}), suche bis zu {fehlende} zusätzliche(s) Thema/Themen "
        "unter bereits abgelehnten/zurückgestellten Vorschlägen."
    )

    chat_model = hole_chat_model()
    agent = hole_aktiven_redaktionsagenten(supabase)
    systemkontext = baue_systemkontext(agent.get("fokus_beschreibung") if agent else "", zusatz_anweisung)

    jetzt = datetime.now(timezone.utc).isoformat()
    aufgefuellt = 0

    for status in ("zurueckgestellt", "abgelehnt"):
        if fehlende <= 0:
            break
        kandidaten = hole_entscheidungen_ohne_thema(supabase, status)
        if not kandidaten:
            continue

        ausgewaehlt = waehle_notfall_kandidaten(
            supabase, chat_model, systemkontext, kandidaten, fehlende, lauf_id=lauf_id
        )
        for k in ausgewaehlt:
            supabase.table("redaktion_entscheidungen").update(
                {
                    "status": "akzeptiert",
                    "akzeptiert": True,
                    "begruendung": f'[Notfall-Auffüllung, ursprünglich "{status}"] {k["notfall_begruendung"]}',
                    "entschieden_am": jetzt,
                }
            ).eq("id", k["entscheidung_id"]).execute()
            print(f'-> Notfall-akzeptiert (vorher {status}): "{k["rohnachricht_titel"]}"')
            aufgefuellt += 1
            fehlende -= 1

    if aufgefuellt == 0:
        print(
            "-> Keine geeigneten Notfall-Kandidaten gefunden (weder zurückgestellt noch "
            "abgelehnt passend) - Episode läuft ggf. mit weniger Themen als gewünscht.\n"
        )
    else:
        print(f"-> {aufgefuellt} Thema/Themen per Notfall-Auffüllung akzeptiert.\n")

    return aufgefuellt


if __name__ == "__main__":
    befehl = sys.argv[1] if len(sys.argv) > 1 else "recherche"

    if befehl == "recherche":
        fuehre_recherche_agenten_aus()
    elif befehl == "redaktion":
        fuehre_redaktion_aus()
    elif befehl == "update_reaktivierung":
        pruefe_update_reaktivierung()
    elif befehl == "verarbeite":
        verarbeite_akzeptierte_entscheidungen()
    elif befehl == "notfall":
        fuehre_notfall_auffuellung_aus()
    elif befehl == "agent":
        if len(sys.argv) < 3:
            print('Nutzung: python recherche_und_redaktion.py agent "<Agent-Name>" ["<Zusatz-Anweisung>"]')
        else:
            fuehre_einzelnen_agenten_aus(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
    else:
        print(
            f'Unbekannter Befehl: "{befehl}". Nutze "recherche", "redaktion", '
            '"update_reaktivierung", "verarbeite", "notfall" oder "agent".'
        )
