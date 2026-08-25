"""Erzeugt aus den aktuell offenen Themen ein Podcast-Manuskript und legt eine
neue Episode in "episoden" an.

Die Moderator-Persona (Ton, Zielgruppe, Stil) wird aus "agenten_konfiguration"
geholt (rolle="moderator") und bildet die Hauptgrundlage für den
Gemini-Prompt.

Zwei unabhängig aufrufbare Funktionen:

1. erstelle_episode(zusatz_anweisung=None, format=None)
   Holt die aktive Moderator-Persona sowie alle Themen mit Status "neu" oder
   "in Verfolgung" samt ihren bisherigen Updates aus "themen_updates", lässt
   Gemini daraus im Ton dieser Persona ein Manuskript schreiben, speichert es
   in "episoden" und markiert die verwendeten Themen als "gesendet", damit sie
   nicht in der nächsten Episode erneut auftauchen. Der Rückgabewert enthält
   zusätzlich "verwendete_themen" (id/titel/zusammenfassung der tatsächlich
   verwendeten Themen) für den nachfolgenden Faktencheck.

   Erkennt automatisch den Wochentag und schaltet für Montag/Freitag ein
   Sonderformat frei (siehe Abschnitt 6 der README): montags wird der
   Einstieg explizit als Wochenend-Rückblick gerahmt (Themen/Updates vom
   Samstag/Sonntag werden dafür mit Datum an Gemini übergeben), freitags
   bekommt Gemini zusätzlich einen Wochenrückblick (alle Themen/Updates seit
   Montag dieser Woche, unabhängig vom Status) als Kontext für den
   Wochenbogen. Di-Do läuft im bisherigen Standard-Format. Für Tests kann
   das Format über den Parameter erzwungen werden, z.B. format="montag"
   (gültig: "montag", "freitag", "standard").

2. pruefe_manuskript(episode_id, manuskripttext, themen)
   Sammelt für jedes übergebene Thema die verknüpften Original-Rohnachrichten
   (über redaktion_entscheidungen -> agent_vorschlaege -> rohnachrichten) als
   Quellenbasis, lässt Gemini jede konkrete Zahl/Namen/Datumsangabe im
   Manuskript dagegen prüfen und speichert Ergebnis + Status ("freigegeben"
   oder "pruefung_fehlgeschlagen" bei mind. einem Widerspruch) direkt in der
   episoden-Zeile. Voraussetzung: Migration
   20260825080000_episoden_faktencheck.sql muss angewendet sein.
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import google.generativeai as genai
from dotenv import load_dotenv
from supabase import create_client

import kosten_tracking

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()

CHAT_MODEL = os.environ["GEMINI_MODEL_NAME"]
OFFENE_STATUS = ("neu", "in Verfolgung")

FORMAT_NACH_WOCHENTAG = {0: "montag", 4: "freitag"}  # datetime.weekday(): Montag=0 .. Sonntag=6
GUELTIGE_FORMATE = {"montag", "freitag", "standard"}


def hole_supabase_client():
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def bestimme_format(format: str | None, heute: datetime) -> str:
    """Validiert einen Format-Override, sonst wird das Format per Wochentag abgeleitet."""
    if format is not None:
        if format not in GUELTIGE_FORMATE:
            raise ValueError(f'Unbekanntes Format "{format}", erlaubt: {sorted(GUELTIGE_FORMATE)}')
        return format
    return FORMAT_NACH_WOCHENTAG.get(heute.weekday(), "standard")


def hole_wochenstart(heute: datetime) -> datetime:
    """Montag 00:00 UTC der Kalenderwoche von `heute`."""
    start = heute - timedelta(days=heute.weekday())
    return start.replace(hour=0, minute=0, second=0, microsecond=0)


def hole_wochenende_daten(heute: datetime) -> tuple[str, str]:
    """(Samstag, Sonntag) des letzten Wochenendes vor der Kalenderwoche von `heute`, als ISO-Datum."""
    wochenstart = hole_wochenstart(heute)
    samstag = (wochenstart - timedelta(days=2)).date().isoformat()
    sonntag = (wochenstart - timedelta(days=1)).date().isoformat()
    return samstag, sonntag


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
        .select("id, titel, zusammenfassung, status, erster_kontaktzeitpunkt")
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
    updates_nach_thema: dict[str, list[dict]] = {}
    for u in updates:
        updates_nach_thema.setdefault(u["thema_id"], []).append({"text": u["was_neu"], "datum": u["datum"]})

    return [{**t, "updates": updates_nach_thema.get(t["id"], [])} for t in themen]


def baue_themen_block(themen: list[dict], mit_daten: bool = False) -> str:
    bloecke = []
    for t in themen:
        titel_zeile = f'[ID: {t["id"]}] Thema: {t["titel"]}'
        if mit_daten and t.get("erster_kontaktzeitpunkt"):
            titel_zeile += f' (zuerst erfasst: {str(t["erster_kontaktzeitpunkt"])[:10]})'
        block = f'{titel_zeile}\nStand: {t["zusammenfassung"] or ""}'
        if t["status"] == "in Verfolgung":
            block += (
                "\nHinweis: Fortsetzung eines bereits gesendeten Themas - es gibt eine "
                "wichtige neue Entwicklung."
            )
        if t["updates"]:
            zeilen = []
            for u in t["updates"]:
                datum_praefix = f'[{str(u["datum"])[:10]}] ' if mit_daten and u.get("datum") else ""
                zeilen.append(f'- {datum_praefix}{u["text"]}')
            block += "\nNeue Fakten seitdem:\n" + "\n".join(zeilen)
        bloecke.append(block)
    return "\n\n".join(bloecke)


def hole_themen_der_woche(supabase, wochenstart_iso: str) -> list[dict]:
    """Alle Themen (jeder Status, auch "gesendet") mit erster_kontaktzeitpunkt seit
    Wochenstart ODER mit mindestens einem Update seit Wochenstart - fuer den
    Freitags-Wochenrueckblick. Jedes Ergebnis bekommt "updates_diese_woche" (chronologisch)."""
    neue = (
        supabase.table("themen")
        .select("id, titel, zusammenfassung, status, erster_kontaktzeitpunkt")
        .gte("erster_kontaktzeitpunkt", wochenstart_iso)
        .execute()
        .data
    )
    updates = (
        supabase.table("themen_updates")
        .select("thema_id, was_neu, datum")
        .gte("datum", wochenstart_iso)
        .order("datum")
        .execute()
        .data
    )

    themen_nach_id = {t["id"]: t for t in neue}
    fehlende_ids = {u["thema_id"] for u in updates} - themen_nach_id.keys()
    if fehlende_ids:
        nachgeladen = (
            supabase.table("themen")
            .select("id, titel, zusammenfassung, status, erster_kontaktzeitpunkt")
            .in_("id", list(fehlende_ids))
            .execute()
            .data
        )
        for t in nachgeladen:
            themen_nach_id[t["id"]] = t

    updates_nach_thema: dict[str, list[dict]] = {}
    for u in updates:
        updates_nach_thema.setdefault(u["thema_id"], []).append(u)

    return [
        {**t, "updates_diese_woche": updates_nach_thema.get(tid, [])}
        for tid, t in themen_nach_id.items()
    ]


def baue_wochenrueckblick_block(themen_woche: list[dict]) -> str:
    if not themen_woche:
        return ""
    bloecke = []
    for t in themen_woche:
        zeilen = [f'Thema: {t["titel"]} (Status: {t["status"]})', f'Ausgangslage: {t.get("zusammenfassung") or ""}']
        for u in t["updates_diese_woche"]:
            zeilen.append(f'  - [{str(u["datum"])[:10]}] {u["was_neu"]}')
        bloecke.append("\n".join(zeilen))
    return "\n\n".join(bloecke)


def baue_format_hinweis(format: str, samstag: str, sonntag: str) -> str:
    if format == "montag":
        return (
            "BESONDERHEIT DIESER FOLGE - MONTAGSFOLGE:\n\n"
            "Das ist die Montagsfolge. Rahme den Einstieg explizit als "
            'Wochenend-Rückblick: "Am Wochenende ist einiges passiert, das ihr '
            'noch nicht gehört habt" oder ähnlich. Die Hörer waren übers '
            "Wochenende nicht dabei, hol sie ab. Themen oder Updates, die oben "
            f"mit einem Datum vom {samstag} (Samstag) oder {sonntag} (Sonntag) "
            "markiert sind, sind vom Wochenende - nutze die für diese Rahmung. "
            "Sind keine Themen/Updates mit diesen Daten markiert, verzichte auf "
            "die Wochenend-Rahmung und steig wie gewohnt ein."
        )
    if format == "freitag":
        return (
            "BESONDERHEIT DIESER FOLGE - FREITAGSFOLGE:\n\n"
            "Das ist die Freitagsfolge. Wiederhole NICHT einfach die Meldungen "
            "der Woche. Zeige stattdessen die LINIE: Wie hat sich ein Thema "
            "über die Woche entwickelt? Was ist das größere Bild, das sich aus "
            "den einzelnen Meldungen ergibt? Fasse mit Abstand zusammen, nicht "
            "mit Details.\n\n"
            "WICHTIG zum WOCHENRÜCKBLICK weiter unten: Das ist Kontext-Material "
            "für dich, KEIN Themen-Pool für zusätzliche Episoden-Segmente. Baue "
            "daraus NICHT für jedes dort aufgeführte Thema einen eigenen "
            "vollwertigen Themenblock mit eigenem Hook, eigenen Detailzahlen "
            "und eigener Handlungsempfehlung wie bei den regulären Themen oben "
            "- sonst ist es wieder nur eine Aneinanderreihung von Meldungen. "
            "Genau eine Ausnahme: Zeigt ein Thema im WOCHENRÜCKBLICK erkennbar "
            "eine echte Entwicklung über mehrere Tage (z.B. Ankündigung -> "
            "Reaktion -> Ergebnis), darfst du dafür einen kurzen, komprimierten "
            "Bogen erzählen (2-4 Sätze, kein eigener Hook, keine "
            "Handlungsempfehlung, keine Detailzahlen über die Linie hinaus). "
            "Alle übrigen WOCHENRÜCKBLICK-Themen ohne erkennbare Entwicklung "
            "über die Woche bekommen höchstens eine beiläufige Erwähnung in "
            "maximal einem Halbsatz (z.B. als Teil eines Übergangs) oder "
            "fallen ganz weg."
        )
    return ""


EROEFFNUNGSSIGNATUR_NACH_FORMAT = {
    "montag": "Guten Morgen, willkommen zur Wochenend-Ausgabe eures KI-Updates.",
    "freitag": "Guten Morgen, willkommen zur Wochenrückblick-Ausgabe eures KI-Updates.",
}
EROEFFNUNGSSIGNATUR_STANDARD = "Guten Morgen, das ist euer KI-Update."


def baue_eroeffnungssignatur(format: str) -> str:
    beispiel = EROEFFNUNGSSIGNATUR_NACH_FORMAT.get(format, EROEFFNUNGSSIGNATUR_STANDARD)
    return (
        f'Beginne JEDE Episode mit exakt diesem kurzen Muster (1 Satz, maximal 2 '
        f'Sekunden Sprechzeit): "{beispiel}" oder einer minimalen, konsistenten '
        "Variante davon. Geh danach OHNE Pause direkt in den Hook über - keine "
        "weitere Anmoderation, keine Themenankündigung, keine Übergangsfloskel "
        "zwischen Begrüßung und Hook."
    )


def baue_manuskript_prompt(
    persona: str,
    themen_block: str,
    zusatz_anweisung: str | None,
    eroeffnungssignatur: str,
    format_hinweis: str = "",
    wochenrueckblick_block: str = "",
) -> str:
    wochenrueckblick_abschnitt = (
        f"WOCHENRÜCKBLICK (Kontext zur Einordnung, NICHT einfach nochmal alle Punkte auflisten):\n\n"
        f"{wochenrueckblick_block}\n\n"
        if wochenrueckblick_block
        else ""
    )
    prompt = (
        f"{persona}\n\n"
        "Erzähle wie eine Geschichte, nicht wie eine Nachrichtenmeldung. Der Hörer "
        "soll sich in den ersten 15 Sekunden gepackt fühlen, nicht erst nach einer "
        "Anmoderation.\n\n"
        "Du schreibst das Manuskript für die nächste Folge deines Podcasts. Hier "
        "sind die aktuell akzeptierten Themen (die [ID: ...]-Markierung ist nur "
        "für dich zur Zuordnung, NICHT vorlesen):\n\n"
        f"{themen_block}\n\n"
        f"{wochenrueckblick_abschnitt}"
        "THEMENAUSWAHL:\n\n"
        "- Wähle daraus die 5-6 wichtigsten Themen für diese Episode aus. Wenn "
        "mehr als 6 Themen aufgeführt sind, lass die übrigen bewusst weg - nimm "
        "die, die für den Hörer gerade am relevantesten oder aktuellsten sind.\n\n"
        + (f"{format_hinweis}\n\n" if format_hinweis else "")
        + "LÄNGE:\n\n"
        "- Das fertige Manuskript soll 1400-1600 Wörter umfassen. Erreiche das "
        "NICHT durch mehr Themen, sondern durch mehr Tiefe pro Geschichte: ein "
        "zusätzliches konkretes Detail, ein kurzes Beispiel aus der Praxis, oder "
        "eine kurze Einordnung, warum das Thema gerade jetzt relevant ist. Jeder "
        "Themenblock darf ruhig 30-50% länger werden als bisher.\n\n"
        "FORTSETZUNGEN:\n\n"
        '- Trägt ein Thema den Hinweis "Fortsetzung eines bereits gesendeten Themas", '
        "erwähne kurz und beiläufig, dass ihr darüber schon mal gesprochen habt "
        '(z.B. "Erinnert ihr euch an ..." oder "Update zu einer Geschichte, die wir '
        'schon hatten"), bevor du die neue Entwicklung erzählst. Bei Themen ohne diesen '
        "Hinweis: keine solche Anmoderation.\n\n"
        "AUFBAU DER EPISODE:\n\n"
        f"- {eroeffnungssignatur}\n\n"
        "- Nach der Eröffnungssignatur (siehe oben) KEIN zusätzliches \"Hallo "
        "zusammen\" oder \"hier sind die Meldungen des Tages\". Geh direkt aus "
        "der Signatur in den Hook über: eine überraschende Frage, ein "
        "plastisches Szenario oder eine Zahl, die den Hörer sofort betrifft. "
        "Keine weitere Anmoderation zwischen Signatur und Hook.\n\n"
        "- Jedes Thema ist eine Mini-Geschichte mit drei Teilen:\n"
        "  1. Ein konkretes, vorstellbares Bild oder Szenario, das den Hörer "
        "betrifft - eine reale Alltagssituation, in die du direkt hineinspringst. "
        "Kein \"Unternehmen könnten betroffen sein\" - ein konkretes Beispiel, "
        "das nachvollziehbar ist (darf erfunden/typisch sein, muss aber "
        "plastisch sein).\n"
        "  2. Was tatsächlich passiert ist - der Fakt, kurz und präzise.\n"
        "  3. Was das konkret für den Hörer heißt, mit einem klaren "
        "Handlungsschritt.\n\n"
        "WICHTIG: Beginne einen Themenblock NICHT mit \"Stell dir vor...\" oder "
        "\"Kennst du das...\" - das wurde in den letzten Folgen bereits mehrfach "
        "verwendet und wirkt dadurch formelhaft. Variiere stattdessen bewusst: "
        "manchmal eine überraschende Zahl direkt am Anfang, manchmal ein "
        "Kontrast/eine Überraschung (\"Ihr würdet nicht erwarten, dass "
        "ausgerechnet...\"), manchmal eine direkte Frage an den Hörer, manchmal "
        "eine kurze plakative Behauptung, die dann aufgelöst wird, manchmal ein "
        "Alltagsszenario, das direkt in der Situation beginnt ohne "
        "Ankündigungsformel (z.B. \"Montagmorgen, das Telefon klingelt...\"). "
        "\"Stell dir vor\" darf in einer ganzen Episode höchstens EINMAL "
        "vorkommen, wenn überhaupt.\n\n"
        "- Schließe jeden Themenblock mit einer kurzen, direkten Frage an den "
        "Hörer ab, die zum Nachdenken oder Handeln anregt.\n\n"
        "- Wiederhole NICHT bei jedem Thema dasselbe Muster. Variiere den Aufbau: "
        "manche Abschnitte enden mit einer direkten Handlungsaufforderung statt "
        "einer Frage an den Hörer, manche starten mit einer überraschenden Zahl "
        "statt einem Szenario. Die Hörer sollen nicht vorhersehen können, wie der "
        "nächste Abschnitt endet.\n\n"
        "- Wiederhole NICHT die exakt gleiche Übergangsformulierung zwischen Fakt "
        "und Handlungsempfehlung (z.B. \"Was heißt das konkret für...\"). "
        "Variiere das bei jedem Thema neu - manchmal ein direkter Imperativ ohne "
        "Ankündigung, manchmal eine kurze Feststellung, manchmal ein "
        "Kontrast-Satz. Kein Thema soll denselben Übergangssatz wie ein "
        "vorheriges nutzen.\n\n"
        "- Zwischen den Themen: echte Übergänge, keine reine Aneinanderreihung. "
        "Nutze inhaltliche Brücken (\"Und weil wir gerade bei Sicherheit sind...\") "
        "oder Kontraste (\"Ganz anders sieht es bei...\").\n\n"
        "- Kurzer, ebenso packender Abschluss am Ende - keine Standard-"
        "Verabschiedungsfloskel.\n\n"
        "HUMOR:\n\n"
        "- Baue an passenden Stellen trockenen, lakonischen Humor ein - keine "
        "Kalauer, kein Slapstick, sondern der Humor eines aufmerksamen "
        "Beobachters, der die Ironie einer Situation sieht. Zum Beispiel: ein "
        "trockener Kommentar, wenn eine KI-Firma ein Problem löst, das sie selbst "
        "mitverursacht hat, oder eine leicht überspitzte, aber treffende "
        "Formulierung für eine kuriose Situation. Nutze das NICHT bei ernsten "
        "Themen wie Sicherheitslücken mit akutem Handlungsbedarf oder "
        "rechtlichen Fristen - dort bleibst du sachlich und dringlich. Der Humor "
        "darf niemals Fakten, Zahlen oder Namen verfälschen oder verharmlosen. "
        "Setze ihn sparsam ein, maximal bei zwei bis drei der Themen, nicht bei "
        "allen.\n\n"
        "Gib NUR den reinen Manuskripttext zurück, ohne Regieanweisungen, "
        "Kapitelüberschriften oder Markdown-Formatierung. Hänge danach als GANZ "
        "LETZTE Zeile exakt in diesem Format an (kein zusätzlicher Text, keine "
        "Erklärung):\n"
        "VERWENDETE_THEMEN_IDS: <id1>,<id2>,...\n"
        "- die IDs (aus den [ID: ...]-Markierungen oben) der Themen, die du "
        "tatsächlich verwendet hast."
    )
    if zusatz_anweisung:
        prompt += f"\n\n--- Zusätzliche Anweisung für diesen Durchlauf ---\n{zusatz_anweisung}"
    return prompt


_IDS_ZEILE = re.compile(r"^VERWENDETE_THEMEN_IDS:\s*(.*)$", re.MULTILINE)


def erstelle_manuskript(
    supabase,
    chat_model,
    persona: str,
    themen_block: str,
    zusatz_anweisung: str | None,
    eroeffnungssignatur: str,
    format_hinweis: str = "",
    wochenrueckblick_block: str = "",
    lauf_id: str | None = None,
    episode_id: str | None = None,
) -> tuple[str, list[str]]:
    prompt = baue_manuskript_prompt(
        persona, themen_block, zusatz_anweisung, eroeffnungssignatur, format_hinweis, wochenrueckblick_block
    )
    rohantwort = chat_model.generate_content(prompt)

    kosten_tracking.logge_api_kosten(
        supabase,
        dienst="gemini",
        modell=CHAT_MODEL,
        schritt="manuskript_erstellung",
        einheit_typ="tokens",
        menge_input=rohantwort.usage_metadata.prompt_token_count,
        menge_output=rohantwort.usage_metadata.candidates_token_count,
        lauf_id=lauf_id,
        episode_id=episode_id,
    )

    antwort = rohantwort.text.strip()

    treffer = _IDS_ZEILE.search(antwort)
    if not treffer:
        print("WARNUNG: Keine VERWENDETE_THEMEN_IDS-Zeile in der Antwort gefunden.")
        return antwort, []

    manuskripttext = antwort[: treffer.start()].strip()
    verwendete_ids = [teil.strip() for teil in treffer.group(1).split(",") if teil.strip()]
    return manuskripttext, verwendete_ids


def erstelle_episode(
    zusatz_anweisung: str | None = None, format: str | None = None, lauf_id: str | None = None
) -> dict | None:
    supabase = hole_supabase_client()
    chat_model = hole_chat_model()

    heute = datetime.now(timezone.utc)
    aktives_format = bestimme_format(format, heute)
    samstag, sonntag = hole_wochenende_daten(heute)
    print(f"Format: {aktives_format}.")

    persona = hole_moderator_persona(supabase)

    themen = hole_offene_themen(supabase)
    if not themen:
        print(f"Keine offenen Themen (Status {OFFENE_STATUS}) gefunden.")
        return None

    print(f"{len(themen)} offene(s) Thema/Themen gefunden:")
    for t in themen:
        print(f'  - "{t["titel"]}" (Status: {t["status"]}, {len(t["updates"])} Update(s))')
    print()

    themen_block = baue_themen_block(themen, mit_daten=(aktives_format == "montag"))

    wochenrueckblick_block = ""
    if aktives_format == "freitag":
        wochenstart_iso = hole_wochenstart(heute).isoformat()
        themen_woche = hole_themen_der_woche(supabase, wochenstart_iso)
        wochenrueckblick_block = baue_wochenrueckblick_block(themen_woche)

    format_hinweis = baue_format_hinweis(aktives_format, samstag, sonntag)
    eroeffnungssignatur = baue_eroeffnungssignatur(aktives_format)

    # Episode-Zeile wird VOR dem Gemini-Call angelegt (noch ohne Manuskript),
    # damit die Manuskript-Kosten (der teuerste Einzelschritt einer Episode)
    # direkt mit episode_id in api_kosten geloggt werden koennen.
    jetzt = datetime.now(timezone.utc).isoformat()
    episode = (
        supabase.table("episoden")
        .insert({"datum": jetzt, "manuskripttext": None})
        .execute()
        .data[0]
    )
    episode_id = episode["id"]

    print("Erzeuge Manuskript...")
    manuskripttext, verwendete_ids = erstelle_manuskript(
        supabase,
        chat_model,
        persona,
        themen_block,
        zusatz_anweisung,
        eroeffnungssignatur,
        format_hinweis,
        wochenrueckblick_block,
        lauf_id=lauf_id,
        episode_id=episode_id,
    )
    print(f"-> Manuskript erzeugt ({len(manuskripttext)} Zeichen).\n")

    episode = (
        supabase.table("episoden")
        .update({"manuskripttext": manuskripttext})
        .eq("id", episode_id)
        .execute()
        .data[0]
    )
    print(f'-> Episode gespeichert (id={episode["id"]}).')

    bekannte_ids = {t["id"] for t in themen}
    gueltige_ids = [tid for tid in verwendete_ids if tid in bekannte_ids]
    if not gueltige_ids:
        print(
            "WARNUNG: Konnte die verwendeten Themen nicht sicher bestimmen - "
            "es wurde KEIN Thema als 'gesendet' markiert. Bitte manuell prüfen "
            "und Status ggf. per Hand setzen.\n"
        )
    else:
        supabase.table("themen").update({"status": "gesendet"}).in_(
            "id", gueltige_ids
        ).execute()
        print(f"-> {len(gueltige_ids)} Thema/Themen als 'gesendet' markiert.\n")

    themen_nach_id = {t["id"]: t for t in themen}
    episode["verwendete_themen"] = [themen_nach_id[tid] for tid in gueltige_ids]

    return episode


def hole_quellen_fuer_themen(supabase, thema_ids: list[str]) -> dict[str, list[dict]]:
    """Sammelt für jede thema_id die verknüpften Original-Rohnachrichten über
    redaktion_entscheidungen -> agent_vorschlaege -> rohnachrichten."""
    quellen_nach_thema: dict[str, list[dict]] = {tid: [] for tid in thema_ids}
    if not thema_ids:
        return quellen_nach_thema

    entscheidungen = (
        supabase.table("redaktion_entscheidungen")
        .select("thema_id, vorschlag_id")
        .in_("thema_id", thema_ids)
        .execute()
        .data
    )
    if not entscheidungen:
        return quellen_nach_thema

    vorschlag_ids = list({e["vorschlag_id"] for e in entscheidungen if e["vorschlag_id"]})
    vorschlaege = (
        supabase.table("agent_vorschlaege")
        .select("id, rohnachricht_id")
        .in_("id", vorschlag_ids)
        .execute()
        .data
        if vorschlag_ids
        else []
    )
    rohnachricht_id_nach_vorschlag = {v["id"]: v["rohnachricht_id"] for v in vorschlaege}

    rohnachricht_ids = list({rid for rid in rohnachricht_id_nach_vorschlag.values() if rid})
    rohnachrichten = (
        supabase.table("rohnachrichten")
        .select("id, titel, text")
        .in_("id", rohnachricht_ids)
        .execute()
        .data
        if rohnachricht_ids
        else []
    )
    rohnachricht_nach_id = {r["id"]: r for r in rohnachrichten}

    gesehen: set[tuple[str, str]] = set()
    for e in entscheidungen:
        thema_id = e["thema_id"]
        rohnachricht = rohnachricht_nach_id.get(rohnachricht_id_nach_vorschlag.get(e["vorschlag_id"]))
        if not rohnachricht or thema_id not in quellen_nach_thema:
            continue
        schluessel = (thema_id, rohnachricht["id"])
        if schluessel in gesehen:
            continue
        gesehen.add(schluessel)
        quellen_nach_thema[thema_id].append(rohnachricht)

    return quellen_nach_thema


def baue_quellen_block(themen: list[dict], quellen_nach_thema: dict[str, list[dict]]) -> str:
    bloecke = []
    for t in themen:
        quellen = quellen_nach_thema.get(t["id"], [])
        if quellen:
            quellentext = "\n\n".join(f'Quelle "{q["titel"]}":\n{q["text"]}' for q in quellen)
        else:
            quellentext = t.get("zusammenfassung") or "(keine Quelle gefunden)"
        bloecke.append(f'=== Thema: {t["titel"]} ===\n{quellentext}')
    return "\n\n".join(bloecke)


def baue_faktencheck_prompt(manuskripttext: str, quellen_block: str) -> str:
    return (
        "Du bist Fakten-Checker für einen Podcast. Prüfe jede konkrete Zahl, "
        "jeden Eigennamen (Personen, Firmen, Produkte) und jede Datumsangabe im "
        "folgenden Manuskript gegen die beigefügten Original-Quellen.\n\n"
        "Für jede solche konkrete Behauptung entscheide:\n"
        '- "bestaetigt": steht so oder so ähnlich in den Quellen\n'
        '- "widerspruch": widerspricht den Quellen (z.B. andere Zahl, anderer Name, '
        "anderes Datum)\n"
        '- "nicht_belegt": lässt sich in den Quellen nicht finden (kann ein bewusst '
        "erfundenes Beispiel/Szenario im Storytelling sein, nicht zwingend ein Fehler)\n\n"
        "Ignoriere reine Stilmittel, erfundene Alltagsszenarien/Beispiele, die klar "
        "illustrativ sind und keine konkrete Zahl/keinen Namen/kein Datum enthalten, "
        "sowie Meinungs- oder Humor-Passagen des Moderators.\n\n"
        f"QUELLEN:\n{quellen_block}\n\n"
        f"MANUSKRIPT:\n{manuskripttext}\n\n"
        "Antworte NUR mit einer JSON-Liste, jedes Element in diesem Format:\n"
        '{"behauptung": string, "status": "bestaetigt"|"widerspruch"|"nicht_belegt", '
        '"quelle_thema": string}'
    )


def pruefe_manuskript(
    episode_id: str, manuskripttext: str, themen: list[dict], lauf_id: str | None = None
) -> dict:
    supabase = hole_supabase_client()
    chat_model = hole_chat_model()

    thema_ids = [t["id"] for t in themen]
    quellen_nach_thema = hole_quellen_fuer_themen(supabase, thema_ids)
    quellen_block = baue_quellen_block(themen, quellen_nach_thema)

    prompt = baue_faktencheck_prompt(manuskripttext, quellen_block)
    antwort = chat_model.generate_content(
        prompt, generation_config={"response_mime_type": "application/json"}
    )

    kosten_tracking.logge_api_kosten(
        supabase,
        dienst="gemini",
        modell=CHAT_MODEL,
        schritt="faktencheck",
        einheit_typ="tokens",
        menge_input=antwort.usage_metadata.prompt_token_count,
        menge_output=antwort.usage_metadata.candidates_token_count,
        lauf_id=lauf_id,
        episode_id=episode_id,
    )

    details = json.loads(antwort.text)

    zaehler = {"bestaetigt": 0, "widerspruch": 0, "nicht_belegt": 0}
    for d in details:
        status = d.get("status")
        if status not in zaehler:
            continue
        zaehler[status] += 1
        if status == "widerspruch":
            print(f'  WIDERSPRUCH: "{d.get("behauptung")}" (Thema: {d.get("quelle_thema")})')
        elif status == "nicht_belegt":
            print(f'  nicht belegt: "{d.get("behauptung")}" (Thema: {d.get("quelle_thema")})')

    ergebnis = {**zaehler, "details": details}
    neuer_status = "pruefung_fehlgeschlagen" if zaehler["widerspruch"] > 0 else "freigegeben"

    supabase.table("episoden").update(
        {"faktencheck_ergebnis": ergebnis, "status": neuer_status}
    ).eq("id", episode_id).execute()

    print(
        f'-> Faktencheck: {zaehler["bestaetigt"]} bestätigt, {zaehler["widerspruch"]} '
        f'Widerspruch/Widersprüche, {zaehler["nicht_belegt"]} nicht belegt -> '
        f'Status "{neuer_status}".\n'
    )

    return ergebnis


if __name__ == "__main__":
    format_override = None
    rest_argumente = []
    for arg in sys.argv[1:]:
        if arg.startswith("format="):
            format_override = arg.split("=", 1)[1]
        else:
            rest_argumente.append(arg)

    zusatz_anweisung = rest_argumente[0] if rest_argumente else None
    erstelle_episode(zusatz_anweisung, format=format_override)
