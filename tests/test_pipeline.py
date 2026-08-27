import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


class ModellzuordnungTests(unittest.TestCase):
    def test_qualitaetsmodell_nur_fuer_manuskript(self):
        import modelle

        with patch.dict(os.environ, {"GEMINI_FAST_MODEL": "fast", "GEMINI_QUALITY_MODEL": "quality"}):
            self.assertEqual(modelle.modell_fuer("manuskript_erstellung"), "quality")
            for schritt in ("recherche_auswahl", "neuigkeit_pruefung", "faktencheck"):
                self.assertEqual(modelle.modell_fuer(schritt), "fast")

    def test_leere_workflow_variable_nutzt_stabilen_standard(self):
        import modelle

        with patch.dict(os.environ, {"GEMINI_FAST_MODEL": ""}):
            self.assertEqual(modelle.schnelles_modell(), modelle.STANDARD_SCHNELLES_MODELL)

    def test_schnelle_kette_beginnt_mit_primaermodell_und_enthaelt_fallbacks(self):
        import modelle

        with patch.dict(os.environ, {"GEMINI_FAST_MODEL": ""}):
            kette = modelle.schnelle_modell_kette()

        self.assertEqual(kette[0], modelle.STANDARD_SCHNELLES_MODELL)
        self.assertEqual(kette, modelle.FALLBACK_SCHNELLES_MODELL)

    def test_qualitaets_kette_beginnt_mit_primaermodell_und_enthaelt_fallbacks(self):
        import modelle

        with patch.dict(os.environ, {"GEMINI_QUALITY_MODEL": ""}):
            kette = modelle.qualitaets_modell_kette()

        self.assertEqual(kette[0], modelle.STANDARD_QUALITAETS_MODELL)
        self.assertEqual(kette, modelle.FALLBACK_QUALITAETS_MODELL)

    def test_env_override_wird_primaer_verliert_aber_sicherheitsnetz_nicht(self):
        import modelle

        with patch.dict(os.environ, {"GEMINI_FAST_MODEL": "custom-modell"}):
            kette = modelle.schnelle_modell_kette()

        self.assertEqual(kette[0], "custom-modell")
        # Rest der eingebauten Kette bleibt komplett erhalten, nur ohne Duplikat:
        self.assertEqual(kette[1:], modelle.FALLBACK_SCHNELLES_MODELL)

    def test_modell_kette_fuer_manuskript_nutzt_qualitaetskette(self):
        import modelle

        with patch.dict(os.environ, {"GEMINI_FAST_MODEL": "", "GEMINI_QUALITY_MODEL": ""}):
            self.assertEqual(
                modelle.modell_kette_fuer("manuskript_erstellung"), modelle.FALLBACK_QUALITAETS_MODELL
            )
            self.assertEqual(
                modelle.modell_kette_fuer("faktencheck"), modelle.FALLBACK_SCHNELLES_MODELL
            )


class GeminiClientTests(unittest.TestCase):
    def test_generate_content_nutzt_neue_client_schnittstelle(self):
        from gemini_client import GeminiModell

        client = MagicMock()
        erwartet = MagicMock(text="Antwort")
        client.models.generate_content.return_value = erwartet
        modell = GeminiModell("test-modell", client=client)

        antwort = modell.generate_content(
            "Prompt", {"response_mime_type": "application/json", "max_output_tokens": 123}
        )

        self.assertIs(antwort, erwartet)
        kwargs = client.models.generate_content.call_args.kwargs
        self.assertEqual(kwargs["model"], "test-modell")
        self.assertEqual(kwargs["contents"], "Prompt")
        self.assertEqual(kwargs["config"].response_mime_type, "application/json")
        self.assertEqual(kwargs["config"].max_output_tokens, 123)

    def test_tts_liefert_pcm_und_usage(self):
        from gemini_client import erzeuge_tts_audio

        client = MagicMock()
        inline_data = MagicMock(data=b"\x00\x00\x01\x00")
        client.models.generate_content.return_value = MagicMock(
            candidates=[MagicMock(content=MagicMock(parts=[MagicMock(inline_data=inline_data)]))],
            usage_metadata=MagicMock(prompt_token_count=12, candidates_token_count=34),
        )
        with patch("gemini_client._neuer_client", return_value=client):
            pcm, input_tokens, output_tokens = erzeuge_tts_audio("modell", "Text", "Charon")

        self.assertEqual(pcm, b"\x00\x00\x01\x00")
        self.assertEqual((input_tokens, output_tokens), (12, 34))
        self.assertEqual(client.models.generate_content.call_args.kwargs["contents"], "Text")
        config = client.models.generate_content.call_args.kwargs["config"]
        self.assertEqual(config.response_modalities, ["AUDIO"])

    def test_503_wird_wiederholt_und_gelingt_dann(self):
        from google.genai import errors as genai_errors
        from gemini_client import GeminiModell

        client = MagicMock()
        ueberlastet = genai_errors.ServerError(
            503, {"error": {"message": "high demand", "status": "UNAVAILABLE"}}
        )
        erwartet = MagicMock(text="Antwort")
        client.models.generate_content.side_effect = [ueberlastet, ueberlastet, erwartet]
        modell = GeminiModell("test-modell", client=client)

        with patch("gemini_client.time.sleep") as schlaf:
            antwort = modell.generate_content("Prompt")

        self.assertIs(antwort, erwartet)
        self.assertEqual(client.models.generate_content.call_count, 3)
        self.assertEqual(schlaf.call_count, 2)

    def test_429_wird_wie_503_behandelt(self):
        from google.genai import errors as genai_errors
        from gemini_client import GeminiModell

        client = MagicMock()
        rate_limit = genai_errors.ClientError(
            429, {"error": {"message": "rate limited", "status": "RESOURCE_EXHAUSTED"}}
        )
        erwartet = MagicMock(text="Antwort")
        client.models.generate_content.side_effect = [rate_limit, erwartet]
        modell = GeminiModell("test-modell", client=client)

        with patch("gemini_client.time.sleep"):
            antwort = modell.generate_content("Prompt")

        self.assertIs(antwort, erwartet)
        self.assertEqual(client.models.generate_content.call_count, 2)

    def test_dauerhafter_fehler_wird_sofort_durchgereicht(self):
        from google.genai import errors as genai_errors
        from gemini_client import GeminiModell

        client = MagicMock()
        ungueltig = genai_errors.ClientError(
            400, {"error": {"message": "bad request", "status": "INVALID_ARGUMENT"}}
        )
        client.models.generate_content.side_effect = ungueltig
        modell = GeminiModell("test-modell", client=client)

        with patch("gemini_client.time.sleep") as schlaf:
            with self.assertRaises(genai_errors.ClientError):
                modell.generate_content("Prompt")

        self.assertEqual(client.models.generate_content.call_count, 1)
        schlaf.assert_not_called()

    def test_transienter_fehler_wirft_nach_letztem_versuch(self):
        from google.genai import errors as genai_errors
        from gemini_client import GeminiModell, RETRY_VERSUCHE

        client = MagicMock()
        ueberlastet = genai_errors.ServerError(
            503, {"error": {"message": "high demand", "status": "UNAVAILABLE"}}
        )
        client.models.generate_content.side_effect = ueberlastet
        modell = GeminiModell("test-modell", client=client)

        with patch("gemini_client.time.sleep"):
            with self.assertRaises(genai_errors.ServerError):
                modell.generate_content("Prompt")

        self.assertEqual(client.models.generate_content.call_count, RETRY_VERSUCHE)

    def test_kette_wechselt_zu_naechstem_modell_nach_erschoepftem_retry(self):
        from google.genai import errors as genai_errors
        from gemini_client import GeminiModell, RETRY_VERSUCHE

        client = MagicMock()
        ueberlastet = genai_errors.ServerError(
            503, {"error": {"message": "high demand", "status": "UNAVAILABLE"}}
        )
        erwartet = MagicMock(text="Antwort von Modell 2")
        client.models.generate_content.side_effect = [ueberlastet] * RETRY_VERSUCHE + [erwartet]
        modell = GeminiModell(["modell-1", "modell-2"], client=client)

        with patch("gemini_client.time.sleep"):
            antwort = modell.generate_content("Prompt")

        self.assertIs(antwort, erwartet)
        self.assertEqual(modell.aktuelles_modell, "modell-2")
        self.assertEqual(client.models.generate_content.call_count, RETRY_VERSUCHE + 1)
        verwendete_modelle = [
            c.kwargs["model"] for c in client.models.generate_content.call_args_list
        ]
        self.assertEqual(verwendete_modelle, ["modell-1"] * RETRY_VERSUCHE + ["modell-2"])

    def test_kette_komplett_erschoepft_wirft_letzten_fehler(self):
        from google.genai import errors as genai_errors
        from gemini_client import GeminiModell

        client = MagicMock()
        nicht_gefunden = genai_errors.ClientError(
            404, {"error": {"message": "model not found", "status": "NOT_FOUND"}}
        )
        client.models.generate_content.side_effect = nicht_gefunden
        modell = GeminiModell(["modell-1", "modell-2"], client=client)

        with patch("gemini_client.time.sleep"):
            with self.assertRaises(genai_errors.ClientError):
                modell.generate_content("Prompt")

        # 404 ist nicht transient (_ist_transienter_fehler) -> je genau 1 Versuch pro Modell
        self.assertEqual(client.models.generate_content.call_count, 2)
        self.assertIsNone(modell.aktuelles_modell)

    def test_tts_stellt_sprechstilanweisung_vor_den_text(self):
        from gemini_client import erzeuge_tts_audio

        client = MagicMock()
        client.models.generate_content.return_value = MagicMock(
            candidates=[
                MagicMock(
                    content=MagicMock(
                        parts=[MagicMock(inline_data=MagicMock(data=b"\x00\x00"))]
                    )
                )
            ],
            usage_metadata=MagicMock(prompt_token_count=1, candidates_token_count=1),
        )
        with patch("gemini_client._neuer_client", return_value=client):
            erzeuge_tts_audio("modell", "Nachricht.", "Charon", "Sprich motiviert.")

        self.assertEqual(
            client.models.generate_content.call_args.kwargs["contents"],
            "Sprich motiviert.\n\nNachricht.",
        )


class PromptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("GEMINI_API_KEY", "test")
        os.environ.setdefault("SUPABASE_URL", "http://localhost")
        os.environ.setdefault("SUPABASE_KEY", "test")
        cls.modul = importlib.import_module("generiere_episode")

    def test_manuskript_prompt_ist_fuer_professionelle_sprechsprache(self):
        template = "{PERSONA}\n{THEMEN_BLOCK}\n{WOCHENRUECKBLICK_ABSCHNITT}{FORMAT_HINWEIS_ABSCHNITT}{KI_KENNZEICHNUNG_HINWEIS}\n{EROEFFNUNGSSIGNATUR}"
        with patch.object(self.modul, "hole_aktive_prompt_version", return_value={"prompt_text": template}):
            prompt = self.modul.baue_manuskript_prompt(
                MagicMock(), "Persona", "Themen", None, "Begrüßung"
            )
        self.assertIn("1.400 bis 1.600 Wörter", prompt)
        self.assertIn("professionelle deutsche Nachrichtenredaktion", prompt)
        self.assertIn("In der heutigen schnelllebigen Welt", prompt)
        self.assertIn("Nutze ausschließlich Zahlen", prompt)

    def test_manuskript_erzeugung_deaktiviert_thinking(self):
        # Siehe generiere_episode.MANUSKRIPT_THINKING_BUDGET: gemini-3.5-flash
        # verbrauchte live beobachtet ~7861 von 8192 max_output_tokens fuers
        # unsichtbare "Denken" und brach das Manuskript nach ~325 sichtbaren
        # Tokens ab (finish_reason=MAX_TOKENS) - unabhaengig von der Themenzahl.
        # thinking_budget=0 muss deshalb bei jedem Versuch mitgeschickt werden.
        chat_model = MagicMock()
        genug_woerter = "Text mit genug Woertern " * 400
        chat_model.generate_content.return_value = MagicMock(
            text=f"{genug_woerter}\nVERWENDETE_THEMEN_IDS: abc",
            usage_metadata=MagicMock(prompt_token_count=1, candidates_token_count=1),
        )
        template = "{PERSONA}\n{THEMEN_BLOCK}\n{WOCHENRUECKBLICK_ABSCHNITT}{FORMAT_HINWEIS_ABSCHNITT}{KI_KENNZEICHNUNG_HINWEIS}\n{EROEFFNUNGSSIGNATUR}"

        with (
            patch.object(self.modul, "hole_aktive_prompt_version", return_value={"prompt_text": template}),
            patch.object(self.modul.kosten_tracking, "logge_api_kosten"),
        ):
            self.modul.erstelle_manuskript(MagicMock(), chat_model, "Persona", "Themen", None, "Begrüßung")

        chat_model.generate_content.assert_called_once()
        generation_config = chat_model.generate_content.call_args.kwargs["generation_config"]
        self.assertEqual(
            generation_config["thinking_config"],
            {"thinking_budget": self.modul.MANUSKRIPT_THINKING_BUDGET},
        )
        self.assertEqual(self.modul.MANUSKRIPT_THINKING_BUDGET, 0)

    def test_verabschiedung_standard_und_freitag(self):
        standard = self.modul.baue_verabschiedung("standard")
        freitag = self.modul.baue_verabschiedung("freitag")

        self.assertIn("bis morgen", standard.lower())
        self.assertNotIn("bis morgen", freitag.lower())
        self.assertIn("schönes wochenende", freitag.lower())
        self.assertIn("bis montag", freitag.lower())

    def test_freitagsprompt_verbietet_bis_morgen(self):
        template = "{PERSONA}\n{THEMEN_BLOCK}\n{WOCHENRUECKBLICK_ABSCHNITT}{FORMAT_HINWEIS_ABSCHNITT}{KI_KENNZEICHNUNG_HINWEIS}\n{EROEFFNUNGSSIGNATUR}"
        verabschiedung = self.modul.baue_verabschiedung("freitag")
        with patch.object(self.modul, "hole_aktive_prompt_version", return_value={"prompt_text": template}):
            prompt = self.modul.baue_manuskript_prompt(
                MagicMock(), "Persona", "Themen", None, "Begrüßung", verabschiedung=verabschiedung
            )

        self.assertIn(verabschiedung, prompt)
        self.assertIn('darf die Verabschiedung insbesondere nicht "bis morgen" sagen', prompt)

    def test_freitagsverabschiedung_wird_nach_modellantwort_erzwungen(self):
        freitag = self.modul.baue_verabschiedung("freitag")
        entwurf = f"Nachrichtentext.\n\n{self.modul.VERABSCHIEDUNG_STANDARD}"

        ergebnis = self.modul.stelle_verabschiedung_sicher(entwurf, freitag)

        self.assertTrue(ergebnis.endswith(freitag))
        self.assertNotIn("bis morgen", ergebnis.lower())
        self.assertEqual(ergebnis.count(freitag), 1)

    def test_faktencheck_deckt_alle_tatsachenbehauptungen_ab(self):
        prompt = self.modul.baue_faktencheck_prompt("Manuskript", "Quellen")
        self.assertIn("jede konkrete Tatsachenbehauptung", prompt)
        self.assertIn("Modellwissen gelten nicht als Beleg", prompt)

    def test_faktencheck_ohne_originalquelle_ruft_kein_modell_auf(self):
        modell = MagicMock()
        with (
            patch.object(self.modul, "hole_supabase_client", return_value=MagicMock()),
            patch.object(self.modul, "hole_chat_model", return_value=modell),
            patch.object(self.modul, "hole_quellen_fuer_themen", return_value={"t1": []}),
        ):
            with self.assertRaises(RuntimeError):
                self.modul.pruefe_manuskript("e1", "Eine Behauptung.", [{"id": "t1", "titel": "Thema"}])
        modell.generate_content.assert_not_called()


class TtsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("DEEPGRAM_API_KEY", "test")
        cls.audio = importlib.import_module("generiere_audio")

    def test_aufbereitung_entfernt_urls_und_normalisiert(self):
        text = "## MARKT\nKI betrifft 25 % der KMU. Mehr: https://example.org/x"
        gesprochen = self.audio.bereite_tts_text_auf(text)
        self.assertNotIn("http", gesprochen)
        self.assertIn("25 Prozent", gesprochen)
        self.assertIn("kleine und mittlere Unternehmen", gesprochen)
        self.assertIn("MARKT.", gesprochen)

    def test_chunks_trennen_keine_normalen_saetze(self):
        chunks = self.audio._teile_text("Erster kurzer Satz. Zweiter kurzer Satz.", max_laenge=25)
        self.assertEqual(chunks, ["Erster kurzer Satz.", "Zweiter kurzer Satz."])

    def test_tts_fehler_veroeffentlicht_keine_unvollstaendige_datei(self):
        client = MagicMock()
        client.speak.v1.audio.generate.side_effect = [iter([b"teil1"]), RuntimeError("TTS kaputt")]
        with tempfile.TemporaryDirectory() as tmp, patch.object(self.audio, "DeepgramClient", return_value=client):
            ziel = Path(tmp) / "episode.mp3"
            with self.assertRaises(RuntimeError):
                self.audio._via_deepgram(("Ein kurzer Satz. " * 150) + "Letzter Satz.", str(ziel))
            self.assertFalse(ziel.exists())

    def test_gemini_tts_schreibt_mp3_und_summiert_tokens(self):
        pcm = b"\x00\x00" * 2400
        antworten = [(pcm, 10, 20), (pcm, 11, 21)]
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(self.audio, "_teile_text", return_value=["Teil eins.", "Teil zwei."]),
            patch.object(self.audio, "erzeuge_tts_audio", side_effect=antworten),
        ):
            ziel = Path(tmp) / "episode.mp3"
            tokens = self.audio._via_gemini_tts("Text", str(ziel))

            self.assertEqual(tokens, (21, 41))
            self.assertEqual(
                self.audio.erzeuge_tts_audio.call_args.args,
                (
                    self.audio.GEMINI_TTS_MODEL,
                    "Teil zwei.",
                    "Charon",
                    self.audio.GEMINI_TTS_SPRECHSTIL,
                ),
            )
            self.assertTrue(ziel.exists())
            self.assertGreater(ziel.stat().st_size, 0)
            self.assertTrue(ziel.read_bytes().startswith((b"ID3", b"\xff\xfb", b"\xff\xf3")))

    def test_gemini_konvertierungsfehler_veroeffentlicht_keine_datei(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(self.audio, "_teile_text", return_value=["Text"]),
            patch.object(self.audio, "erzeuge_tts_audio", return_value=(b"", 1, 1)),
        ):
            ziel = Path(tmp) / "episode.mp3"
            with self.assertRaisesRegex(RuntimeError, "leere PCM-Daten"):
                self.audio._via_gemini_tts("Text", str(ziel))
            self.assertFalse(ziel.exists())

    def test_deepgram_bleibt_als_fallback_erhalten(self):
        with (
            patch.object(self.audio, "_via_deepgram") as deepgram,
            patch.object(self.audio.kosten_tracking, "logge_api_kosten"),
            patch.object(self.audio, "hole_supabase_client", return_value=MagicMock()),
        ):
            self.audio.text_zu_audio("Text", "episode.mp3", anbieter="deepgram")
        deepgram.assert_called_once()

    def test_gemini_ist_standard_und_protokolliert_beide_tokenmengen(self):
        supabase = MagicMock()
        with (
            patch.object(self.audio, "_via_gemini_tts", return_value=(12, 34)) as gemini,
            patch.object(self.audio, "hole_supabase_client", return_value=supabase),
            patch.object(self.audio.kosten_tracking, "logge_api_kosten") as logge_kosten,
        ):
            self.audio.text_zu_audio("Text", "episode.mp3")

        gemini.assert_called_once_with("Text", "episode.mp3")
        logge_kosten.assert_called_once_with(
            supabase,
            dienst="gemini_tts",
            modell=self.audio.GEMINI_TTS_MODEL,
            schritt="audio_synthese",
            einheit_typ="tokens",
            menge_input=12,
            menge_output=34,
            lauf_id=None,
            episode_id=None,
        )

    def test_gemini_tts_fehlschlag_faellt_automatisch_auf_deepgram_zurueck(self):
        import io
        from contextlib import redirect_stdout

        supabase = MagicMock()
        with (
            patch.object(self.audio, "_via_gemini_tts", side_effect=RuntimeError("Kontingent erschöpft")),
            patch.object(self.audio, "_via_deepgram") as deepgram,
            patch.object(self.audio, "hole_supabase_client", return_value=supabase),
            patch.object(self.audio.kosten_tracking, "logge_api_kosten") as logge_kosten,
        ):
            ausgabe = io.StringIO()
            with redirect_stdout(ausgabe):
                self.audio.text_zu_audio("Text", "episode.mp3")

        deepgram.assert_called_once()
        self.assertIn("WARNUNG", ausgabe.getvalue())
        self.assertIn("gemini_tts", ausgabe.getvalue())
        logge_kosten.assert_called_once_with(
            supabase,
            dienst="deepgram",
            modell=self.audio.DEEPGRAM_MODEL,
            schritt="audio_synthese",
            einheit_typ="zeichen",
            menge_input=len("Text"),
            lauf_id=None,
            episode_id=None,
        )

    def test_gemini_und_deepgram_fehlschlag_faellt_auf_elevenlabs_zurueck(self):
        supabase = MagicMock()
        with (
            patch.object(self.audio, "_via_gemini_tts", side_effect=RuntimeError("Preview down")),
            patch.object(self.audio, "_via_deepgram", side_effect=RuntimeError("Deepgram down")),
            patch.object(self.audio, "_via_elevenlabs") as elevenlabs,
            patch.object(self.audio, "hole_supabase_client", return_value=supabase),
            patch.object(self.audio.kosten_tracking, "logge_api_kosten") as logge_kosten,
        ):
            self.audio.text_zu_audio("Text", "episode.mp3")

        elevenlabs.assert_called_once()
        logge_kosten.assert_called_once_with(
            supabase,
            dienst="elevenlabs",
            modell=self.audio.ELEVENLABS_MODEL,
            schritt="audio_synthese",
            einheit_typ="zeichen",
            menge_input=len("Text"),
            lauf_id=None,
            episode_id=None,
        )

    def test_alle_tts_anbieter_fehlgeschlagen_wirft_letzten_fehler(self):
        with (
            patch.object(self.audio, "_via_gemini_tts", side_effect=RuntimeError("Gemini down")),
            patch.object(self.audio, "_via_deepgram", side_effect=RuntimeError("Deepgram down")),
            patch.object(self.audio, "_via_elevenlabs", side_effect=RuntimeError("ElevenLabs down")),
            patch.object(self.audio.kosten_tracking, "logge_api_kosten") as logge_kosten,
        ):
            with self.assertRaisesRegex(RuntimeError, "ElevenLabs down"):
                self.audio.text_zu_audio("Text", "episode.mp3")

        logge_kosten.assert_not_called()


class OrchestrierungsTests(unittest.TestCase):
    def test_morgenlauf_importiert_keine_recherche(self):
        text = Path("morgenlauf.py").read_text(encoding="utf-8")
        self.assertNotIn("recherche_und_redaktion", text)
        self.assertNotIn("rss_einlesen", text)

    def test_unbelegte_fakten_blockieren_audio(self):
        os.environ.setdefault("SUPABASE_URL", "http://localhost")
        os.environ.setdefault("SUPABASE_KEY", "test")
        modul = importlib.import_module("morgenlauf")
        self.assertTrue(modul.faktencheck_blockiert_audio({"widerspruch": 1, "nicht_belegt": 0}))
        self.assertTrue(modul.faktencheck_blockiert_audio({"widerspruch": 0, "nicht_belegt": 1}))
        self.assertFalse(modul.faktencheck_blockiert_audio({"widerspruch": 0, "nicht_belegt": 0}))


class _FakeQueryResult:
    def __init__(self, data):
        self.data = data


class _FakeTable:
    """Minimales Supabase-Query-Builder-Double: genug, um die konkreten
    Aufruf-Ketten aus recherche_und_redaktion.py nachzubilden (select/eq/
    is_/in_/update/execute), ohne echte Supabase-Verbindung."""

    def __init__(self, fake_supabase, name):
        self.fake_supabase = fake_supabase
        self.name = name
        self.status_filter = None
        self.in_values = None
        self.update_daten = None
        self.eq_id = None

    def select(self, *_a, **_k):
        return self

    def eq(self, feld, wert):
        if feld == "status":
            self.status_filter = wert
        if feld == "id":
            self.eq_id = wert
        return self

    def is_(self, _feld, _wert):
        return self

    def in_(self, _feld, werte):
        self.in_values = set(werte)
        return self

    def update(self, daten):
        self.update_daten = daten
        return self

    def execute(self):
        if self.update_daten is not None:
            self.fake_supabase.updates.append((self.name, self.eq_id, self.update_daten))
            return _FakeQueryResult([])
        if self.name == "themen":
            return _FakeQueryResult(self.fake_supabase.themen)
        if self.name == "redaktion_entscheidungen":
            return _FakeQueryResult(
                self.fake_supabase.entscheidungen_nach_status.get(self.status_filter, [])
            )
        if self.name == "agent_vorschlaege":
            return _FakeQueryResult(
                [v for v in self.fake_supabase.vorschlaege if v["id"] in self.in_values]
            )
        if self.name == "rohnachrichten":
            return _FakeQueryResult(
                [r for r in self.fake_supabase.rohnachrichten if r["id"] in self.in_values]
            )
        if self.name == "agenten_konfiguration":
            return _FakeQueryResult(self.fake_supabase.agenten)
        raise AssertionError(f"Unerwartete Tabelle in Test: {self.name}")


class _FakeSupabase:
    def __init__(self, themen, entscheidungen_nach_status, vorschlaege, rohnachrichten, agenten):
        self.themen = themen
        self.entscheidungen_nach_status = entscheidungen_nach_status
        self.vorschlaege = vorschlaege
        self.rohnachrichten = rohnachrichten
        self.agenten = agenten
        self.updates: list[tuple[str, str, dict]] = []

    def table(self, name):
        return _FakeTable(self, name)


def _gemini_json_antwort(payload) -> MagicMock:
    return MagicMock(
        text=json.dumps(payload),
        usage_metadata=MagicMock(prompt_token_count=1, candidates_token_count=1),
    )


class NotfallAuffuellungTests(unittest.TestCase):
    """Deckt die Notfall-Auffüllung ab (recherche_und_redaktion.py,
    fuehre_notfall_auffuellung_aus): Sicherheitsnetz, das bei zu wenig
    offenen Themen zunächst zurückgestellte, dann abgelehnte Vorschläge mit
    gelockertem Maßstab erneut prüfen lässt - siehe Kontext: morgenlauf
    scheiterte an zu kurzem Manuskript, weil nur 3 offene Themen vorlagen."""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("GEMINI_API_KEY", "test")
        os.environ.setdefault("SUPABASE_URL", "http://localhost")
        os.environ.setdefault("SUPABASE_KEY", "test")
        cls.modul = importlib.import_module("recherche_und_redaktion")

    def test_genug_themen_vorhanden_ist_no_op(self):
        supabase = _FakeSupabase(
            themen=[{"id": f"t{i}"} for i in range(self.modul.MIN_THEMEN_FUER_EPISODE)],
            entscheidungen_nach_status={"akzeptiert": []},
            vorschlaege=[],
            rohnachrichten=[],
            agenten=[],
        )
        chat_model = MagicMock()

        with (
            patch.object(self.modul, "hole_supabase_client", return_value=supabase),
            patch.object(self.modul, "hole_chat_model", return_value=chat_model),
        ):
            anzahl = self.modul.fuehre_notfall_auffuellung_aus()

        self.assertEqual(anzahl, 0)
        chat_model.generate_content.assert_not_called()
        self.assertEqual(supabase.updates, [])

    def test_zu_wenig_themen_zieht_erst_zurueckgestellte_dann_abgelehnte(self):
        supabase = _FakeSupabase(
            themen=[{"id": "t1"}],
            entscheidungen_nach_status={
                "akzeptiert": [],
                "zurueckgestellt": [
                    {"id": "e1", "vorschlag_id": "v1", "begruendung": "Gut, aber gestern kein Platz"}
                ],
                "abgelehnt": [
                    {"id": "e2", "vorschlag_id": "v2", "begruendung": "Nur mittlere Relevanz"},
                    {"id": "e3", "vorschlag_id": "v3", "begruendung": "Duplikat einer älteren Meldung"},
                ],
            },
            vorschlaege=[
                {"id": "v1", "rohnachricht_id": "r1"},
                {"id": "v2", "rohnachricht_id": "r2"},
                {"id": "v3", "rohnachricht_id": "r3"},
            ],
            rohnachrichten=[
                {"id": "r1", "titel": "Thema A", "text": "Text A"},
                {"id": "r2", "titel": "Thema B", "text": "Text B"},
                {"id": "r3", "titel": "Thema C (Duplikat)", "text": "Text C"},
            ],
            agenten=[{"id": "a1", "name": "Redaktion", "fokus_beschreibung": "KMU-Fokus"}],
        )
        chat_model = MagicMock()
        chat_model.generate_content.side_effect = [
            _gemini_json_antwort([{"index": 0, "begruendung": "Sachlich korrekt, trotzdem aufnehmen"}]),
            _gemini_json_antwort([{"index": 0, "begruendung": "Kein Duplikat, bleibt drin"}]),
        ]

        with (
            patch.object(self.modul, "hole_supabase_client", return_value=supabase),
            patch.object(self.modul, "hole_chat_model", return_value=chat_model),
            patch.object(self.modul.kosten_tracking, "logge_api_kosten"),
        ):
            anzahl = self.modul.fuehre_notfall_auffuellung_aus()

        self.assertEqual(anzahl, 2)
        self.assertEqual(chat_model.generate_content.call_count, 2)

        aktualisierte_ids = {eq_id for (_tabelle, eq_id, _daten) in supabase.updates}
        self.assertEqual(aktualisierte_ids, {"e1", "e2"})
        self.assertNotIn("e3", aktualisierte_ids)

        daten_nach_id = {eq_id: daten for (_tabelle, eq_id, daten) in supabase.updates}
        self.assertEqual(daten_nach_id["e1"]["status"], "akzeptiert")
        self.assertTrue(daten_nach_id["e1"]["akzeptiert"])
        self.assertIn("zurueckgestellt", daten_nach_id["e1"]["begruendung"])
        self.assertIn("abgelehnt", daten_nach_id["e2"]["begruendung"])


if __name__ == "__main__":
    unittest.main()
