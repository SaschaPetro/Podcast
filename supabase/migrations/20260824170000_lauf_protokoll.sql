create table lauf_protokoll (
    id uuid primary key default gen_random_uuid(),
    zeitstempel timestamptz not null default now(),
    dauer_sekunden integer not null,
    erfolgreich boolean not null,
    fehler_details text
);
