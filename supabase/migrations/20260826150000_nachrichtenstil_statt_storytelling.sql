begin;

update manuskript_prompt_versionen
set ist_aktiv = false
where ist_aktiv = true;

insert into manuskript_prompt_versionen (
    version_nummer,
    prompt_text,
    ist_aktiv,
    erstellt_von,
    begruendung
)
select
    6,
    replace(
      replace(
        replace(
          replace(
            replace(
              prompt_text,
              'Erzähle wie eine Geschichte, nicht wie eine Nachrichtenmeldung. Der Hörer soll sich in den ersten 15 Sekunden gepackt fühlen, nicht erst nach einer Anmoderation.',
              'Schreibe eine klare Nachrichtensendung. Nenne bei jedem Thema zuerst die neue, belegte Meldung. Nutze Storytelling nur sparsam, um Ursache, Zusammenhang und Auswirkung nachvollziehbar zu machen.'
            ),
            'mehr Tiefe pro Geschichte',
            'mehr belastbare Einordnung pro Nachricht'
          ),
          'Jedes Thema ist eine Mini-Geschichte mit drei Teilen:',
          'Jedes Thema ist ein Nachrichtenblock mit drei Teilen:'
        ),
        '1. Ein konkretes, vorstellbares Bild oder Szenario, das den Hörer betrifft - eine reale Alltagssituation, in die du direkt hineinspringst. Kein "Unternehmen könnten betroffen sein" - ein konkretes Beispiel, das nachvollziehbar ist (darf erfunden/typisch sein, muss aber plastisch sein).',
        '1. Was neu passiert ist - der belegte Nachrichtenkern in einem klaren Satz. Nenne die betroffenen Unternehmen, Produkte oder Regeln direkt.'
      ),
      '2. Was tatsächlich passiert ist - der Fakt, kurz und präzise.',
      '2. Der nötige Kontext - kurz und präzise: Was führte dazu und wie ist die Entwicklung einzuordnen?'
    ),
    true,
    'mensch',
    'Nachrichtenkern hat Vorrang. Storytelling dient nur noch der kurzen, nachvollziehbaren Einordnung belegter Fakten.'
from manuskript_prompt_versionen
where version_nummer = 1
on conflict (version_nummer) do update
set prompt_text = excluded.prompt_text,
    ist_aktiv = true,
    erstellt_von = excluded.erstellt_von,
    begruendung = excluded.begruendung;

update manuskript_prompt_versionen
set prompt_text = regexp_replace(
    prompt_text,
    E'- Nach der Eröffnungssignatur[^\\r\\n]*',
    '- Nach der Eröffnungssignatur gehst du direkt in die wichtigste Meldung. Beginne mit dem neuen Fakt, einer belegten Zahl oder der unmittelbaren Konsequenz. Keine zusätzliche Anmoderation und kein erfundenes Einstiegsszenario.'
)
where version_nummer = 6;

update manuskript_prompt_versionen
set prompt_text = regexp_replace(
    prompt_text,
    E'WICHTIG: Beginne einen Themenblock[^\\r\\n]*',
    'WICHTIG: Beginne jeden Themenblock mit der eigentlichen Meldung. Fragen oder Beispiele dürfen erst nach dem Nachrichtenkern folgen und nur, wenn sie die Einordnung messbar verständlicher machen.'
)
where version_nummer = 6;

update manuskript_prompt_versionen
set prompt_text = regexp_replace(
    prompt_text,
    E'- Wiederhole NICHT bei jedem Thema dasselbe Muster\\.[^\\r\\n]*',
    '- Variiere Satzlänge und Übergänge, aber nicht die journalistische Reihenfolge: Nachricht zuerst, dann Kontext und konkrete Bedeutung.'
)
where version_nummer = 6;

commit;
