"""Erzeugt eine Audiodatei aus Text, wahlweise über Deepgram oder ElevenLabs."""
import os

from deepgram import DeepgramClient
from dotenv import load_dotenv
from elevenlabs import ElevenLabs

load_dotenv()

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
ELEVENLABS_VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"
ELEVENLABS_MODEL = "eleven_multilingual_v2"


def _unterstuetzt_speed(model: str) -> bool:
    return any(model.endswith(f"-{sprache}") for sprache in DEEPGRAM_SPEED_SPRACHEN)


def _via_deepgram(text: str, dateipfad: str, speed: float = DEEPGRAM_SPEED_STANDARD) -> None:
    if not DEEPGRAM_SPEED_MIN <= speed <= DEEPGRAM_SPEED_MAX:
        raise ValueError(
            f"speed muss zwischen {DEEPGRAM_SPEED_MIN} und {DEEPGRAM_SPEED_MAX} liegen, war {speed}."
        )

    client = DeepgramClient(api_key=os.environ["DEEPGRAM_API_KEY"])

    if _unterstuetzt_speed(DEEPGRAM_MODEL):
        audio_bytes = b"".join(
            client.speak.v1.audio.generate(text=text, model=DEEPGRAM_MODEL, speed=speed)
        )
    else:
        if speed != DEEPGRAM_SPEED_STANDARD:
            raise ValueError(
                f'Deepgram unterstützt den speed-Parameter aktuell nur bei englischen Aura-2-Stimmen, '
                f'nicht bei "{DEEPGRAM_MODEL}". speed={speed} kann daher nicht angewendet werden.'
            )
        print(
            f'Hinweis: "{DEEPGRAM_MODEL}" unterstützt aktuell keine speed-Kontrolle bei Deepgram '
            "(nur englische Aura-2-Stimmen) - wird ohne speed erzeugt."
        )
        audio_bytes = b"".join(client.speak.v1.audio.generate(text=text, model=DEEPGRAM_MODEL))

    with open(dateipfad, "wb") as f:
        f.write(audio_bytes)


def _via_elevenlabs(text: str, dateipfad: str) -> None:
    client = ElevenLabs(api_key=os.environ["ELEVENLABS_API_KEY"])
    audio_bytes = b"".join(
        client.text_to_speech.convert(
            voice_id=ELEVENLABS_VOICE_ID,
            text=text,
            model_id=ELEVENLABS_MODEL,
            output_format="mp3_44100_128",
        )
    )

    with open(dateipfad, "wb") as f:
        f.write(audio_bytes)


def text_zu_audio(
    text: str, dateipfad: str, anbieter: str = "deepgram", speed: float = DEEPGRAM_SPEED_STANDARD
) -> None:
    print(f'Erzeuge Audio über "{anbieter}" -> {dateipfad}')

    if anbieter == "deepgram":
        _via_deepgram(text, dateipfad, speed=speed)
    elif anbieter == "elevenlabs":
        _via_elevenlabs(text, dateipfad)
    else:
        raise ValueError(f'Unbekannter Anbieter "{anbieter}". Erlaubt: "deepgram", "elevenlabs".')

    print(f"Audio gespeichert: {dateipfad}")


if __name__ == "__main__":
    text_zu_audio("Das ist ein Test.", "test_audio.mp3")
