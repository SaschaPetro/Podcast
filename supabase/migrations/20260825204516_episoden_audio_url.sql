alter table episoden add column audio_url text;

comment on column episoden.audio_url is
    'Oeffentliche Supabase-Storage-URL der Episoden-Audiodatei (Bucket "episoden-audio", Dateiname = episode_id + ".mp3"). Wird von text_zu_audio() in generiere_audio.py automatisch befuellt, sofern der Upload gelingt - schlaegt er fehl, bleibt die Spalte NULL, die lokale audio_pfad-Spalte bleibt in jedem Fall die verlaessliche Referenz.';
