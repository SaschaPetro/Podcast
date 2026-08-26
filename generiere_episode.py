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

   Speichert danach für jedes tatsächlich verwendete Thema dauerhaft die
   verknüpften Original-Rohnachrichten (Quelle-Name, URL, Titel) in
   "episoden_quellen" - über denselben Datenweg wie pruefe_manuskript
   (redaktion_entscheidungen -> agent_vorschlaege -> rohnachrichten). Themen
   ohne nachvollziehbare Verknüpfung (z.B. alte Seed-/Testdaten) bekommen
   keine Zeile, nur eine Konsolen-Meldung. Voraussetzung: Migration
   20260825134000_episoden_quellen.sql muss angewendet sein.

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

Das Prompt-TEMPLATE für baue_manuskript_prompt() liegt NICHT mehr im Code,
sondern versioniert in der Tabelle "manuskript_prompt_versionen"
(hole_aktive_prompt_version() liest die Zeile mit ist_aktiv=true). Der
Template-Text enthält die Platzhalter {PERSONA}, {THEMEN_BLOCK},
{WOCHENRUECKBLICK_ABSCHNITT}, {FORMAT_HINWEIS_ABSCHNITT},
{KI_KENNZEICHNUNG_HINWEIS}, {EROEFFNUNGSSIGNATUR} (siehe PFLICHT_PLATZHALTER),
die baue_manuskript_prompt() per einfachem str.replace() befüllt -
"zusatz_anweisung" ist bewusst NICHT Teil des Templates, wird weiterhin
separat angehängt. aktiviere_prompt_version(version_nummer) aktiviert eine
bestehende Version (und deaktiviert die vorherige) - für den manuellen
Rücksprung, falls eine automatische Anpassung (siehe rhetorik_check.py)
sich als Fehlgriff herausstellt. Voraussetzung: Migration
20260826064911_manuskript_prompt_versionen.sql muss angewendet sein.
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from supabase import create_client

from gemini_client import GeminiModell
import kosten_tracking
import modelle

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()

MANUSKRIPT_MODELL = modelle.modell_fuer("manuskript_erstellung")
FAKTENCHECK_MODELL = modelle.modell_fuer("faktencheck")
OFFENE_STATUS = ("neu", "in Verfolgung")
MANUSKRIPT_ZIEL_MIN_WOERTER = 1400
MANUSKRIPT_ZIEL_MAX_WOERTER = 1600
MANUSKRIPT_HARTE_MIN_WOERTER = 1350
MANUSKRIPT_MAX_VERSUCHE = 2
MANUSKRIPT_MAX_OUTPUT_TOKENS = 8192
PFLICHT_PLATZHALTER = (
    "{PERSONA}",
    "{THEMEN_BLOCK}",
    "{WOCHENRUECKBLICK_ABSCHNITT}",
    "{FORMAT_HINWEIS_ABSCHNITT}",
    "{KI_KENNZEICHNUNG_HINWEIS}",
    "{EROEFFNUNGSSIGNATUR}",
)

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


def hole_chat_model(modellname: str = MANUSKRIPT_MODELL):
    return GeminiModell(modellname)


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


KI_KENNZEICHNUNG_SATZ = (
    "Kurzer Hinweis vorweg: Diese Folge wurde vollautomatisch mit Künstlicher "
    "Intelligenz erstellt - Recherche, Text und Stimme."
)


def baue_ki_kennzeichnung_hinweis() -> str:
    return (
        "Die ALLERERSTE Zeile des gesamten Manuskripts, noch VOR der "
        "Eröffnungssignatur (nächster Punkt), muss WORTWÖRTLICH und UNVERÄNDERT "
        "dieser Satz sein - keine Umformulierung, Kürzung, Ergänzung oder "
        f'sprachliche Anpassung an den restlichen Ton: "{KI_KENNZEICHNUNG_SATZ}" '
        "(Pflicht-Kennzeichnung für KI-generierte Audioinhalte nach Art. 50 EU AI "
        "Act - muss zu Beginn jeder Folge stehen, unabhängig vom Format). Geh "
        "danach OHNE Absatz oder Pause direkt mit der Eröffnungssignatur weiter."
    )


def hole_aktive_prompt_version(supabase) -> dict:
    treffer = (
        supabase.table("manuskript_prompt_versionen")
        .select("id, version_nummer, prompt_text")
        .eq("ist_aktiv", True)
        .limit(1)
        .execute()
        .data
    )
    if not treffer:
        raise RuntimeError(
            "Keine aktive Zeile in manuskript_prompt_versionen (ist_aktiv=true) gefunden."
        )
    return treffer[0]


def aktiviere_prompt_version(version_nummer: int, supabase=None) -> None:
    """Aktiviert die Prompt-Template-Version mit `version_nummer` (setzt ist_aktiv=true)
    und deaktiviert alle anderen Versionen. Für den manuellen Rücksprung, falls eine
    automatische Anpassung (siehe rhetorik_check.py) sich als Fehlgriff herausstellt -
    kann aber auch von der automatischen Anpassung selbst zum Aktivieren der neuen
    Version genutzt werden."""
    if supabase is None:
        supabase = hole_supabase_client()

    treffer = (
        supabase.table("manuskript_prompt_versionen")
        .select("id")
        .eq("version_nummer", version_nummer)
        .limit(1)
        .execute()
        .data
    )
    if not treffer:
        raise ValueError(f"Keine Prompt-Version mit version_nummer={version_nummer} gefunden.")

    supabase.table("manuskript_prompt_versionen").update({"ist_aktiv": False}).eq(
        "ist_aktiv", True
    ).execute()
    supabase.table("manuskript_prompt_versionen").update({"ist_aktiv": True}).eq(
        "id", treffer[0]["id"]
    ).execute()
    print(f"-> Prompt-Version {version_nummer} aktiviert.")


def baue_manuskript_prompt(
    supabase,
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
    format_hinweis_abschnitt = f"{format_hinweis}\n\n" if format_hinweis else ""

    template = hole_aktive_prompt_version(supabase)["prompt_text"]
    prompt = (
        template.replace("{PERSONA}", persona)
        .replace("{THEMEN_BLOCK}", themen_block)
        .replace("{WOCHENRUECKBLICK_ABSCHNITT}", wochenrueckblick_abschnitt)
        .replace("{FORMAT_HINWEIS_ABSCHNITT}", format_hinweis_abschnitt)
        .replace("{KI_KENNZEICHNUNG_HINWEIS}", baue_ki_kennzeichnung_hinweis())
        .replace("{EROEFFNUNGSSIGNATUR}", eroeffnungssignatur)
    )
    prompt += """

--- VERBINDLICHE REDAKTIONELLE LEITLINIE: NACHRICHTEN VOR STORYTELLING ---

Das Ergebnis ist eine Nachrichtensendung, keine Geschichte und kein Hörspiel. Der Nachrichtenkern muss bei jedem Thema innerhalb der ersten zwei Sätze klar sein: Was ist neu, wer ist betroffen und warum ist es relevant?

Baue jeden Themenblock in dieser Reihenfolge auf:
1. Die neue, belegte Nachricht in einem klaren Satz.
2. Den nötigen Kontext: Was hat zu dieser Entwicklung geführt?
3. Die konkrete Bedeutung für Unternehmen und gegebenenfalls einen Handlungsschritt.

Storytelling ist ausschließlich ein Werkzeug zur Erklärung und kommt erst NACH dem Nachrichtenkern und dem belegten Kontext. Nutze höchstens ein kurzes, sachnahes Beispiel, wenn es einen komplizierten Zusammenhang erklärt. Erfinde keine Figuren, Dialoge, Schauplätze, Tagesabläufe oder dramatischen Szenen. Beginne nicht mit einer Atmosphäre oder Spannungskurve. Halte Beispiele deutlich kürzer als die eigentliche Nachricht und kehre sofort zu den belegten Fakten zurück. Wenn kein Beispiel zum Verständnis nötig ist, verwende gar kein Storytelling.

Formuliere sachlich, direkt und hörbar. Spannung entsteht aus Relevanz, Konsequenzen und überraschenden Fakten – nicht aus Dramatisierung. Wenn eine Stilregel im übrigen Prompt dieser Leitlinie widerspricht, hat diese Leitlinie Vorrang.

SPRACHE UND DRAMATURGIE FÜR GESPROCHENES DEUTSCH:
Schreibe wie eine professionelle deutsche Nachrichtenredaktion für das Ohr. Verwende kurze, gut sprechbare Sätze, aktive Verben und konkrete Subjekte. Erkläre jeden unvermeidbaren Fachbegriff beim ersten Auftreten in einem einfachen Halbsatz. Verbinde Themen mit inhaltlich begründeten Übergängen, nicht mit Standardsätzen. Erkläre bei jedem Thema konkret, warum es für kleine und mittlere Unternehmen relevant ist. Bei einer Fortsetzung nennst du zuerst knapp den bisherigen Stand und danach klar die tatsächliche Neuigkeit.

Beginne nach Pflichtkennzeichnung und kurzer Begrüßung mit der stärksten belegten Nachricht. Ende mit einer knappen Zusammenfassung der zwei oder drei wichtigsten Folgen. Schreibe für vorgelesene Sprache, nicht wie einen Blogartikel: keine Listen, Markdown-Überschriften, Fußnoten oder vorgelesenen URLs. Quellen erscheinen vollständig in den Show Notes und werden im Manuskript nur natürlich benannt, wenn die Einordnung es erfordert.

Verboten sind unbelegte Ergänzungen sowie typische KI-Floskeln und Übertreibungen. Verwende insbesondere nicht: „In der heutigen schnelllebigen Welt“, „Es bleibt spannend“, „Die Zukunft wird zeigen“, „Ein echter Gamechanger“, „Tauchen wir ein“ und „Zusammenfassend lässt sich sagen“. Vermeide rhetorische Fragen, wiederholte Begrüßungen und immer gleiche Übergänge.

FAKTENTREUE:
Nutze ausschließlich Zahlen, Namen, Daten und konkrete Tatsachen, die im bereitgestellten Themen- und Quellenmaterial stehen. Wenn eine Information dort fehlt, lasse sie weg. Erfinde keine plausibel klingenden Details, Beispiele, Prognosen oder Handlungsempfehlungen mit Tatsachencharakter.

LÄNGE UND SENDEDAUER:
Das Manuskript muss 1.400 bis 1.600 Wörter umfassen. Das entspricht ungefähr zehn Minuten. Nutze die Länge für belegte Details, verständliche Einordnung, Folgen für Unternehmen und konkrete, aus den Quellen ableitbare Handlungsmöglichkeiten – niemals für Wiederholungen oder Füllsätze. Prüfe die Wortzahl vor der Ausgabe selbst. Unter 1.350 Wörtern ist das Manuskript unvollständig.
"""
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
        supabase, persona, themen_block, zusatz_anweisung, eroeffnungssignatur, format_hinweis, wochenrueckblick_block
    )
    antwort = ""
    for versuch in range(1, MANUSKRIPT_MAX_VERSUCHE + 1):
        versuchs_prompt = prompt
        if versuch > 1:
            bisherige_wortzahl = len(_IDS_ZEILE.sub("", antwort).strip().split())
            mindestens_fehlende_woerter = max(
                MANUSKRIPT_ZIEL_MIN_WOERTER - bisherige_wortzahl, 0
            )
            versuchs_prompt += (
                "\n\n--- ZU KURZER ENTWURF: VERBINDLICHE ÜBERARBEITUNG ---\n"
                f"Der folgende Entwurf hat nur {bisherige_wortzahl} Wörter. Überarbeite genau diesen "
                f"Entwurf zu einem vollständigen Manuskript mit {MANUSKRIPT_ZIEL_MIN_WOERTER} bis "
                f"{MANUSKRIPT_ZIEL_MAX_WOERTER} Wörtern. Gib anschließend das gesamte überarbeitete "
                f"Manuskript aus, nicht nur Ergänzungen. Füge netto mindestens {mindestens_fehlende_woerter} "
                "inhaltlich neue Wörter hinzu. Kürze dabei keine bereits vorhandene sachliche Passage. "
                "Erweitere ausschließlich die journalistische "
                "Substanz: zusätzliche belegte Details aus den oben bereitgestellten Themen, Hintergrund, "
                "Zusammenhänge, Folgen für Unternehmen und konkrete Handlungsmöglichkeiten. Verwende "
                "kein zusätzliches Storytelling, keine erfundenen Inhalte und keine Wiederholungen. "
                "Erhalte am Ende die Zeile VERWENDETE_THEMEN_IDS.\n\n"
                "BISHERIGER ENTWURF:\n"
                f"{antwort}\n"
                "--- ENDE DES BISHERIGEN ENTWURFS ---"
            )

        rohantwort = chat_model.generate_content(
            versuchs_prompt,
            generation_config={"max_output_tokens": MANUSKRIPT_MAX_OUTPUT_TOKENS},
        )
        kosten_tracking.logge_api_kosten(
            supabase,
            dienst="gemini",
            modell=MANUSKRIPT_MODELL,
            schritt="manuskript_erstellung",
            einheit_typ="tokens",
            menge_input=rohantwort.usage_metadata.prompt_token_count,
            menge_output=rohantwort.usage_metadata.candidates_token_count,
            lauf_id=lauf_id,
            episode_id=episode_id,
        )
        antwort = rohantwort.text.strip()
        manuskript_ohne_ids = _IDS_ZEILE.sub("", antwort).strip()
        wortzahl = len(manuskript_ohne_ids.split())
        print(f"-> Manuskript-Versuch {versuch}: {wortzahl} Wörter.")
        if wortzahl >= MANUSKRIPT_HARTE_MIN_WOERTER:
            break

    if len(_IDS_ZEILE.sub("", antwort).strip().split()) < MANUSKRIPT_HARTE_MIN_WOERTER:
        raise RuntimeError(
            f"Manuskript nach {MANUSKRIPT_MAX_VERSUCHE} Versuchen kürzer als "
            f"{MANUSKRIPT_HARTE_MIN_WOERTER} Wörter - Episode wird nicht gespeichert."
        )

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
    chat_model = hole_chat_model(MANUSKRIPT_MODELL)

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
    verwendete_themen = [themen_nach_id[tid] for tid in gueltige_ids]
    episode["verwendete_themen"] = verwendete_themen

    if verwendete_themen:
        speichere_episoden_quellen(supabase, episode_id, verwendete_themen)

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
        .select("id, titel, text, quelle, url")
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


def speichere_episoden_quellen(supabase, episode_id: str, themen: list[dict]) -> None:
    """Legt für jedes tatsächlich verwendete Thema die verknüpften Original-Rohnachrichten
    in episoden_quellen ab (gleicher Datenweg wie hole_quellen_fuer_themen:
    redaktion_entscheidungen -> agent_vorschlaege -> rohnachrichten). Themen ohne
    nachvollziehbare Verknüpfung (z.B. alte Seed-/Testdaten) bekommen keine Zeile,
    nur eine Konsolen-Meldung - kein Fehler."""
    thema_ids = [t["id"] for t in themen]
    quellen_nach_thema = hole_quellen_fuer_themen(supabase, thema_ids)

    zeilen = []
    for t in themen:
        quellen = quellen_nach_thema.get(t["id"], [])
        if not quellen:
            print(f'Thema "{t["titel"]}": keine Quellenverknüpfung vorhanden.')
            continue
        for q in quellen:
            zeilen.append(
                {
                    "episode_id": episode_id,
                    "thema_id": t["id"],
                    "rohnachricht_id": q["id"],
                    "quelle_name": q.get("quelle"),
                    "quelle_url": q.get("url"),
                    "titel": q.get("titel"),
                }
            )

    if zeilen:
        supabase.table("episoden_quellen").insert(zeilen).execute()
        print(f"-> {len(zeilen)} Quellen-Verknüpfung(en) in episoden_quellen gespeichert.\n")


def baue_quellen_block(themen: list[dict], quellen_nach_thema: dict[str, list[dict]]) -> str:
    bloecke = []
    for t in themen:
        quellen = quellen_nach_thema.get(t["id"], [])
        if quellen:
            quellentext = "\n\n".join(f'Quelle "{q["titel"]}":\n{q["text"]}' for q in quellen)
        else:
            quellentext = "(keine gespeicherte Originalquelle)"
        bloecke.append(f'=== Thema: {t["titel"]} ===\n{quellentext}')
    return "\n\n".join(bloecke)


def baue_faktencheck_prompt(manuskripttext: str, quellen_block: str) -> str:
    return (
        "Du bist ein strenger Fakten-Checker für einen Podcast. Extrahiere und prüfe jede "
        "konkrete Tatsachenbehauptung, insbesondere jede Zahl, jeden Eigennamen "
        "(Personen, Firmen, Produkte), jedes Datum und jede kausale Aussage im "
        "folgenden Manuskript gegen die beigefügten Original-Quellen.\n\n"
        "Für jede solche konkrete Behauptung entscheide:\n"
        '- "bestaetigt": steht so oder so ähnlich in den Quellen\n'
        '- "widerspruch": widerspricht den Quellen (z.B. andere Zahl, anderer Name, '
        "anderes Datum)\n"
        '- "nicht_belegt": lässt sich in keiner gespeicherten Originalquelle finden\n\n'
        "Bewerte nur anhand der gelieferten Quellen. Plausibilität oder Modellwissen gelten "
        "nicht als Beleg. Zerlege Sätze mit mehreren Fakten in einzelne Behauptungen. "
        "Ignoriere nur eindeutig als Meinung markierte Wertungen ohne Tatsachenkern.\n\n"
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
    if not themen:
        raise RuntimeError("Faktencheck ohne verwendete Themen nicht möglich.")

    chat_model = hole_chat_model(FAKTENCHECK_MODELL)

    thema_ids = [t["id"] for t in themen]
    quellen_nach_thema = hole_quellen_fuer_themen(supabase, thema_ids)
    fehlende_quellen = [t["titel"] for t in themen if not quellen_nach_thema.get(t["id"])]
    if fehlende_quellen:
        raise RuntimeError(
            "Faktencheck abgebrochen: keine gespeicherte Originalquelle für: "
            + ", ".join(fehlende_quellen)
        )
    quellen_block = baue_quellen_block(themen, quellen_nach_thema)

    prompt = baue_faktencheck_prompt(manuskripttext, quellen_block)
    antwort = chat_model.generate_content(
        prompt, generation_config={"response_mime_type": "application/json"}
    )

    kosten_tracking.logge_api_kosten(
        supabase,
        dienst="gemini",
        modell=FAKTENCHECK_MODELL,
        schritt="faktencheck",
        einheit_typ="tokens",
        menge_input=antwort.usage_metadata.prompt_token_count,
        menge_output=antwort.usage_metadata.candidates_token_count,
        lauf_id=lauf_id,
        episode_id=episode_id,
    )

    details = json.loads(antwort.text)
    if not isinstance(details, list) or not details:
        raise RuntimeError("Faktencheck lieferte keine prüfbaren Behauptungen; keine Freigabe möglich.")

    zaehler = {"bestaetigt": 0, "widerspruch": 0, "nicht_belegt": 0}
    for d in details:
        status = d.get("status")
        if status not in zaehler:
            raise RuntimeError(f'Faktencheck lieferte ungültigen Status: "{status}".')
        zaehler[status] += 1
        if status == "widerspruch":
            print(f'  WIDERSPRUCH: "{d.get("behauptung")}" (Thema: {d.get("quelle_thema")})')
        elif status == "nicht_belegt":
            print(f'  nicht belegt: "{d.get("behauptung")}" (Thema: {d.get("quelle_thema")})')

    ergebnis = {**zaehler, "details": details}
    blockierende_funde = zaehler["widerspruch"] + zaehler["nicht_belegt"]
    neuer_status = "pruefung_fehlgeschlagen" if blockierende_funde > 0 else "freigegeben"

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
