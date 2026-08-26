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
