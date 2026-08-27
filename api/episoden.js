const responseHeaders = {
  "Cache-Control": "public, s-maxage=300, stale-while-revalidate=900",
  "Content-Type": "application/json; charset=utf-8",
};

async function querySupabase(path, signal) {
  const baseUrl = process.env.SUPABASE_URL;
  const apiKey = process.env.SUPABASE_KEY;

  if (!baseUrl || !apiKey) {
    throw new Error("SUPABASE_URL oder SUPABASE_KEY fehlt.");
  }

  const response = await fetch(`${baseUrl}/rest/v1/${path}`, {
    headers: {
      apikey: apiKey,
      authorization: `Bearer ${apiKey}`,
    },
    signal,
  });

  if (!response.ok) {
    throw new Error(`Supabase-Anfrage fehlgeschlagen (${response.status}).`);
  }

  return response.json();
}

export default async function handler(request, response) {
  if (request.method !== "GET") {
    response.setHeader("Allow", "GET");
    return response.status(405).json({ error: "Methode nicht erlaubt." });
  }

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 8000);

  try {
    const episodes = await querySupabase(
      "episoden?select=id,datum,manuskripttext,audio_url,status&audio_url=not.is.null&order=datum.desc&limit=24",
      controller.signal,
    );

    if (!episodes.length) {
      Object.entries(responseHeaders).forEach(([name, value]) => response.setHeader(name, value));
      return response.status(200).json({ episodes: [] });
    }

    const episodeIds = episodes.map(({ id }) => id).join(",");
    const sources = await querySupabase(
      `episoden_quellen?select=episode_id,quelle_name,quelle_url,titel&episode_id=in.(${episodeIds})&order=zeitstempel.asc`,
      controller.signal,
    );
    // Direkt der Folge zugeordnete Erzeugungskosten (Manuskript, Faktencheck,
    // Audio-Synthese) - Recherche/Redaktion-Kosten aus Takt 1 (sammellauf.py)
    // haben keine episode_id (koennen mehreren/keiner spaeteren Folge dienen)
    // und sind hier bewusst NICHT enthalten, um nichts Unbelegtes hochzurechnen.
    const kosten = await querySupabase(
      `api_kosten?select=episode_id,geschaetzte_kosten_usd&episode_id=in.(${episodeIds})`,
      controller.signal,
    );

    const result = episodes.map((episode) => ({
      ...episode,
      quellen: sources.filter((source) => source.episode_id === episode.id),
      kosten_usd: kosten
        .filter((eintrag) => eintrag.episode_id === episode.id)
        .reduce((summe, eintrag) => summe + Number(eintrag.geschaetzte_kosten_usd || 0), 0),
    }));

    Object.entries(responseHeaders).forEach(([name, value]) => response.setHeader(name, value));
    return response.status(200).json({ episodes: result });
  } catch (error) {
    console.error("Episoden konnten nicht geladen werden:", error.message);
    return response.status(503).json({ error: "Episoden sind vorübergehend nicht erreichbar." });
  } finally {
    clearTimeout(timeout);
  }
}
