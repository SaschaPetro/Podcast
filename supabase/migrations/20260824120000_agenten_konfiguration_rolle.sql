alter table agenten_konfiguration
    add column rolle text default 'recherche'
        constraint agenten_konfiguration_rolle_check check (rolle in ('recherche', 'redaktion'));
