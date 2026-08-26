"""Kleine Adapter-Schicht fuer das aktuelle Google Gen AI SDK."""
import os

from google import genai
from google.genai import types


def _neuer_client():
    return genai.Client(api_key=os.environ["GEMINI_API_KEY"])


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
