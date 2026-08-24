alter table agenten_konfiguration
    drop constraint agenten_konfiguration_rolle_check;

alter table agenten_konfiguration
    add constraint agenten_konfiguration_rolle_check
        check (rolle in ('recherche', 'redaktion', 'moderator'));
