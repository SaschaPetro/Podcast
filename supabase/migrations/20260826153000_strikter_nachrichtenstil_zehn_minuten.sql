begin;

update manuskript_prompt_versionen
set ist_aktiv = false
where ist_aktiv = true;

insert into manuskript_prompt_versionen (
    version_nummer, prompt_text, ist_aktiv, erstellt_von, begruendung
)
select
    7,
    prompt_text || $LEITLINIE$

VERBINDLICH: STORYTELLING NUR ZUR ERKLÄRUNG
Diese Sendung besteht aus Nachrichten, nicht aus Geschichten. Nenne bei jedem Thema zuerst die neue, belegte Meldung, dann den notwendigen Kontext und anschließend die Bedeutung für Unternehmen. Storytelling darf erst danach und nur dann eingesetzt werden, wenn ein komplexer Zusammenhang ohne ein kurzes Beispiel schwer verständlich wäre. Wenn die Nachricht ohne Beispiel verständlich ist, verwende gar kein Storytelling. Keine erfundenen Figuren, Dialoge, Schauplätze, Tagesabläufe, Atmosphären oder Spannungskurven.

VERBINDLICH: UNGEFÄHR ZEHN MINUTEN
Das Manuskript muss 1.300 bis 1.450 Wörter umfassen und darf keinesfalls weniger als 1.200 Wörter haben. Nutze die Länge ausschließlich für belegte Details, Hintergrund, Zusammenhänge, Folgen für Unternehmen und konkrete Handlungsmöglichkeiten. Keine Wiederholungen und keine Füllsätze. Prüfe die Wortzahl vor der Ausgabe.
$LEITLINIE$,
    true,
    'mensch',
    'Storytelling nur noch bei echtem Erklärungsbedarf nach dem Nachrichtenkern; 1300-1450 Wörter für ungefähr zehn Minuten.'
from manuskript_prompt_versionen
where version_nummer = 6
on conflict (version_nummer) do update
set prompt_text = excluded.prompt_text,
    ist_aktiv = true,
    erstellt_von = excluded.erstellt_von,
    begruendung = excluded.begruendung;

commit;
