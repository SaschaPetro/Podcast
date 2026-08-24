"""Erzeugt eine Audiodatei aus Text, wahlweise über Deepgram oder ElevenLabs."""
import os

from deepgram import DeepgramClient
from dotenv import load_dotenv
from elevenlabs import ElevenLabs

load_dotenv()

DEEPGRAM_MODEL = "aura-2-lara-de"
ELEVENLABS_VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"
ELEVENLABS_MODEL = "eleven_multilingual_v2"


def _via_deepgram(text: str, dateipfad: str) -> None:
    client = DeepgramClient(api_key=os.environ["DEEPGRAM_API_KEY"])
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


def text_zu_audio(text: str, dateipfad: str, anbieter: str = "deepgram") -> None:
    print(f'Erzeuge Audio über "{anbieter}" -> {dateipfad}')

    if anbieter == "deepgram":
        _via_deepgram(text, dateipfad)
    elif anbieter == "elevenlabs":
        _via_elevenlabs(text, dateipfad)
    else:
        raise ValueError(f'Unbekannter Anbieter "{anbieter}". Erlaubt: "deepgram", "elevenlabs".')

    print(f"Audio gespeichert: {dateipfad}")


if __name__ == "__main__":
    text_zu_audio("Das ist ein Test.", "test_audio.mp3")
