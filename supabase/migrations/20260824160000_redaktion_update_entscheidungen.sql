create table redaktion_update_entscheidungen (
    id uuid primary key default gen_random_uuid(),
    update_id uuid references themen_updates(id) on delete cascade,
    thema_id uuid references themen(id) on delete cascade,
    wieder_aufgenommen boolean not null,
    begruendung text,
    entschieden_am timestamptz default now()
);
