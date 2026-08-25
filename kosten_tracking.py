"""Zentrale Kosten-Erfassung für alle externen API-Aufrufe (Gemini, Deepgram,
ElevenLabs).

logge_api_kosten() berechnet die geschätzten Kosten eines API-Aufrufs anhand
der PREISTABELLE unten und speichert einen Eintrag in "api_kosten".
zaehle_tokens() liefert die echte Tokenzahl für Texte, bei denen die
Gemini-API selbst keine usage_metadata mitliefert (z.B. embed_content).
hole_kosten_summe() aggregiert bestehende Einträge nach lauf_id/episode_id.

Voraussetzung: Migration 20260825090000_api_kosten.sql muss angewendet sein.
"""
import google.generativeai as genai

# ==========================================================================
# PREISTABELLE — muss manuell aktuell gehalten werden!
# Alle Preise in USD pro 1.000.000 Einheiten (Token oder Zeichen).
# Stand: Wissensstand zum Zeitpunkt der Erstellung, NICHT live geprüft.
# Vor produktivem Einsatz und danach regelmäßig gegen die aktuellen
# Preisseiten der Anbieter abgleichen:
#   - Gemini:      https://ai.google.dev/gemini-api/docs/pricing
#   - Deepgram:    https://developers.deepgram.com/docs/pricing
#   - ElevenLabs:  https://elevenlabs.io/pricing (hängt stark vom Tarif ab!)
# ==========================================================================
PREISTABELLE = {
    "gemini": {
        # input/output = USD pro 1 Mio. Tokens
        "gemini-2.0-flash": {"input": 0.10, "output": 0.40},
        "gemini-2.0-flash-lite": {"input": 0.075, "output": 0.30},
        "gemini-2.5-flash": {"input": 0.30, "output": 2.50},
        "gemini-2.5-flash-lite": {"input": 0.10, "output": 0.40},
        "gemini-2.5-pro": {"input": 1.25, "output": 10.00},
        "gemini-embedding-001": {"input": 0.15, "output": 0.0},
        # Aktuell in .env eingetragenes Modell (GEMINI_MODEL_NAME) - Preis
        # NICHT verifiziert, bitte sobald bekannt durch den echten Wert ersetzen:
        "gemini-3.5-flash-lite": {"input": 0.10, "output": 0.40},
    },
    "deepgram": {
        # USD pro 1 Mio. Zeichen (Aura-2 TTS)
        "aura-2-julius-de": {"input": 30.00, "output": 0.0},
    },
    "elevenlabs": {
        # USD pro 1 Mio. Zeichen - stark tarifabhängig, hier nur ein grober
        # Richtwert. Bitte gegen den tatsächlich gebuchten Tarif prüfen!
        "eleven_multilingual_v2": {"input": 180.00, "output": 0.0},
    },
}

# Fallback falls dienst/modell nicht in der PREISTABELLE steht (z.B. neues
# Modell) - verhindert einen Crash, druckt aber eine Warnung, damit es auffällt.
STANDARD_PREIS = {"input": 0.0, "output": 0.0}


def _hole_preise(dienst: str, modell: str) -> dict:
    preise = PREISTABELLE.get(dienst, {}).get(modell)
    if preise is None:
        print(
            f'WARNUNG: Kein Preis für dienst="{dienst}", modell="{modell}" in der '
            "PREISTABELLE (kosten_tracking.py) hinterlegt - Kosten werden als 0 "
            "berechnet. Bitte Preis dort ergänzen."
        )
        return STANDARD_PREIS
    return preise


def logge_api_kosten(
    supabase,
    dienst: str,
    modell: str,
    schritt: str,
    einheit_typ: str,
    menge_input: int,
    menge_output: int | None = None,
    lauf_id: str | None = None,
    episode_id: str | None = None,
) -> float:
    """Berechnet die geschätzten Kosten anhand der PREISTABELLE und speichert
    einen Eintrag in "api_kosten". Gibt die berechneten Kosten (USD) zurück."""
    preise = _hole_preise(dienst, modell)
    kosten = (menge_input / 1_000_000) * preise["input"]
    if menge_output:
        kosten += (menge_output / 1_000_000) * preise["output"]
    kosten = round(kosten, 6)

    supabase.table("api_kosten").insert(
        {
            "dienst": dienst,
            "modell": modell,
            "schritt": schritt,
            "einheit_typ": einheit_typ,
            "menge_input": menge_input,
            "menge_output": menge_output,
            "geschaetzte_kosten_usd": kosten,
            "lauf_id": lauf_id,
            "episode_id": episode_id,
        }
    ).execute()

    return kosten


def zaehle_tokens(modell: str, text: str) -> int:
    """Ermittelt die echte Tokenzahl für `text` über einen kostenlosen
    count_tokens()-Call. Für Aufrufe wie embed_content, die selbst keine
    usage_metadata liefern. Voraussetzung: genai.configure() wurde vom
    Aufrufer bereits ausgeführt. Fällt bei Fehlern auf eine grobe Schätzung
    (Zeichenzahl / 4) zurück, damit die Kosten-Erfassung nicht an einem
    einzelnen fehlgeschlagenen count_tokens()-Call scheitert."""
    try:
        antwort = genai.GenerativeModel(modell).count_tokens(text)
        return antwort.total_tokens
    except Exception as e:
        geschaetzt = max(1, len(text) // 4)
        print(
            f"WARNUNG: count_tokens() fehlgeschlagen ({type(e).__name__}: {e}), "
            f"nutze grobe Schätzung ({geschaetzt} Tokens)."
        )
        return geschaetzt


def hole_kosten_summe(supabase, lauf_id: str | None = None, episode_id: str | None = None) -> float:
    """Summiert geschaetzte_kosten_usd aus "api_kosten", gefiltert nach
    lauf_id und/oder episode_id (mindestens eines von beiden angeben)."""
    query = supabase.table("api_kosten").select("geschaetzte_kosten_usd")
    if lauf_id:
        query = query.eq("lauf_id", lauf_id)
    if episode_id:
        query = query.eq("episode_id", episode_id)
    eintraege = query.execute().data
    return round(sum(e["geschaetzte_kosten_usd"] for e in eintraege), 6)
