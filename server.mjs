import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { extname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("./public/", import.meta.url));
const port = Number(process.env.PORT || 4173);
const types = { ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml" };
const json = (res, status, body) => { res.writeHead(status, { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" }); res.end(JSON.stringify(body)); };

async function supabase(path) {
  const base = process.env.SUPABASE_URL;
  const key = process.env.SUPABASE_KEY;
  if (!base || !key) throw new Error("Supabase ist nicht konfiguriert.");
  const response = await fetch(`${base}/rest/v1/${path}`, { headers: { apikey: key, authorization: `Bearer ${key}` } });
  if (!response.ok) throw new Error(`Supabase ${response.status}`);
  return response.json();
}

async function getEpisodes() {
  const episodes = await supabase("episoden?select=id,datum,manuskripttext,audio_url,status&audio_url=not.is.null&order=datum.desc&limit=24");
  if (!episodes.length) return [];
  const ids = episodes.map(({ id }) => id).join(",");
  const sources = await supabase(`episoden_quellen?select=episode_id,quelle_name,quelle_url,titel&episode_id=in.(${ids})&order=zeitstempel.asc`);
  const kosten = await supabase(`api_kosten?select=episode_id,geschaetzte_kosten_usd&episode_id=in.(${ids})`);
  return episodes.map((episode) => ({
    ...episode,
    quellen: sources.filter((source) => source.episode_id === episode.id),
    kosten_usd: kosten
      .filter((eintrag) => eintrag.episode_id === episode.id)
      .reduce((summe, eintrag) => summe + Number(eintrag.geschaetzte_kosten_usd || 0), 0),
  }));
}

createServer(async (req, res) => {
  try {
    const url = new URL(req.url, `http://${req.headers.host}`);
    if (url.pathname === "/api/episoden") return json(res, 200, { episodes: await getEpisodes() });
    const requested = url.pathname === "/" ? "index.html" : url.pathname.slice(1);
    const path = normalize(join(root, requested));
    if (!path.startsWith(root)) return json(res, 403, { error: "Nicht erlaubt" });
    const file = await readFile(path);
    res.writeHead(200, { "content-type": types[extname(path)] || "application/octet-stream", "cache-control": "public, max-age=300" });
    res.end(file);
  } catch (error) {
    if (req.url?.startsWith("/api/")) return json(res, 503, { error: error.message });
    try { res.writeHead(404, { "content-type": "text/html; charset=utf-8" }); res.end(await readFile(join(root, "index.html"))); }
    catch { res.end("Nicht gefunden"); }
  }
}).listen(port, () => console.log(`Frühschicht KI läuft auf http://localhost:${port}`));
