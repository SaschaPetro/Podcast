// Vercel Cron Job (siehe vercel.json "crons") ruft diesen Endpoint werktags
// 6:45 Uhr lokal auf und loest darueber den GitHub-Actions-Workflow
// "Sammellauf" (workflow_dispatch) aus - siehe README Abschnitt 10.
//
// Warum ueberhaupt ein Umweg ueber Vercel statt GitHub direkt per "schedule"
// zu triggern: GitHub Actions "schedule"-Events koennen bei Auslastung um
// Stunden verzoegert oder komplett fallengelassen werden (dokumentiertes
// GitHub-Verhalten, kein Bug hier) - beobachtet am 2026-08-31 (6h23min
// Verspaetung) und 2026-09-01 (Lauf blieb ganz aus). Vercel Cron Jobs sind
// zuverlaessiger. Morgenlauf haengt sich per "workflow_run" automatisch an
// das Ende von Sammellauf - siehe morgenlauf.yml - statt selbst eine feste
// Uhrzeit zu raten.
//
// Absicherung gegen fremdes Ausloesen: Vercel haengt bei konfiguriertem
// CRON_SECRET automatisch "Authorization: Bearer <CRON_SECRET>" an eigene
// Cron-Aufrufe an - ohne passenden Header wird abgelehnt.

const DISPATCH_URL =
  "https://api.github.com/repos/SaschaPetro/Podcast/actions/workflows/sammellauf.yml/dispatches";

export default async function handler(request, response) {
  if (request.method !== "GET" && request.method !== "POST") {
    response.setHeader("Allow", "GET, POST");
    return response.status(405).json({ error: "Methode nicht erlaubt." });
  }

  const cronSecret = process.env.CRON_SECRET;
  const authHeader = request.headers["authorization"];
  if (!cronSecret || authHeader !== `Bearer ${cronSecret}`) {
    return response.status(401).json({ error: "Nicht autorisiert." });
  }

  const token = process.env.GITHUB_ACTIONS_TOKEN;
  if (!token) {
    console.error("GITHUB_ACTIONS_TOKEN fehlt als Environment Variable.");
    return response.status(500).json({ error: "GITHUB_ACTIONS_TOKEN ist nicht konfiguriert." });
  }

  try {
    const githubResponse = await fetch(DISPATCH_URL, {
      method: "POST",
      headers: {
        authorization: `Bearer ${token}`,
        accept: "application/vnd.github+json",
        "content-type": "application/json",
        "user-agent": "fruehschicht-ki-vercel-cron",
      },
      body: JSON.stringify({ ref: "main" }),
    });

    if (githubResponse.status !== 204) {
      const body = await githubResponse.text();
      console.error(`Sammellauf-Trigger fehlgeschlagen (${githubResponse.status}): ${body}`);
      return response
        .status(502)
        .json({ error: "Sammellauf konnte nicht ausgelöst werden.", status: githubResponse.status });
    }

    return response.status(200).json({ ok: true, ausgeloest: "sammellauf.yml" });
  } catch (error) {
    console.error("Sammellauf-Trigger fehlgeschlagen:", error.message);
    return response.status(502).json({ error: "Sammellauf konnte nicht ausgelöst werden." });
  }
}
