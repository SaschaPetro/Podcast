alter table episoden
    add column faktencheck_ergebnis jsonb;

alter table episoden
    add column status text not null default 'ungeprueft'
        check (status in ('ungeprueft', 'freigegeben', 'pruefung_fehlgeschlagen'));
