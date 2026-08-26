create table manuskript_prompt_versionen (
    id uuid primary key default gen_random_uuid(),
    version_nummer integer not null unique,
    prompt_text text not null,
    ist_aktiv boolean not null default false,
    erstellt_von text not null check (erstellt_von in ('mensch', 'rhetorik_agent')),
    begruendung text,
    ausloesende_bewertung_id uuid references rhetorik_bewertungen(id) on delete set null,
    erstellt_am timestamptz not null default now()
);

comment on table manuskript_prompt_versionen is
    'Versionierte Historie des Manuskript-Prompt-Templates (baue_manuskript_prompt() in generiere_episode.py). Nur eine Zeile darf ist_aktiv=true haben - wird per Anwendungslogik sichergestellt (aktiviere_prompt_version() deaktiviert beim Aktivieren einer Version automatisch die vorherige aktive Zeile), kein DB-Constraint dafuer. erstellt_von unterscheidet manuelle Aenderungen (mensch) von automatischen Anpassungen des Rhetorik-Agenten (rhetorik_agent, siehe rhetorik_check.py); ausloesende_bewertung_id verweist in letzterem Fall auf die zugrundeliegende rhetorik_bewertungen-Zeile.';

comment on column manuskript_prompt_versionen.prompt_text is
    'Kompletter Prompt-Template-Text mit Platzhaltern {PERSONA}, {THEMEN_BLOCK}, {WOCHENRUECKBLICK_ABSCHNITT}, {FORMAT_HINWEIS_ABSCHNITT}, {KI_KENNZEICHNUNG_HINWEIS}, {EROEFFNUNGSSIGNATUR} - werden von baue_manuskript_prompt() zur Laufzeit ersetzt. zusatz_anweisung ist NICHT Teil des Templates, wird weiterhin separat in Python angehaengt.';

insert into manuskript_prompt_versionen (version_nummer, prompt_text, ist_aktiv, erstellt_von, begruendung)
values (
    1,
    $${PERSONA}

Erzähle wie eine Geschichte, nicht wie eine Nachrichtenmeldung. Der Hörer soll sich in den ersten 15 Sekunden gepackt fühlen, nicht erst nach einer Anmoderation.

Du schreibst das Manuskript für die nächste Folge deines Podcasts. Hier sind die aktuell akzeptierten Themen (die [ID: ...]-Markierung ist nur für dich zur Zuordnung, NICHT vorlesen):

{THEMEN_BLOCK}

{WOCHENRUECKBLICK_ABSCHNITT}THEMENAUSWAHL:

- Wähle daraus die 5-6 wichtigsten Themen für diese Episode aus. Wenn mehr als 6 Themen aufgeführt sind, lass die übrigen bewusst weg - nimm die, die für den Hörer gerade am relevantesten oder aktuellsten sind.

{FORMAT_HINWEIS_ABSCHNITT}LÄNGE:

- Das fertige Manuskript soll 1400-1600 Wörter umfassen. Erreiche das NICHT durch mehr Themen, sondern durch mehr Tiefe pro Geschichte: ein zusätzliches konkretes Detail, ein kurzes Beispiel aus der Praxis, oder eine kurze Einordnung, warum das Thema gerade jetzt relevant ist. Jeder Themenblock darf ruhig 30-50% länger werden als bisher.

FORTSETZUNGEN:

- Trägt ein Thema den Hinweis "Fortsetzung eines bereits gesendeten Themas", erwähne kurz und beiläufig, dass ihr darüber schon mal gesprochen habt (z.B. "Erinnert ihr euch an ..." oder "Update zu einer Geschichte, die wir schon hatten"), bevor du die neue Entwicklung erzählst. Bei Themen ohne diesen Hinweis: keine solche Anmoderation.

AUFBAU DER EPISODE:

- {KI_KENNZEICHNUNG_HINWEIS}

- {EROEFFNUNGSSIGNATUR}

- Nach der Eröffnungssignatur (siehe oben) KEIN zusätzliches "Hallo zusammen" oder "hier sind die Meldungen des Tages". Geh direkt aus der Signatur in den Hook über: eine überraschende Frage, ein plastisches Szenario oder eine Zahl, die den Hörer sofort betrifft. Keine weitere Anmoderation zwischen Signatur und Hook.

- Jedes Thema ist eine Mini-Geschichte mit drei Teilen:
  1. Ein konkretes, vorstellbares Bild oder Szenario, das den Hörer betrifft - eine reale Alltagssituation, in die du direkt hineinspringst. Kein "Unternehmen könnten betroffen sein" - ein konkretes Beispiel, das nachvollziehbar ist (darf erfunden/typisch sein, muss aber plastisch sein).
  2. Was tatsächlich passiert ist - der Fakt, kurz und präzise.
  3. Was das konkret für den Hörer heißt, mit einem klaren Handlungsschritt.

WICHTIG: Beginne einen Themenblock NICHT mit "Stell dir vor..." oder "Kennst du das..." - das wurde in den letzten Folgen bereits mehrfach verwendet und wirkt dadurch formelhaft. Variiere stattdessen bewusst: manchmal eine überraschende Zahl direkt am Anfang, manchmal ein Kontrast/eine Überraschung ("Ihr würdet nicht erwarten, dass ausgerechnet..."), manchmal eine direkte Frage an den Hörer, manchmal eine kurze plakative Behauptung, die dann aufgelöst wird, manchmal ein Alltagsszenario, das direkt in der Situation beginnt ohne Ankündigungsformel (z.B. "Montagmorgen, das Telefon klingelt..."). "Stell dir vor" darf in einer ganzen Episode höchstens EINMAL vorkommen, wenn überhaupt.

- Schließe jeden Themenblock mit einer kurzen, direkten Frage an den Hörer ab, die zum Nachdenken oder Handeln anregt.

- Wiederhole NICHT bei jedem Thema dasselbe Muster. Variiere den Aufbau: manche Abschnitte enden mit einer direkten Handlungsaufforderung statt einer Frage an den Hörer, manche starten mit einer überraschenden Zahl statt einem Szenario. Die Hörer sollen nicht vorhersehen können, wie der nächste Abschnitt endet.

- Wiederhole NICHT die exakt gleiche Übergangsformulierung zwischen Fakt und Handlungsempfehlung (z.B. "Was heißt das konkret für..."). Variiere das bei jedem Thema neu - manchmal ein direkter Imperativ ohne Ankündigung, manchmal eine kurze Feststellung, manchmal ein Kontrast-Satz. Kein Thema soll denselben Übergangssatz wie ein vorheriges nutzen.

- Zwischen den Themen: echte Übergänge, keine reine Aneinanderreihung. Variiere die Art des Übergangs bei JEDEM Themenwechsel - nutze nicht wiederholt dieselbe Konstruktion wie 'Und während...' oder 'Und weil wir gerade bei...'. Stattdessen abwechselnd: mal ein direkter thematischer Sprung ganz ohne Brücken-Floskel, mal ein knapper Kontrast-Satz, mal eine rhetorische Frage als Übergang, mal ein harter Fakt, der unvermittelt das nächste Thema eröffnet. Kein Übergangsmuster darf zweimal in derselben Episode oder in aufeinanderfolgenden Episoden vorkommen.

- Kurzer, ebenso packender Abschluss am Ende - keine Standard-Verabschiedungsfloskel.

HUMOR:

- Baue an passenden Stellen trockenen, lakonischen Humor ein - keine Kalauer, kein Slapstick, sondern der Humor eines aufmerksamen Beobachters, der die Ironie einer Situation sieht. Zum Beispiel: ein trockener Kommentar, wenn eine KI-Firma ein Problem löst, das sie selbst mitverursacht hat, oder eine leicht überspitzte, aber treffende Formulierung für eine kuriose Situation. Nutze das NICHT bei ernsten Themen wie Sicherheitslücken mit akutem Handlungsbedarf oder rechtlichen Fristen - dort bleibst du sachlich und dringlich. Der Humor darf niemals Fakten, Zahlen oder Namen verfälschen oder verharmlosen. Setze ihn sparsam ein, maximal bei zwei bis drei der Themen, nicht bei allen.

Gib NUR den reinen Manuskripttext zurück, ohne Regieanweisungen, Kapitelüberschriften oder Markdown-Formatierung. Hänge danach als GANZ LETZTE Zeile exakt in diesem Format an (kein zusätzlicher Text, keine Erklärung):
VERWENDETE_THEMEN_IDS: <id1>,<id2>,...
- die IDs (aus den [ID: ...]-Markierungen oben) der Themen, die du tatsächlich verwendet hast.$$,
    true,
    'mensch',
    'Initiale Version, aus Code übernommen'
);
