"""Erzeugt denselben Beispieltext mit 4 unterschiedlichen Deepgram Aura-2
Stimmen zum Vergleich, jeweils gespeichert unter output/stimme_1.mp3 bis
output/stimme_4.mp3.

Stimmen-Auswahl (recherchiert in der Deepgram-Doku, alle deutsch verfügbar,
https://developers.deepgram.com/docs/tts-models):
- Viktoria: energetisch (Charismatic, Cheerful, Enthusiastic, Friendly, Warm)
- Lara:     warm/freundlich (Caring, Cheerful, Empathetic, Expressive, Warm)
- Fabian:   professionell/sachlich (Confident, Knowledgeable, Natural, Polite, Professional)
- Julius:   mehr Persönlichkeit/Drive (Casual, Cheerful, Engaging, Expressive, Friendly)
"""
import os
import sys

from deepgram import DeepgramClient
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()

BEISPIELTEXT = (
    "Guten Morgen und willkommen zu eurem KI-Update. Heute geht's um drei "
    "spannende Entwicklungen aus der Welt der künstlichen Intelligenz."
)

OUTPUT_ORDNER = "output"

STIMMEN = [
    {
        "dateiname": "stimme_1.mp3",
        "model": "aura-2-viktoria-de",
        "name": "Viktoria",
        "charakter": "energetisch (Charismatic, Cheerful, Enthusiastic, Friendly, Warm)",
    },
    {
        "dateiname": "stimme_2.mp3",
        "model": "aura-2-lara-de",
        "name": "Lara",
        "charakter": "warm/freundlich (Caring, Cheerful, Empathetic, Expressive, Warm)",
    },
    {
        "dateiname": "stimme_3.mp3",
        "model": "aura-2-fabian-de",
        "name": "Fabian",
        "charakter": "professionell/sachlich (Confident, Knowledgeable, Natural, Polite, Professional)",
    },
    {
        "dateiname": "stimme_4.mp3",
        "model": "aura-2-julius-de",
        "name": "Julius",
        "charakter": "mehr Persönlichkeit/Drive (Casual, Cheerful, Engaging, Expressive, Friendly)",
    },
]


def erzeuge_stimme(client: DeepgramClient, text: str, model: str, dateipfad: str) -> None:
    audio_bytes = b"".join(client.speak.v1.audio.generate(text=text, model=model))
    with open(dateipfad, "wb") as f:
        f.write(audio_bytes)


def main() -> None:
    os.makedirs(OUTPUT_ORDNER, exist_ok=True)
    client = DeepgramClient(api_key=os.environ["DEEPGRAM_API_KEY"])

    print(f'Beispieltext: "{BEISPIELTEXT}"\n')

    for stimme in STIMMEN:
        dateipfad = os.path.join(OUTPUT_ORDNER, stimme["dateiname"])
        print(f'Erzeuge "{stimme["name"]}" ({stimme["model"]}) -> {dateipfad}...')
        erzeuge_stimme(client, BEISPIELTEXT, stimme["model"], dateipfad)
        print(f'-> {dateipfad}: {stimme["name"]} - {stimme["charakter"]}\n')

    print("Fertig. Übersicht:")
    for stimme in STIMMEN:
        dateipfad = os.path.join(OUTPUT_ORDNER, stimme["dateiname"])
        print(f'  {dateipfad}: {stimme["name"]} ({stimme["model"]}) - {stimme["charakter"]}')


if __name__ == "__main__":
    main()
