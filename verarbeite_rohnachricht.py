"""Verarbeitet einen einzelnen Rohtext (z.B. aus "rohnachrichten"):

1. Embedding für den Text erzeugen (Gemini text-embedding-004)
2. Über finde_aehnliche_themen (Schwellenwert 0.85) nach ähnlichem Thema suchen
3. Falls ähnliches Thema gefunden: Gemini 2.0 Flash fragen, ob es einen neuen
   Fakt gibt -> Update anlegen, oder Text als Duplikat verwerfen
4. Falls kein ähnliches Thema gefunden: neues Thema anlegen

Voraussetzung: Migration 20260824095747_embedding_dim_768_gemini.sql muss
angewendet sein (Spalte "embedding" ist dann vector(768) statt vector(1536)).
"""
import json
import os
import sys
from datetime import datetime, timezone

import google.generativeai as genai
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

EMBEDDING_MODEL = "models/gemini-embedding-001"
EMBEDDING_DIM = 768
CHAT_MODEL = "gemini-3.6-flash"
SCHWELLENWERT = 0.85

genai.configure(api_key=os.environ["GEMINI_API_KEY"])
supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
chat_model = genai.GenerativeModel(CHAT_MODEL)


def erzeuge_embedding(text: str) -> list[float]:
    # Gleicher task_type wie im Backfill, da dasselbe Embedding sowohl für den
    # Ähnlichkeitsvergleich als auch (falls kein Treffer) für die Neuanlage
    # eines Themas verwendet wird.
    antwort = genai.embed_content(
        model=EMBEDDING_MODEL,
        content=text,
        task_type="retrieval_document",
        output_dimensionality=EMBEDDING_DIM,
    )
    return antwort["embedding"]


def hole_letztes_update(thema_id: str) -> str | None:
    ergebnis = (
        supabase.table("themen_updates")
        .select("was_neu, datum")
        .eq("thema_id", thema_id)
        .order("datum", desc=True)
        .limit(1)
        .execute()
    )
    if ergebnis.data:
        return ergebnis.data[0]["was_neu"]
    return None


def pruefe_auf_neuigkeit(text: str, thema: dict, letztes_update: str | None) -> dict:
    stand = thema.get("zusammenfassung") or ""
    if letztes_update:
        stand += f"\nLetztes Update: {letztes_update}"

    prompt = (
        f'Bestehender Stand zum Thema "{thema["titel"]}":\n'
        f"{stand}\n\n"
        f"Neuer Text:\n{text}\n\n"
        "Gibt es hier einen konkreten neuen Fakt (Zahl, Datum, Entscheidung, Name), "
        "der noch nicht im bestehenden Stand erwähnt ist? "
        'Antworte NUR mit JSON: {"hat_neuigkeit": bool, "neuigkeit_text": string oder null}'
    )

    antwort = chat_model.generate_content(
        prompt,
        generation_config={"response_mime_type": "application/json"},
    )
    return json.loads(antwort.text)


def verarbeite_text(text: str) -> None:
    anzeige = text if len(text) <= 80 else text[:80] + "..."
    print(f'Verarbeite Text: "{anzeige}"')

    embedding = erzeuge_embedding(text)
    print("Embedding erzeugt.")

    treffer = supabase.rpc(
        "finde_aehnliche_themen",
        {"such_embedding": embedding, "schwellenwert": SCHWELLENWERT},
    ).execute()
    aehnliche_themen = treffer.data

    if aehnliche_themen:
        bestes = aehnliche_themen[0]
        thema_id = bestes["id"]
        aehnlichkeit = bestes["aehnlichkeit"]
        print(
            f'Ähnlichkeit {aehnlichkeit:.2f} zu Thema "{bestes["titel"]}" '
            f'gefunden (Status: {bestes["status"]}). Prüfe auf neue Fakten...'
        )

        thema_voll = (
            supabase.table("themen")
            .select("id, titel, zusammenfassung, status")
            .eq("id", thema_id)
            .single()
            .execute()
            .data
        )
        letztes_update = hole_letztes_update(thema_id)

        pruefung = pruefe_auf_neuigkeit(text, thema_voll, letztes_update)

        if pruefung.get("hat_neuigkeit"):
            neuigkeit_text = pruefung.get("neuigkeit_text")
            print(f"-> Neue Info erkannt: {neuigkeit_text}")

            jetzt = datetime.now(timezone.utc).isoformat()
            supabase.table("themen_updates").insert(
                {"thema_id": thema_id, "was_neu": neuigkeit_text, "datum": jetzt}
            ).execute()
            supabase.table("themen").update({"letztes_update": jetzt}).eq("id", thema_id).execute()
            print(f'-> Update zu Thema "{thema_voll["titel"]}" gespeichert.')
        else:
            print("-> Keine neue Info erkannt, Text ist Duplikat und wird verworfen.")
    else:
        print(f"Kein ähnliches Thema gefunden (Schwellenwert {SCHWELLENWERT}). Lege neues Thema an.")

        titel = text.strip().splitlines()[0][:120] if text.strip() else "Unbenanntes Thema"
        jetzt = datetime.now(timezone.utc).isoformat()

        supabase.table("themen").insert(
            {
                "titel": titel,
                "zusammenfassung": text,
                "erster_kontaktzeitpunkt": jetzt,
                "letztes_update": jetzt,
                "status": "neu",
                "embedding": embedding,
            }
        ).execute()
        print(f'-> Neues Thema angelegt: "{titel}"')


if __name__ == "__main__":
    beispieltext = sys.argv[1] if len(sys.argv) > 1 else "Beispieltext zum Testen."
    verarbeite_text(beispieltext)
