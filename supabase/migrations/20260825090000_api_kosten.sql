create table api_kosten (
    id uuid primary key default gen_random_uuid(),
    zeitstempel timestamptz not null default now(),
    dienst text not null,
    modell text not null,
    schritt text not null,
    einheit_typ text not null
        check (einheit_typ in ('tokens', 'zeichen')),
    menge_input integer not null,
    menge_output integer,
    geschaetzte_kosten_usd numeric not null,
    lauf_id uuid references lauf_protokoll(id) on delete set null,
    episode_id uuid references episoden(id) on delete set null
);

comment on column episoden.kosten is
    'Aggregierte tatsaechliche Kosten (Summe aus api_kosten), nicht mehr geschaetzt.';
