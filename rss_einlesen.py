"""Liest RSS-Feeds aus und speichert neue Einträge in "rohnachrichten".

Für jeden konfigurierten Feed werden nur Einträge der letzten
MAX_ALTER_TAGE Tage berücksichtigt (basierend auf dem Veröffentlichungs-
datum im Feed, falls vorhanden). Einträge, deren URL bereits in
"rohnachrichten" existiert, werden übersprungen.
"""
import os
from datetime import datetime, timedelta, timezone

import feedparser
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

MAX_ALTER_TAGE = 3

FEEDS = [
    ("THE DECODER", "https://the-decoder.de/feed/"),
    ("T3N", "https://t3n.de/rss.xml"),
    ("HEISE", "https://www.heise.de/rss/heise-atom.xml"),
    ("OPENAI", "https://openai.com/blog/rss.xml"),
    ("GOLEM", "https://www.golem.de/rss.php?feed=RSS2.0"),
    ("NETZPOLITIK", "https://netzpolitik.org/feed/"),
    ("GRÜNDERSZENE", "https://www.gruenderszene.de/feed"),
    ("HANDELSBLATT", "https://www.handelsblatt.com/contentexport/feed/technologie"),
]


def hole_supabase_client():
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def hole_veroeffentlichungsdatum(eintrag) -> datetime | None:
    zeitstruktur = eintrag.get("published_parsed") or eintrag.get("updated_parsed")
    if not zeitstruktur:
        return None
    return datetime(*zeitstruktur[:6], tzinfo=timezone.utc)


def hole_zusammenfassung(eintrag) -> str:
    return eintrag.get("summary", "").strip()


def url_existiert(supabase, url: str) -> bool:
    ergebnis = supabase.table("rohnachrichten").select("id").eq("url", url).limit(1).execute()
    return bool(ergebnis.data)


def verarbeite_feed(supabase, quelle: str, feed_url: str, grenze: datetime) -> tuple[int, int]:
    eingefuegt = 0
    uebersprungen = 0

    geparst = feedparser.parse(feed_url)

    for eintrag in geparst.entries:
        url = eintrag.get("link")
        if not url:
            continue

        veroeffentlicht = hole_veroeffentlichungsdatum(eintrag)
        if veroeffentlicht and veroeffentlicht < grenze:
            continue

        if url_existiert(supabase, url):
            uebersprungen += 1
            continue

        supabase.table("rohnachrichten").insert(
            {
                "quelle": quelle,
                "url": url,
                "titel": eintrag.get("title", ""),
                "text": hole_zusammenfassung(eintrag),
                "abrufzeitpunkt": datetime.now(timezone.utc).isoformat(),
            }
        ).execute()
        eingefuegt += 1

    return eingefuegt, uebersprungen


def main():
    supabase = hole_supabase_client()
    grenze = datetime.now(timezone.utc) - timedelta(days=MAX_ALTER_TAGE)

    print(f"Lese {len(FEEDS)} Feeds ein (nur Einträge der letzten {MAX_ALTER_TAGE} Tage)...\n")

    gesamt_eingefuegt = 0
    gesamt_uebersprungen = 0

    for quelle, feed_url in FEEDS:
        print(f"Feed: {quelle} ({feed_url})")
        try:
            eingefuegt, uebersprungen = verarbeite_feed(supabase, quelle, feed_url, grenze)
        except Exception as e:
            print(f"-> Fehler beim Verarbeiten: {e}\n")
            continue

        gesamt_eingefuegt += eingefuegt
        gesamt_uebersprungen += uebersprungen
        print(f"-> {eingefuegt} neu eingefügt, {uebersprungen} übersprungen (bereits vorhanden)\n")

    print(f"Fertig. Insgesamt {gesamt_eingefuegt} neue Einträge, {gesamt_uebersprungen} übersprungen.")


if __name__ == "__main__":
    main()
