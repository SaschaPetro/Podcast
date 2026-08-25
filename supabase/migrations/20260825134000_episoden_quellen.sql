create table episoden_quellen (
    id uuid primary key default gen_random_uuid(),
    zeitstempel timestamptz not null default now(),
    episode_id uuid not null references episoden(id) on delete cascade,
    thema_id uuid references themen(id) on delete set null,
    rohnachricht_id uuid references rohnachrichten(id) on delete set null,
    quelle_name text,
    quelle_url text,
    titel text,
    unique (episode_id, thema_id, rohnachricht_id)
);

create index episoden_quellen_episode_id_idx on episoden_quellen (episode_id);

comment on table episoden_quellen is
    'Dauerhafte Verknuepfung jeder Episode zu den Original-Rohnachrichten (Quelle-Name + URL) der tatsaechlich verwendeten Themen. Wird beim Speichern einer Episode in erstelle_episode() automatisch befuellt (siehe generiere_episode.py). Fuer Themen ohne nachvollziehbare Rohnachricht-Verknuepfung (z.B. alte Seed-/Testdaten ohne redaktion_entscheidungen-Bezug) entsteht keine Zeile - kein Fehler, nur keine Quelle vorhanden.';
