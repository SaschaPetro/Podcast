import importlib
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


if __name__ == "__main__":
    unittest.main()
