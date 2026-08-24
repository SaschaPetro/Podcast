create extension if not exists pgcrypto;

create table rohnachrichten (
  id uuid primary key default gen_random_uuid(),
  quelle text,
  url text,
  titel text,
  text text,
  abrufzeitpunkt timestamptz
);

create table themen (
  id uuid primary key default gen_random_uuid(),
  titel text,
  zusammenfassung text,
  quelle text,
  erster_kontaktzeitpunkt timestamptz,
  letztes_update timestamptz,
  status text
);

create table themen_updates (
  id uuid primary key default gen_random_uuid(),
  thema_id uuid references themen(id) on delete cascade,
  was_neu text,
  datum timestamptz
);

create table episoden (
  id uuid primary key default gen_random_uuid(),
  datum timestamptz,
  manuskripttext text,
  audio_pfad text,
  kosten numeric
);
