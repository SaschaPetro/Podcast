"""Kleine Adapter-Schicht fuer das aktuelle Google Gen AI SDK."""
import os
import time

from google import genai
from google.genai import errors as genai_errors
from google.genai import types


_client: genai.Client | None = None

# Siehe Morgenlauf #12 (2026-08-27): "This model is currently experiencing
# high demand" (503 UNAVAILABLE) brach die gesamte Episode ab, ohne dass
# ueberhaupt ein zweiter Versuch unternommen wurde. 5xx-Serverfehler und 429
# Rate-Limit sind typischerweise voruebergehend - alle anderen Fehler (400
# Bad Request, 403, ...) sind dauerhaft und werden weiterhin sofort
# durchgereicht (Retry wuerde nur Zeit kosten, ohne das Ergebnis zu aendern).
RETRY_VERSUCHE = 3
RETRY_BASIS_SEKUNDEN = 5


def _ist_transienter_fehler(e: Exception) -> bool:
    if isinstance(e, genai_errors.ServerError):
        return True
    if isinstance(e, genai_errors.ClientError) and getattr(e, "code", None) == 429:
        return True
    return False


def _mit_retry(aufruf):
    """Fuehrt `aufruf` (ein no-arg Callable) aus und wiederholt bei
    transienten Gemini-Fehlern bis zu RETRY_VERSUCHE mal mit exponentiellem
    Backoff (RETRY_BASIS_SEKUNDEN, RETRY_BASIS_SEKUNDEN*2, ...). Nicht-
    transiente Fehler und der letzte Versuch werden unveraendert weitergereicht."""
    letzter_fehler: Exception | None = None
    for versuch in range(1, RETRY_VERSUCHE + 1):
        try:
            return aufruf()
        except Exception as e:
            letzter_fehler = e
            if not _ist_transienter_fehler(e) or versuch == RETRY_VERSUCHE:
                raise
            wartezeit = RETRY_BASIS_SEKUNDEN * (2 ** (versuch - 1))
            print(
                f"WARNUNG: Gemini-Anfrage fehlgeschlagen ({type(e).__name__}: {e}), "
                f"Versuch {versuch}/{RETRY_VERSUCHE}, warte {wartezeit}s..."
            )
            time.sleep(wartezeit)
    raise letzter_fehler  # pragma: no cover - Schleife verlaesst immer per return/raise


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
    """generate_content() nimmt zusaetzlich zum bestehenden Kurzzeit-Retry
    (_mit_retry, gleiches Modell) eine zweite Schicht Modell-Fallback: `modell`
    kann eine Kette (primaeres Modell + Ausweich-Modelle, siehe
    modelle.schnelle_modell_kette()/qualitaets_modell_kette()) statt eines
    einzelnen Modellnamens sein. Erschoepft der Kurzzeit-Retry sich fuer ein
    Modell trotzdem (Kontingent, abgeschaltetes Modell, ...), wird automatisch
    das naechste Modell der Kette probiert, bevor endgueltig aufgegeben wird -
    damit legt ein einzelnes erschoepftes Kontingent nie die ganze Pipeline
    lahm. `aktuelles_modell` zeigt nach einem erfolgreichen Call, welches
    Modell tatsaechlich geantwortet hat (fuer Kosten-Logging o.ae.)."""

    def __init__(self, modell: str | list[str], client=None):
        self.modellkette = [modell] if isinstance(modell, str) else list(modell)
        self._client = client
        self.aktuelles_modell: str | None = None

    @property
    def modellname(self) -> str:
        """Rueckwaertskompatibel: das primaere (erste) Modell der Kette."""
        return self.modellkette[0]

    @property
    def client(self):
        if self._client is None:
            self._client = _neuer_client()
        return self._client

    def generate_content(self, prompt: str, generation_config: dict | None = None):
        config = types.GenerateContentConfig(**generation_config) if generation_config else None
        letzter_fehler: Exception | None = None
        for i, kandidat in enumerate(self.modellkette):
            try:
                antwort = _mit_retry(
                    lambda kandidat=kandidat: self.client.models.generate_content(
                        model=kandidat, contents=prompt, config=config
                    )
                )
                self.aktuelles_modell = kandidat
                return antwort
            except Exception as e:
                letzter_fehler = e
                if i + 1 < len(self.modellkette):
                    naechstes = self.modellkette[i + 1]
                    print(
                        f'WARNUNG: Modell "{kandidat}" nicht nutzbar '
                        f"({type(e).__name__}: {e}), wechsle zu \"{naechstes}\"..."
                    )
        raise letzter_fehler


def erzeuge_embedding(
    modell: str, text: str, task_type: str, output_dimensionality: int
) -> list[float]:
    antwort = _mit_retry(
        lambda: _neuer_client().models.embed_content(
            model=modell.removeprefix("models/"),
            contents=text,
            config=types.EmbedContentConfig(
                task_type=task_type, output_dimensionality=output_dimensionality
            ),
        )
    )
    if not antwort.embeddings or antwort.embeddings[0].values is None:
        raise RuntimeError("Gemini lieferte kein Embedding.")
    return list(antwort.embeddings[0].values)


def zaehle_tokens(modell: str, text: str) -> int:
    antwort = _mit_retry(
        lambda: _neuer_client().models.count_tokens(
            model=modell.removeprefix("models/"), contents=text
        )
    )
    return int(antwort.total_tokens)


def erzeuge_tts_audio(
    modell: str,
    text: str,
    voice_name: str,
    sprechstil: str | None = None,
) -> tuple[bytes, int, int]:
    """Erzeugt Sprachaudio ueber Gemini TTS. Gibt (pcm_bytes, input_tokens,
    output_tokens) zurueck: rohe PCM-Daten (24kHz, 16-bit, mono -
    unkomprimiert, ohne Container - der Aufrufer muss sie selbst in ein
    abspielbares Format bringen) sowie die tatsaechlich abgerechneten
    Tokenzahlen direkt aus der API-Antwort (usage_metadata), keine
    Schaetzung. Siehe https://ai.google.dev/gemini-api/docs/speech-generation."""
    inhalt = f"{sprechstil.strip()}\n\n{text}" if sprechstil else text
    antwort = _mit_retry(
        lambda: _neuer_client().models.generate_content(
            model=modell,
            contents=inhalt,
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_name)
                    )
                ),
            ),
        )
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
