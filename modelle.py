"""Zentrale, aufgabenspezifische Modellzuordnung der Podcast-Pipeline."""
import os


STANDARD_SCHNELLES_MODELL = "gemini-2.5-flash-lite"
STANDARD_QUALITAETS_MODELL = "gemini-2.5-pro"


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
