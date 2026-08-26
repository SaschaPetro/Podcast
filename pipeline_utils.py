"""Gemeinsame Orchestrierungs-Infrastruktur für sammellauf.py und morgenlauf.py.

Enthält nur episode-/rhetorik-unabhängige Bausteine, die beide Takte
identisch brauchen: Schritt-Ausführung mit Logging/Exception-Handling,
Lauf-Protokoll-Verwaltung und die abschließende Konsolen-Zusammenfassung.
Reine Infrastruktur, keine Business-Logik.

Voraussetzung: Migration 20260826073502_lauf_protokoll_lauftyp.sql muss
angewendet sein (Spalte "lauftyp" in lauf_protokoll).
"""
import os
import time
import traceback
from datetime import datetime, timezone

from dotenv import load_dotenv
from supabase import create_client

load_dotenv()


def hole_supabase_client():
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def jetzt_lesbar() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def fuehre_schritt_aus(name: str, funktion, ergebnis_formatierer=None) -> dict:
    """Führt `funktion` aus, loggt Start/Ende/Dauer/Ergebnis und fängt Exceptions ab."""
    print(f"\n{'=' * 70}")
    print(f"[{name}] Start: {jetzt_lesbar()}")
    print("=" * 70)

    start = time.monotonic()
    try:
        rueckgabewert = funktion()
        dauer = time.monotonic() - start
        zusammenfassung = ergebnis_formatierer(rueckgabewert) if ergebnis_formatierer else "OK"
        print(f"[{name}] ERFOLGREICH - Dauer: {dauer:.1f}s - {zusammenfassung}")
        return {
            "name": name,
            "status": "erfolgreich",
            "dauer": dauer,
            "fehler": None,
            "ergebnis": rueckgabewert,
        }
    except Exception as e:
        dauer = time.monotonic() - start
        fehlertext = f"{type(e).__name__}: {e}"
        print(f"\n{'!' * 70}")
        print(f"[{name}] FEHLGESCHLAGEN nach {dauer:.1f}s")
        print(fehlertext)
        traceback.print_exc()
        print("!" * 70)
        return {
            "name": name,
            "status": "fehlgeschlagen",
            "dauer": dauer,
            "fehler": fehlertext,
            "ergebnis": None,
        }


def markiere_uebersprungen(name: str, grund: str) -> dict:
    print(f"\n{'=' * 70}")
    print(f"[{name}] ÜBERSPRUNGEN: {grund}")
    print("=" * 70)
    return {
        "name": name,
        "status": "uebersprungen",
        "dauer": 0.0,
        "fehler": grund,
        "ergebnis": None,
    }


def starte_lauf_protokoll(lauftyp: str) -> str | None:
    """Legt den lauf_protokoll-Eintrag VOR dem eigentlichen Lauf an (mit
    Platzhalterwerten) und gibt seine id zurück. Diese id wird als lauf_id an
    alle Schritte durchgereicht, damit api_kosten-Einträge während des Laufs
    darauf verweisen können (Fremdschlüssel-Constraint verlangt eine bereits
    existierende Zeile). `lauftyp` ist "sammellauf" oder "morgenlauf" - siehe
    lauf_protokoll.lauftyp. Wird am Ende über aktualisiere_lauf_protokoll()
    mit den echten Werten befüllt."""
    try:
        supabase = hole_supabase_client()
        eintrag = (
            supabase.table("lauf_protokoll")
            .insert({"dauer_sekunden": 0, "erfolgreich": False, "lauftyp": lauftyp})
            .execute()
            .data[0]
        )
        return eintrag["id"]
    except Exception as e:
        print(f"WARNUNG: Konnte Lauf-Protokoll nicht anlegen: {type(e).__name__}: {e}")
        return None


def aktualisiere_lauf_protokoll(
    lauf_id: str | None, dauer_sekunden: int, erfolgreich: bool, fehler_details: str | None
) -> None:
    if lauf_id is None:
        print("WARNUNG: Kein Lauf-Protokoll-Eintrag vorhanden, Abschluss wird nicht gespeichert.")
        return
    try:
        supabase = hole_supabase_client()
        supabase.table("lauf_protokoll").update(
            {
                "dauer_sekunden": dauer_sekunden,
                "erfolgreich": erfolgreich,
                "fehler_details": fehler_details,
            }
        ).eq("id", lauf_id).execute()
        print("Lauf-Protokoll aktualisiert.")
    except Exception as e:
        print(f"WARNUNG: Konnte Lauf-Protokoll nicht aktualisieren: {type(e).__name__}: {e}")


def drucke_zusammenfassung(
    schritte: list[dict],
    gesamt_dauer: float,
    gesamtkosten_usd: float | None = None,
) -> None:
    """Rein generische Schritt-/Kosten-Zusammenfassung, ohne Kenntnis von
    Episoden- oder Rhetorik-spezifischen Details. morgenlauf.py hängt nach
    dem Aufruf optional noch seinen eigenen Rhetorik-Block an (siehe dort) -
    das gehört nicht hierher, damit pipeline_utils.py generisch bleibt und
    nicht auf morgenlauf.py zurückverweisen muss."""
    print(f"\n{'#' * 70}")
    print("GESAMT-ZUSAMMENFASSUNG")
    print("#" * 70)

    symbole = {"erfolgreich": "OK ", "fehlgeschlagen": "FEHLER ", "uebersprungen": "SKIP "}
    for schritt in schritte:
        symbol = symbole[schritt["status"]]
        zeile = f"  [{symbol}] {schritt['name']} - {schritt['dauer']:.1f}s"
        if schritt["fehler"]:
            zeile += f" - {schritt['fehler']}"
        print(zeile)

    anzahl_erfolgreich = sum(1 for s in schritte if s["status"] == "erfolgreich")
    anzahl_fehlgeschlagen = sum(1 for s in schritte if s["status"] == "fehlgeschlagen")
    anzahl_uebersprungen = sum(1 for s in schritte if s["status"] == "uebersprungen")

    print(
        f"\n{anzahl_erfolgreich}/{len(schritte)} Schritte erfolgreich, "
        f"{anzahl_fehlgeschlagen} fehlgeschlagen, {anzahl_uebersprungen} übersprungen."
    )
    print(f"Gesamtlaufzeit: {gesamt_dauer:.1f}s")
    if gesamtkosten_usd is not None:
        print(f"Geschätzte API-Kosten dieses Laufs: ${gesamtkosten_usd:.4f}")
    print("#" * 70)
