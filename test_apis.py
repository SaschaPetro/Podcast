import os
from dotenv import load_dotenv

load_dotenv()

# --- Tavily testen ---
print("=== Tavily Test ===")
try:
    from tavily import TavilyClient
    tavily_client = TavilyClient(api_key=os.getenv("TAVILY_API_KEY"))
    result = tavily_client.search("neueste KI Nachrichten heute")
    print("Tavily funktioniert!")
    print("Erster Treffer:", result["results"][0]["title"])
except Exception as e:
    print("Tavily-Fehler:", e)

print()

# --- Exa testen ---
print("=== Exa Test ===")
try:
    from exa_py import Exa
    exa_client = Exa(api_key=os.getenv("EXA_API_KEY"))
    result = exa_client.search("neueste KI Nachrichten heute")
    print("Exa funktioniert!")
    print("Erster Treffer:", result.results[0].title)
except Exception as e:
    print("Exa-Fehler:", e)