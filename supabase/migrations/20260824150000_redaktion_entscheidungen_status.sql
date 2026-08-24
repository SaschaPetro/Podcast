alter table redaktion_entscheidungen
    add column status text check (status in ('akzeptiert', 'abgelehnt', 'zurueckgestellt'));

update redaktion_entscheidungen
set status = case
    when akzeptiert = true then 'akzeptiert'
    when akzeptiert = false then 'abgelehnt'
    else status
end;

alter table redaktion_entscheidungen
    add column erste_zurueckstellung_am timestamptz;
