"""Erzeugt eine MP3-Audiodatei über Gemini TTS, Deepgram oder ElevenLabs.

Wird zusätzlich eine episode_id übergeben, lädt text_zu_audio() die lokal
gespeicherte MP3 danach automatisch in den öffentlichen Supabase-Storage-
Bucket "episoden-audio" hoch (Dateiname = episode_id + ".mp3") und gibt die
öffentliche URL zurück - schlägt der Upload fehl, wird das nur als
Konsolen-Warnung gemeldet, kein Fehler; die lokale Datei bleibt in jedem
Fall die verlässliche Ausgabe. Voraussetzung: Migration
20260825204516_episoden_audio_url.sql muss angewendet sein, sowie der
Bucket "episoden-audio" (öffentlich lesbar) muss existieren.
"""
import os
import re
import sys
import tempfile

import lameenc
from deepgram import DeepgramClient
from dotenv import load_dotenv
from elevenlabs import ElevenLabs
from supabase import create_client

from gemini_client import erzeuge_tts_audio
import kosten_tracking

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()


def hole_supabase_client():
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

AUDIO_BUCKET = "episoden-audio"
DEEPGRAM_MODEL = "aura-2-julius-de"
DEEPGRAM_SPEED_STANDARD = 1.15  # etwas schneller/lebendiger als normal (erlaubter Bereich: 0.7-1.5)
DEEPGRAM_SPEED_MIN = 0.7
DEEPGRAM_SPEED_MAX = 1.5
# Deepgram unterstützt den speed-Parameter (Stand 2026) nur bei aktualisierten
# englischen Aura-2 Voice-Packs. Bei anderen Sprachen (u.a. Deutsch) lehnt die
# API JEDE Anfrage mit speed-Parameter mit 400 Bad Request ab - unabhängig vom
# Wert. Sobald Deepgram das für Deutsch freischaltet, reicht es, hier "de" zu
# ergänzen.
DEEPGRAM_SPEED_SPRACHEN = ("en",)
# Deepgram lehnt Anfragen mit mehr als 2000 Zeichen Text mit 413 ab - für ein
# komplettes Episoden-Manuskript reicht das so gut wie nie.
DEEPGRAM_TEXT_LIMIT = 2000
ELEVENLABS_VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"
ELEVENLABS_MODEL = "eleven_multilingual_v2"

# Gemini 3.1 Flash TTS (Preview, Stand 2026-08-26 recherchiert:
# https://ai.google.dev/gemini-api/docs/speech-generation,
# https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-tts-preview).
# Seit 2026-08-26 Standard-Anbieter (siehe text_zu_audio/morgenlauf.py) -
# Deepgram bleibt als Fallback über anbieter="deepgram" nutzbar.
GEMINI_TTS_MODEL = "gemini-3.1-flash-tts-preview"
# "Charon" = laut Google als "Informative" beschrieben - passt zu einem
# sachlichen Nachrichtenformat. Alternativen fürs Probehören: "Rasalgethi"
# (ebenfalls "Informative") oder "Sadaltager" ("Knowledgeable").
GEMINI_TTS_VOICE = "Charon"
# Gemini TTS kann die Sprechweise über eine natürliche Regieanweisung im
# Eingabetext steuern. Charon soll motiviert und präsent klingen, ohne die
# Seriosität eines Nachrichtenformats zu verlieren oder hektisch zu werden.
GEMINI_TTS_SPRECHSTIL = (
    "Sprich den folgenden deutschen Nachrichtentext motiviert, engagiert und "
    "mit spürbar positiver Energie. Betone die wichtigsten Aussagen klar und "
    "abwechslungsreich. Bleibe dabei professionell, glaubwürdig und ruhig genug "
    "für ein seriöses Nachrichtenformat; sprich weder monoton noch überdreht."
)
# Antwort ist rohes PCM: 24kHz, 16-bit, mono - wird direkt im Speicher (ohne
# WAV-Zwischendatei) über lameenc zu MP3 konvertiert, siehe _pcm_zu_mp3().
# lameenc statt ffmpeg/pydub: reines Python-Wheel (bindet den LAME-Encoder
# ein), kein System-Binary nötig - ffmpeg ist weder auf GitHub-Actions
# ubuntu-latest-Runnern noch lokal vorinstalliert (Stand 2026-08-26 geprüft).
GEMINI_TTS_SAMPLE_RATE = 24000
GEMINI_TTS_MP3_BITRATE = 128  # kbps, gleicher Wert wie ELEVENLABS_MODEL-Output
# Kein festes Zeichenlimit dokumentiert, aber ein Kontextfenster von 32k
# Tokens (8192 Input/16384 Output) sowie der Hinweis, dass die Qualität bei
# durchgehenden Ausgaben über wenigen Minuten driften kann. 3000 Zeichen
# entsprechen bei deutscher Sprechgeschwindigkeit ca. 2-3 Minuten Audio -
# bleibt mit großem Puffer unter beiden Token-Grenzen und in der von Google
# empfohlenen Länge. Gleiche Chunking-Logik wie bei Deepgram (_teile_text).
GEMINI_TTS_TEXT_LIMIT = 3000


def _unterstuetzt_speed(model: str) -> bool:
    return any(model.endswith(f"-{sprache}") for sprache in DEEPGRAM_SPEED_SPRACHEN)


_URL = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_UEBERSCHRIFT = re.compile(r"^#{1,6}\s+|^[A-ZÄÖÜ0-9][A-ZÄÖÜ0-9 /&–-]{3,}:?$")
_ABKUERZUNGEN = {
    "z. B.": "zum Beispiel",
    "d. h.": "das heißt",
    "u. a.": "unter anderem",
    "KMU": "kleine und mittlere Unternehmen",
    "KI": "K I",
    "EU": "E U",
}


def bereite_tts_text_auf(text: str) -> str:
    """Entfernt nicht sprechbare Elemente und setzt hörbare Abschnittspausen."""
    zeilen = []
    for rohzeile in text.splitlines():
        zeile = _URL.sub("", rohzeile).strip()
        if not zeile:
            if zeilen and zeilen[-1] != "":
                zeilen.append("")
            continue
        ist_ueberschrift = bool(_UEBERSCHRIFT.match(zeile))
        zeile = re.sub(r"^#{1,6}\s+", "", zeile).rstrip(":")
        for kurz, gesprochen in _ABKUERZUNGEN.items():
            zeile = zeile.replace(kurz, gesprochen)
        zeile = re.sub(r"(\d+(?:[.,]\d+)?)\s*%", r"\1 Prozent", zeile)
        zeile = re.sub(r"€\s*(\d+(?:[.,]\d+)?)", r"\1 Euro", zeile)
        zeile = re.sub(r"(\d+(?:[.,]\d+)?)\s*€", r"\1 Euro", zeile)
        if ist_ueberschrift:
            if zeilen and zeilen[-1] != "":
                zeilen.append("")
            zeile = f"{zeile}."
        zeilen.append(zeile)
        if ist_ueberschrift:
            zeilen.append("")
    return "\n".join(zeilen).strip()


def _teile_langen_satz(satz: str, max_laenge: int) -> list[str]:
    if len(satz) <= max_laenge:
        return [satz]
    teile = re.split(r"(?<=[,;:–-])\s+", satz)
    if any(len(teil) > max_laenge for teil in teile):
        raise ValueError("Ein einzelner Satz ist länger als das TTS-Limit und nicht sicher teilbar.")
    return teile


def _teile_text(text: str, max_laenge: int = DEEPGRAM_TEXT_LIMIT) -> list[str]:
    text = bereite_tts_text_auf(text)
    saetze = []
    for abschnitt in re.split(r"\n\s*\n", text):
        abschnitt_saetze = re.split(r"(?<=[.!?])\s+", abschnitt.strip())
        saetze.extend(teil for satz in abschnitt_saetze for teil in _teile_langen_satz(satz, max_laenge))
        if saetze:
            saetze[-1] += "\n\n"
    chunks: list[str] = []
    aktueller = ""
    for satz in saetze:
        kandidat = f"{aktueller} {satz}".strip() if aktueller else satz
        if len(kandidat) > max_laenge and aktueller:
            chunks.append(aktueller)
            aktueller = satz
        else:
            aktueller = kandidat
    if aktueller:
        chunks.append(aktueller.strip())
    return chunks


def _via_deepgram(text: str, dateipfad: str, speed: float = DEEPGRAM_SPEED_STANDARD) -> None:
    if not DEEPGRAM_SPEED_MIN <= speed <= DEEPGRAM_SPEED_MAX:
        raise ValueError(
            f"speed muss zwischen {DEEPGRAM_SPEED_MIN} und {DEEPGRAM_SPEED_MAX} liegen, war {speed}."
        )

    speed_wird_gesendet = _unterstuetzt_speed(DEEPGRAM_MODEL)
    if not speed_wird_gesendet and speed != DEEPGRAM_SPEED_STANDARD:
        raise ValueError(
            f'Deepgram unterstützt den speed-Parameter aktuell nur bei englischen Aura-2-Stimmen, '
            f'nicht bei "{DEEPGRAM_MODEL}". speed={speed} kann daher nicht angewendet werden.'
        )
    if not speed_wird_gesendet:
        print(
            f'Hinweis: "{DEEPGRAM_MODEL}" unterstützt aktuell keine speed-Kontrolle bei Deepgram '
            "(nur englische Aura-2-Stimmen) - wird ohne speed erzeugt."
        )

    client = DeepgramClient(api_key=os.environ["DEEPGRAM_API_KEY"])

    chunks = _teile_text(text)
    if len(chunks) > 1:
        print(
            f"Text hat {len(text)} Zeichen (Limit pro Anfrage: {DEEPGRAM_TEXT_LIMIT}), "
            f"wird in {len(chunks)} Teile aufgeteilt."
        )

    audio_teile = []
    for chunk in chunks:
        if speed_wird_gesendet:
            audio_teile.append(
                b"".join(client.speak.v1.audio.generate(text=chunk, model=DEEPGRAM_MODEL, speed=speed))
            )
        else:
            audio_teile.append(b"".join(client.speak.v1.audio.generate(text=chunk, model=DEEPGRAM_MODEL)))

    _schreibe_atomar(dateipfad, b"".join(audio_teile))


def _schreibe_atomar(dateipfad: str, audio_bytes: bytes) -> None:
    """Veröffentlicht die Zieldatei erst, nachdem alle Audioblöcke vorliegen."""
    zielordner = os.path.dirname(os.path.abspath(dateipfad))
    os.makedirs(zielordner, exist_ok=True)
    temp_pfad = None
    try:
        with tempfile.NamedTemporaryFile(dir=zielordner, suffix=".mp3.tmp", delete=False) as tmp:
            temp_pfad = tmp.name
            tmp.write(audio_bytes)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(temp_pfad, dateipfad)
    finally:
        if temp_pfad and os.path.exists(temp_pfad):
            os.unlink(temp_pfad)


def _via_elevenlabs(text: str, dateipfad: str) -> None:
    client = ElevenLabs(api_key=os.environ["ELEVENLABS_API_KEY"])
    audio_bytes = b"".join(
        client.text_to_speech.convert(
            voice_id=ELEVENLABS_VOICE_ID,
            text=bereite_tts_text_auf(text),
            model_id=ELEVENLABS_MODEL,
            output_format="mp3_44100_128",
        )
    )

    _schreibe_atomar(dateipfad, audio_bytes)


def _pcm_zu_mp3(pcm_bytes: bytes) -> bytes:
    """Kodiert rohe PCM-Daten (siehe GEMINI_TTS_SAMPLE_RATE, 16-bit/mono)
    direkt im Speicher zu MP3 - keine WAV-Zwischendatei, kein ffmpeg."""
    if not pcm_bytes:
        raise RuntimeError("Gemini TTS lieferte leere PCM-Daten; MP3-Konvertierung abgebrochen.")
    if len(pcm_bytes) % 2:
        raise RuntimeError(
            "Gemini TTS lieferte unvollständige 16-Bit-PCM-Daten; "
            "MP3-Konvertierung abgebrochen."
        )

    encoder = lameenc.Encoder()
    encoder.set_bit_rate(GEMINI_TTS_MP3_BITRATE)
    encoder.set_in_sample_rate(GEMINI_TTS_SAMPLE_RATE)
    encoder.set_channels(1)
    encoder.set_quality(2)  # 2 = hohe Qualität (LAME: 0=beste/langsamste .. 9=schnellste)
    mp3_bytes = encoder.encode(pcm_bytes)
    mp3_bytes += encoder.flush()
    mp3_bytes = bytes(mp3_bytes)
    if not mp3_bytes:
        raise RuntimeError("MP3-Konvertierung lieferte keine Daten.")
    return mp3_bytes


def _via_gemini_tts(text: str, dateipfad: str) -> tuple[int, int]:
    """Erzeugt Audio über Gemini TTS und schreibt direkt eine MP3-Datei -
    das rohe PCM aus der API wird vollständig im Speicher zu MP3 konvertiert
    (_pcm_zu_mp3), es entsteht nie eine WAV-Zwischendatei auf der Platte.
    Gibt (input_tokens, output_tokens) für die Kostenerfassung zurück -
    echte Werte aus der API-Antwort (usage_metadata), keine Schätzung."""
    chunks = _teile_text(text, max_laenge=GEMINI_TTS_TEXT_LIMIT)
    if len(chunks) > 1:
        print(
            f"Text hat {len(text)} Zeichen (Richtwert pro Anfrage: {GEMINI_TTS_TEXT_LIMIT}), "
            f"wird in {len(chunks)} Teile aufgeteilt."
        )

    pcm_teile = []
    input_tokens = 0
    output_tokens = 0
    for chunk in chunks:
        pcm, chunk_input_tokens, chunk_output_tokens = erzeuge_tts_audio(
            GEMINI_TTS_MODEL,
            chunk,
            GEMINI_TTS_VOICE,
            GEMINI_TTS_SPRECHSTIL,
        )
        pcm_teile.append(pcm)
        input_tokens += chunk_input_tokens
        output_tokens += chunk_output_tokens

    mp3_bytes = _pcm_zu_mp3(b"".join(pcm_teile))
    _schreibe_atomar(dateipfad, mp3_bytes)
    return input_tokens, output_tokens


def lade_audio_hoch(supabase, dateipfad: str, episode_id: str) -> str | None:
    """Lädt die lokale MP3-Datei zusätzlich in den Supabase-Storage-Bucket
    AUDIO_BUCKET hoch (Dateiname = episode_id + ".mp3") und gibt die
    öffentliche URL zurück. Schlägt der Upload fehl (Netzwerk, Bucket fehlt
    etc.), wird NICHT geworfen - nur eine Konsolen-Warnung, die lokale Datei
    bleibt in jedem Fall die verlässliche Ausgabe."""
    pfad_im_bucket = f"{episode_id}.mp3"
    try:
        supabase.storage.from_(AUDIO_BUCKET).upload(
            path=pfad_im_bucket,
            file=dateipfad,
            file_options={"content-type": "audio/mpeg", "upsert": "true"},
        )
        url = supabase.storage.from_(AUDIO_BUCKET).get_public_url(pfad_im_bucket)
        print(f"-> Audio zusätzlich in Supabase Storage hochgeladen: {url}")
        return url
    except Exception as e:
        print(
            f"WARNUNG: Upload nach Supabase Storage fehlgeschlagen "
            f"({type(e).__name__}: {e}) - lokale Datei bleibt maßgeblich."
        )
        return None


def text_zu_audio(
    text: str,
    dateipfad: str,
    anbieter: str = "gemini_tts",
    speed: float = DEEPGRAM_SPEED_STANDARD,
    lauf_id: str | None = None,
    episode_id: str | None = None,
) -> str | None:
    print(f'Erzeuge Audio über "{anbieter}" -> {dateipfad}')

    if anbieter == "deepgram":
        _via_deepgram(text, dateipfad, speed=speed)
        modell = DEEPGRAM_MODEL
    elif anbieter == "elevenlabs":
        _via_elevenlabs(text, dateipfad)
        modell = ELEVENLABS_MODEL
    elif anbieter == "gemini_tts":
        gemini_tts_tokens = _via_gemini_tts(text, dateipfad)
        modell = GEMINI_TTS_MODEL
    else:
        raise ValueError(
            f'Unbekannter Anbieter "{anbieter}". Erlaubt: "deepgram", "elevenlabs", "gemini_tts".'
        )

    if anbieter == "gemini_tts":
        # Gemini TTS wird pro Token abgerechnet (Text-Input UND Audio-Output),
        # nicht pro Zeichen wie Deepgram/ElevenLabs - siehe PREISTABELLE.
        # Echte Tokenzahlen aus der API-Antwort, siehe _via_gemini_tts.
        text_tokens, audio_tokens = gemini_tts_tokens
        kosten_tracking.logge_api_kosten(
            hole_supabase_client(),
            dienst=anbieter,
            modell=modell,
            schritt="audio_synthese",
            einheit_typ="tokens",
            menge_input=text_tokens,
            menge_output=audio_tokens,
            lauf_id=lauf_id,
            episode_id=episode_id,
        )
    else:
        # TTS hat sonst kein Output-Token-Konzept - getrackt wird die Anzahl
        # der übergebenen Zeichen als menge_input.
        kosten_tracking.logge_api_kosten(
            hole_supabase_client(),
            dienst=anbieter,
            modell=modell,
            schritt="audio_synthese",
            einheit_typ="zeichen",
            menge_input=len(text),
            lauf_id=lauf_id,
            episode_id=episode_id,
        )

    print(f"Audio gespeichert: {dateipfad}")

    audio_url = None
    if episode_id:
        audio_url = lade_audio_hoch(hole_supabase_client(), dateipfad, episode_id)

    return audio_url


if __name__ == "__main__":
    text_zu_audio("Das ist ein Test.", "test_audio.mp3")
