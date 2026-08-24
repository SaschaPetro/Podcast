create table agenten_konfiguration (
    id uuid primary key default gen_random_uuid(),
    name text,
    fokus_beschreibung text,
    aktiv boolean default true,
    erstellt_am timestamptz default now()
);

create table agent_vorschlaege (
    id uuid primary key default gen_random_uuid(),
    agent_id uuid references agenten_konfiguration(id) on delete cascade,
    rohnachricht_id uuid references rohnachrichten(id) on delete cascade,
    begruendung text,
    vorgeschlagen_am timestamptz default now()
);

create table redaktion_entscheidungen (
    id uuid primary key default gen_random_uuid(),
    vorschlag_id uuid references agent_vorschlaege(id) on delete cascade,
    akzeptiert boolean,
    begruendung text,
    thema_id uuid references themen(id) on delete set null,
    entschieden_am timestamptz default now()
);
