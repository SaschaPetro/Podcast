alter table themen
    add column zweite_quelle_bestaetigt boolean,
    add column zweite_quelle_url text,
    add column zweite_quelle_einschaetzung text;

comment on column themen.zweite_quelle_bestaetigt is
    'Ergebnis der Zweite-Quelle-Verifikation (Ausschreibungs-Kriterium 5): true wenn eine unabhaengige Quelle (Tavily/Exa) den Kernfakt bestaetigt hat, false wenn geprueft aber nicht bestaetigt. NULL = nicht geprueft (Update/Duplikat, oder keine Suchergebnisse gefunden). Wird nur bei NEU angelegten Themen in verarbeite_akzeptierte_entscheidungen() befuellt (siehe recherche_und_redaktion.py).';
comment on column themen.zweite_quelle_url is
    'URL der bestaetigenden Quelle, falls zweite_quelle_bestaetigt = true.';
comment on column themen.zweite_quelle_einschaetzung is
    'Kurze Gemini-Einschaetzung zur Zweite-Quelle-Pruefung - auch vorhanden wenn zweite_quelle_bestaetigt = false (erklaert dann, warum keine Bestaetigung gefunden wurde).';
