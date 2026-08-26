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

from dotenv import load_dotenv
from supabase import create_client

import kosten_tracking
from gemini_client import GeminiModell, erzeuge_embedding as erzeuge_gemini_embedding
import modelle

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()

EMBEDDING_MODEL = "models/gemini-embedding-001"
EMBEDDING_MODEL_KURZ = "gemini-embedding-001"
EMBEDDING_DIM = 768
CHAT_MODEL = modelle.modell_fuer("neuigkeit_pruefung")
SCHWELLENWERT = 0.85

supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
chat_model = GeminiModell(CHAT_MODEL)


def erzeuge_embedding(text: str, lauf_id: str | None = None) -> list[float]:
    # Gleicher task_type wie im Backfill, da dasselbe Embedding sowohl für den
    # Ähnlichkeitsvergleich als auch (falls kein Treffer) für die Neuanlage
    # eines Themas verwendet wird.
    embedding = erzeuge_gemini_embedding(
        modell=EMBEDDING_MODEL,
        text=text,
        task_type="retrieval_document",
        output_dimensionality=EMBEDDING_DIM,
    )

    tokens = kosten_tracking.zaehle_tokens(EMBEDDING_MODEL, text)
    kosten_tracking.logge_api_kosten(
        supabase,
        dienst="gemini",
        modell=EMBEDDING_MODEL_KURZ,
        schritt="embedding_verarbeitung",
        einheit_typ="tokens",
        menge_input=tokens,
        lauf_id=lauf_id,
    )

    return embedding


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


def pruefe_auf_neuigkeit(
    text: str, thema: dict, letztes_update: str | None, lauf_id: str | None = None
) -> dict:
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

    kosten_tracking.logge_api_kosten(
        supabase,
        dienst="gemini",
        modell=CHAT_MODEL,
        schritt="neuigkeit_pruefung",
        einheit_typ="tokens",
        menge_input=antwort.usage_metadata.prompt_token_count,
        menge_output=antwort.usage_metadata.candidates_token_count,
        lauf_id=lauf_id,
    )

    return json.loads(antwort.text)


def verarbeite_text(text: str, lauf_id: str | None = None) -> dict:
    """Verarbeitet einen Rohtext und gibt zurück, welchem Thema er zugeordnet wurde.

    Rückgabe: {"art": "neu"|"update"|"duplikat", "thema_id": str, "titel": str}
    """
    anzeige = text if len(text) <= 80 else text[:80] + "..."
    print(f'Verarbeite Text: "{anzeige}"')

    embedding = erzeuge_embedding(text, lauf_id=lauf_id)
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

        pruefung = pruefe_auf_neuigkeit(text, thema_voll, letztes_update, lauf_id=lauf_id)

        if pruefung.get("hat_neuigkeit"):
            neuigkeit_text = pruefung.get("neuigkeit_text")
            print(f"-> Neue Info erkannt: {neuigkeit_text}")

            jetzt = datetime.now(timezone.utc).isoformat()
            supabase.table("themen_updates").insert(
                {"thema_id": thema_id, "was_neu": neuigkeit_text, "datum": jetzt}
            ).execute()
            supabase.table("themen").update({"letztes_update": jetzt}).eq("id", thema_id).execute()
            print(f'-> Update zu Thema "{thema_voll["titel"]}" gespeichert.')
            return {"art": "update", "thema_id": thema_id, "titel": thema_voll["titel"]}
        else:
            print("-> Keine neue Info erkannt, Text ist Duplikat und wird verworfen.")
            return {"art": "duplikat", "thema_id": thema_id, "titel": thema_voll["titel"]}
    else:
        print(f"Kein ähnliches Thema gefunden (Schwellenwert {SCHWELLENWERT}). Lege neues Thema an.")

        titel = text.strip().splitlines()[0][:120] if text.strip() else "Unbenanntes Thema"
        jetzt = datetime.now(timezone.utc).isoformat()

        neues_thema = (
            supabase.table("themen")
            .insert(
                {
                    "titel": titel,
                    "zusammenfassung": text,
                    "erster_kontaktzeitpunkt": jetzt,
                    "letztes_update": jetzt,
                    "status": "neu",
                    "embedding": embedding,
                }
            )
            .execute()
            .data
        )
        print(f'-> Neues Thema angelegt: "{titel}"')
        return {"art": "neu", "thema_id": neues_thema[0]["id"], "titel": titel}


if __name__ == "__main__":
    beispieltext = sys.argv[1] if len(sys.argv) > 1 else "Beispieltext zum Testen."
    verarbeite_text(beispieltext)
