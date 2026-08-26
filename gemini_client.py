"""Kleine Adapter-Schicht fuer das aktuelle Google Gen AI SDK."""
import os

from google import genai
from google.genai import types


_client: genai.Client | None = None


def _neuer_client():
    """Liefert einen ueber das Modul hinweg wiederverwendeten Client.
    WICHTIG: NIE genai.Client(...) als anonymes Ausdrucksergebnis direkt
    verketten (z.B. "genai.Client(...).models.generate_content(...)") - ohne
    eigene Referenz raeumt Python das Objekt per Refcounting sofort wieder
    auf und schliesst dabei den internen httpx-Client, bevor die Anfrage
    rausgeht ("RuntimeError: Cannot send a request, as the client has been
    closed."). Deshalb hier modulweit gecacht statt pro Aufruf neu gebaut."""
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return _client


class GeminiModell:
    """Bewahrt die bisherige generate_content-Schnittstelle intern kompatibel."""

    def __init__(self, modellname: str, client=None):
        self.modellname = modellname
        self._client = client

    @property
    def client(self):
        if self._client is None:
            self._client = _neuer_client()
        return self._client

    def generate_content(self, prompt: str, generation_config: dict | None = None):
        config = types.GenerateContentConfig(**generation_config) if generation_config else None
        return self.client.models.generate_content(
            model=self.modellname, contents=prompt, config=config
        )


def erzeuge_embedding(
    modell: str, text: str, task_type: str, output_dimensionality: int
) -> list[float]:
    antwort = _neuer_client().models.embed_content(
        model=modell.removeprefix("models/"),
        contents=text,
        config=types.EmbedContentConfig(
            task_type=task_type, output_dimensionality=output_dimensionality
        ),
    )
    if not antwort.embeddings or antwort.embeddings[0].values is None:
        raise RuntimeError("Gemini lieferte kein Embedding.")
    return list(antwort.embeddings[0].values)


def zaehle_tokens(modell: str, text: str) -> int:
    antwort = _neuer_client().models.count_tokens(
        model=modell.removeprefix("models/"), contents=text
    )
    return int(antwort.total_tokens)


def erzeuge_tts_audio(modell: str, text: str, voice_name: str) -> tuple[bytes, int, int]:
    """Erzeugt Sprachaudio ueber Gemini TTS. Gibt (pcm_bytes, input_tokens,
    output_tokens) zurueck: rohe PCM-Daten (24kHz, 16-bit, mono -
    unkomprimiert, ohne Container - der Aufrufer muss sie selbst in ein
    abspielbares Format bringen) sowie die tatsaechlich abgerechneten
    Tokenzahlen direkt aus der API-Antwort (usage_metadata), keine
    Schaetzung. Siehe https://ai.google.dev/gemini-api/docs/speech-generation."""
    antwort = _neuer_client().models.generate_content(
        model=modell,
        contents=text,
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_name)
                )
            ),
        ),
    )
    teile = antwort.candidates[0].content.parts if antwort.candidates else None
    if not teile or teile[0].inline_data is None:
        raise RuntimeError("Gemini TTS lieferte keine Audiodaten.")
    pcm_bytes = teile[0].inline_data.data
    if not pcm_bytes:
        raise RuntimeError("Gemini TTS lieferte leere Audiodaten.")
    usage = antwort.usage_metadata
    input_tokens = int(usage.prompt_token_count or 0) if usage else 0
    output_tokens = int(usage.candidates_token_count or 0) if usage else 0
    return bytes(pcm_bytes), input_tokens, output_tokens
