"""Zentrale, aufgabenspezifische Modellzuordnung der Podcast-Pipeline."""
import os


STANDARD_SCHNELLES_MODELL = "gemini-3.5-flash-lite"
# UEBERGANGSLOESUNG (2026-08-26): gemini-2.5-pro ist fuer neue Nutzer
# abgeschaltet, der von Google vorgeschlagene Ersatz gemini-3.1-pro-preview
# hat fuer den aktuellen GEMINI_API_KEY ein Free-Tier-Kontingent von 0
# (Billing/Plan-Frage, kein Code-Problem). gemini-3.5-flash ist bestaetigt
# im Free Tier erreichbar, aber KEIN Pro-Modell - Manuskriptqualitaet bis
# zur Klaerung des Billing-Themas ggf. schlechter als mit einem echten
# Pro-Modell. Sobald Billing/Plan geklaert ist: auf gemini-3.1-pro-preview
# (oder aktuelleres Pro-Modell) zurueckstellen.
STANDARD_QUALITAETS_MODELL = "gemini-3.5-flash"

# Fallback-Ketten fuer automatischen Modellwechsel bei erschoepftem Kontingent
# (429) oder abgeschaltetem/deprecatetem Modell (404 o.ae.) - siehe
# gemini_client.GeminiModell. Jedes Modell hat ein eigenes Free-Tier-Kontingent,
# ein Wechsel umgeht also ein erschoepftes Kontingent des vorherigen Modells.
# Stand 2026-08-27 gegen https://ai.google.dev/gemini-api/docs/pricing und
# https://ai.google.dev/gemini-api/docs/models geprueft (nicht geraten):
# gemini-2.0-flash-lite ist laut Modell-Seite "shut down" und deshalb NICHT
# Teil der Kette. Der aeltere Kommentar/Preis-Hinweis, gemini-2.5-flash-lite
# sei "fuer neue Nutzer abgeschaltet" (siehe kosten_tracking.py), liess sich
# an dieser Stelle nicht bestaetigen - Modell ist laut aktueller Doku wieder
# regulaer als stabil und frei gelistet.
FALLBACK_SCHNELLES_MODELL = [
    "gemini-3.5-flash-lite",  # heutiger Standard, unveraendert primaer
    "gemini-3.1-flash-lite",  # eine Generation aelter, eigenes Kontingent
    "gemini-2.5-flash-lite",  # zwei Generationen aelter, eigenes Kontingent
]
FALLBACK_QUALITAETS_MODELL = [
    "gemini-3.5-flash",  # heutiges Uebergangs-Qualitaetsmodell, unveraendert primaer
    "gemini-3.6-flash",  # neuere Generation, ebenfalls frei+stabil
    "gemini-2.5-flash",  # aeltere, sehr etablierte Generation
]


def schnelles_modell() -> str:
    """Klassifikation, Auswahl, Normalisierung und Faktencheck."""
    return os.getenv("GEMINI_FAST_MODEL") or STANDARD_SCHNELLES_MODELL


def qualitaetsmodell() -> str:
    """Ausschliesslich die qualitaetskritische Manuskripterstellung."""
    return os.getenv("GEMINI_QUALITY_MODEL") or STANDARD_QUALITAETS_MODELL


def modell_fuer(schritt: str) -> str:
    if schritt == "manuskript_erstellung":
        return qualitaetsmodell()
    return schnelles_modell()


def _kette_mit_primaer(primaer: str, eingebaute_kette: list[str]) -> list[str]:
    """Setzt `primaer` (z.B. per Env-Var uebersteuert) an die erste Stelle und
    haengt den Rest der eingebauten Kette als Sicherheitsnetz dahinter - ein
    Env-Override ersetzt also nie den automatischen Fallback, sondern nur die
    Reihenfolge, mit der die Kette beginnt."""
    rest = [m for m in eingebaute_kette if m != primaer]
    return [primaer, *rest]


def schnelle_modell_kette() -> list[str]:
    """Fallback-Kette fuer Klassifikation, Auswahl, Normalisierung, Faktencheck,
    Rhetorik-Pruefung, Notfall-Auffuellung, Update-Reaktivierung und
    Zweite-Quelle-Bewertung. Erster Eintrag = schnelles_modell()."""
    return _kette_mit_primaer(schnelles_modell(), FALLBACK_SCHNELLES_MODELL)


def qualitaets_modell_kette() -> list[str]:
    """Fallback-Kette ausschliesslich fuer die Manuskripterstellung. Erster
    Eintrag = qualitaetsmodell()."""
    return _kette_mit_primaer(qualitaetsmodell(), FALLBACK_QUALITAETS_MODELL)


def modell_kette_fuer(schritt: str) -> list[str]:
    if schritt == "manuskript_erstellung":
        return qualitaets_modell_kette()
    return schnelle_modell_kette()
