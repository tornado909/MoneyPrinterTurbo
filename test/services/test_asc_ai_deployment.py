import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent


class TestAscAIDeployment(unittest.TestCase):
    def test_chatterbox_dockerfile_pins_both_upstream_revisions(self):
        source = (ROOT / "docker/chatterbox/Dockerfile.cpu").read_text(
            encoding="utf-8"
        )
        api_sha = "a5f466128e4baa8e4cceb3bba9b7ca9de6f7ec6b"
        multilingual_sha = "c33cc0286ad166f14dd143c02c2a1c3309ab4727"

        self.assertIn(f"CHATTERBOX_API_SHA={api_sha}", source)
        self.assertIn(f"CHATTERBOX_MULTILINGUAL_SHA={multilingual_sha}", source)
        self.assertIn('git checkout "$CHATTERBOX_API_SHA"', source)
        self.assertIn(
            "chatterbox-multilingual.git@$CHATTERBOX_MULTILINGUAL_SHA",
            source,
        )
        self.assertNotIn("@exp", source)
        self.assertIn("DEVICE=cpu", source)

    def test_chatterbox_overlay_is_cpu_only_private_and_persistent(self):
        source = (ROOT / "docker-compose.chatterbox.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("DEVICE: cpu", source)
        self.assertIn('"127.0.0.1:4123:4123"', source)
        self.assertIn("/srv/ai/cache/chatterbox:/cache", source)
        self.assertIn("/srv/ai-data/chatterbox/voices:/voices", source)
        self.assertIn(
            "MPT_CHATTERBOX_BASE_URL: http://chatterbox-tts:4123/v1",
            source,
        )
        self.assertNotRegex(source, re.compile(r"\bgpus?:|runtime:\s*nvidia"))

    def test_russian_chatterbox_default_is_consistent(self):
        config_text = (ROOT / "config.example.toml").read_text(encoding="utf-8")
        config = tomllib.loads(config_text)
        voice = (ROOT / "app/services/voice.py").read_text(encoding="utf-8")
        controller = (
            ROOT / "app/controllers/v1/asc_ai.py"
        ).read_text(encoding="utf-8")

        self.assertEqual(config["chatterbox"]["default_voice"], "ru-default")
        self.assertEqual(config["chatterbox"]["voices"], ["ru-default"])
        self.assertNotIn("default_voice", config["kokoro"])
        self.assertEqual(config["ui"]["voice_name"], "chatterbox:ru-default")
        self.assertIn('["chatterbox:ru-default"]', voice)
        self.assertIn("chatterbox:ru-default", controller)
        self.assertNotIn(
            "chatterbox:default-Female",
            config_text + voice + controller,
        )

    def test_asc_ai_overlay_does_not_shadow_release_image_code(self):
        overlay = (ROOT / "docker-compose.asc-ai.yml").read_text(encoding="utf-8")
        release = (ROOT / "docker-compose.release.yml").read_text(encoding="utf-8")

        self.assertIn("ghcr.io/tornado909/moneyprinterturbo:latest", release)
        self.assertNotIn("./:/MoneyPrinterTurbo", overlay)
        self.assertIn("/srv/ai-data:/srv/ai-data", overlay)
        self.assertIn("docker-compose.release.yml", overlay)

    def test_acceptance_full_render_is_bounded_and_local_only(self):
        source = (
            ROOT / "scripts/asc-ai-acceptance.sh"
        ).read_text(encoding="utf-8")
        self.assertIn('"max_local_video_scenes": 0', source)
        self.assertIn('"max_public_image_scenes": 0', source)
        self.assertIn('"public_research_enabled": False', source)
        self.assertIn('"public_media_enabled": False', source)
        self.assertIn('"bgm_type": ""', source)
        self.assertIn('if [[ "$state" == "1" ]]', source)
        self.assertIn('if [[ "$state" == "-1" ]]', source)
        self.assertIn("/api/v1/asc-ai/production/$TASK_ID/evidence", source)
        self.assertNotIn("openrouter", source.lower())
        self.assertNotIn("ofox", source.lower())
        self.assertNotIn("seedance", source.lower())

    def test_cost_aware_public_defaults_are_explicit(self):
        config = tomllib.loads(
            (ROOT / "config.example.toml").read_text(encoding="utf-8")
        )
        asc = config["asc_ai"]
        self.assertTrue(asc["public_research_enabled"])
        self.assertTrue(asc["public_media_enabled"])
        self.assertEqual(asc["director_max_public_image_scenes"], 2)
        self.assertFalse(asc["public_media_allow_share_alike"])
        self.assertTrue(asc["final_qc_enabled"])
        self.assertTrue(asc["final_qc_required"])
        self.assertTrue(asc["final_qc_visual_analysis"])

    def test_bootstrap_uploads_voice_with_russian_language_metadata(self):
        source = (
            ROOT / "scripts/bootstrap-chatterbox-ru-voice.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("CHATTERBOX_RU_VOICE_NAME:-ru-default", source)
        self.assertIn('-F "voice_name=$VOICE_NAME"', source)
        self.assertIn('-F "language=ru"', source)
        self.assertIn("/v1/voices", source)


if __name__ == "__main__":
    unittest.main()
