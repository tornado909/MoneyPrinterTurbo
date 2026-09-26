import re
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
        config = (ROOT / "config.example.toml").read_text(encoding="utf-8")
        voice = (ROOT / "app/services/voice.py").read_text(encoding="utf-8")
        controller = (
            ROOT / "app/controllers/v1/asc_ai.py"
        ).read_text(encoding="utf-8")

        self.assertIn('default_voice = "ru-default"', config)
        self.assertIn('voice_name = "chatterbox:ru-default"', config)
        self.assertIn('["chatterbox:ru-default"]', voice)
        self.assertIn("chatterbox:ru-default", controller)
        self.assertNotIn("chatterbox:default-Female", config + voice + controller)

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
