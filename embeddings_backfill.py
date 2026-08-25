"""Erzeugt Embeddings (Gemini text-embedding-004) für alle Themen,
bei denen die Spalte "embedding" noch leer ist, und speichert sie in Supabase.

Voraussetzung: Migration 20260824095747_embedding_dim_768_gemini.sql muss
angewendet sein (Spalte "embedding" ist dann vector(768) statt vector(1536)).
"""
import os
import sys

import google.generativeai as genai
from dotenv import load_dotenv
from supabase import create_client

import kosten_tracking

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()

EMBEDDING_MODEL = "models/gemini-embedding-001"
EMBEDDING_MODEL_KURZ = "gemini-embedding-001"
EMBEDDING_DIM = 768


def hole_supabase_client():
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def erzeuge_embedding(supabase, text: str) -> list[float]:
    antwort = genai.embed_content(
        model=EMBEDDING_MODEL,
        content=text,
        task_type="retrieval_document",
        output_dimensionality=EMBEDDING_DIM,
    )

    tokens = kosten_tracking.zaehle_tokens(EMBEDDING_MODEL, text)
    kosten_tracking.logge_api_kosten(
        supabase,
        dienst="gemini",
        modell=EMBEDDING_MODEL_KURZ,
        schritt="embedding_backfill",
        einheit_typ="tokens",
        menge_input=tokens,
    )

    return antwort["embedding"]


def main():
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
    supabase = hole_supabase_client()

    ergebnis = (
        supabase.table("themen")
        .select("id, titel, zusammenfassung")
        .is_("embedding", "null")
        .execute()
    )
    themen = ergebnis.data

    if not themen:
        print("Keine Themen ohne Embedding gefunden. Nichts zu tun.")
        return

    print(f"{len(themen)} Themen ohne Embedding gefunden. Starte Backfill...\n")

    erfolgreich = 0
    uebersprungen = 0
    fehler = 0

    for thema in themen:
        titel = thema.get("titel") or ""
        zusammenfassung = thema.get("zusammenfassung") or ""
        text = f"{titel} {zusammenfassung}".strip()

        if not text:
            uebersprungen += 1
            print(f"[{thema['id']}] Übersprungen: kein Titel und keine Zusammenfassung vorhanden.")
            continue

        try:
            embedding = erzeuge_embedding(supabase, text)
            supabase.table("themen").update({"embedding": embedding}).eq("id", thema["id"]).execute()
            erfolgreich += 1
            anzeige_titel = titel[:60] + ("..." if len(titel) > 60 else "")
            print(f"[{thema['id']}] Embedding erzeugt & gespeichert: \"{anzeige_titel}\"")
        except Exception as e:
            fehler += 1
            print(f"[{thema['id']}] Fehler beim Erzeugen/Speichern: {e}")

    print(f"\nFertig. {erfolgreich} Embeddings gespeichert, {uebersprungen} übersprungen, {fehler} Fehler.")


if __name__ == "__main__":
    main()
