alter table lauf_protokoll
    add column lauftyp text not null default 'morgenlauf'
        check (lauftyp in ('sammellauf', 'morgenlauf'));

comment on column lauf_protokoll.lauftyp is
    'Unterscheidet, welches Orchestrierungs-Skript diesen Lauf erzeugt hat: "sammellauf" (sammellauf.py, Takt 1: RSS/Recherche/Redaktion/Themenpflege) oder "morgenlauf" (morgenlauf.py, Takt 2: Manuskript/Faktencheck/Audio/Rhetorik). Default "morgenlauf" gilt fuer alle Alt-Zeilen vor Einfuehrung dieser Spalte, die noch den kompletten 9-Schritte-Durchlauf darstellten.';
