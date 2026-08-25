alter table agenten_konfiguration
    drop constraint agenten_konfiguration_rolle_check;

alter table agenten_konfiguration
    add constraint agenten_konfiguration_rolle_check
        check (rolle in ('recherche', 'redaktion', 'moderator', 'rhetorik'));

create table rhetorik_bewertungen (
    id uuid primary key default gen_random_uuid(),
    zeitstempel timestamptz not null default now(),
    episode_ids uuid[] not null,
    gesamteinschaetzung text,
    konkrete_probleme jsonb
);

comment on table rhetorik_bewertungen is
    'Ergebnis der periodischen Rhetorik-Pruefung (alle 4 Episoden): prueft rhetorische Qualitaet ueber mehrere Manuskripte hinweg (Wiederholungen, Struktur, Einstieg, Uebergaenge) - unabhaengig vom faktischen Faktencheck in episoden.faktencheck_ergebnis. Wird von pruefe_rhetorik() in rhetorik_check.py befuellt, reine Analyse/Empfehlung, keine automatische Aenderung am Manuskript-Prompt.';
