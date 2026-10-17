#!/usr/bin/env python3
"""The gateway's input check (docker/server/inputs.py): what the loaded model
takes, and the refusal for anything else. No dependencies, no server."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "docker/server"))
import inputs  # noqa: E402

TEXT_ONLY = inputs.accepted({"has_image_understanding": False, "has_audio_understanding": False})
VISION = inputs.accepted({"has_image_understanding": True})


def msg(*parts):
    return [{"role": "user", "content": list(parts)}]


TEXT = {"type": "text", "text": "what is this?"}
IMAGE = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
AUDIO = {"type": "input_audio", "input_audio": {"data": "AAAA", "format": "wav"}}
VIDEO = {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,AAAA"}}


class Inputs(unittest.TestCase):
    def test_what_sglang_reports_becomes_the_flag(self):
        self.assertEqual(TEXT_ONLY, {"text"})
        self.assertEqual(VISION, {"text", "image"})
        self.assertEqual(inputs.accepted({"has_image_understanding": True, "has_audio_understanding": True}),
                         {"text", "image", "audio"})
        self.assertEqual(inputs.accepted({}), {"text"})  # an older SGLang that doesn't say: text only

    def test_text_always_passes(self):
        for accepts in (TEXT_ONLY, VISION, None):
            self.assertIsNone(inputs.refusal([{"role": "user", "content": "hi"}], accepts, "m"))
            self.assertIsNone(inputs.refusal(msg(TEXT), accepts, "m"))

    def test_a_picture_is_refused_by_a_text_only_model_and_says_why(self):
        why = inputs.refusal(msg(TEXT, IMAGE), TEXT_ONLY, "Qwen2.5-Coder-14B-Instruct-AWQ")
        self.assertIn("The loaded model (Qwen2.5-Coder-14B-Instruct-AWQ) can't take pictures: it takes text only", why)
        self.assertIn("vision encoder", why)
        self.assertIsNone(inputs.refusal(msg(TEXT, IMAGE), VISION, "m"))
        self.assertIsNone(inputs.refusal(msg({"type": "input_image", "image_url": "x"}), VISION, "m"))

    def test_audio_needs_an_audio_model(self):
        self.assertIn("can't take audio: it takes text and pictures only", inputs.refusal(msg(AUDIO), VISION, "m"))
        self.assertIn("can't take audio and pictures", inputs.refusal(msg(IMAGE, AUDIO), TEXT_ONLY, "m"))

    def test_video_is_never_served(self):
        everything = frozenset({"text", "image", "audio"})
        for accepts in (TEXT_ONLY, everything, None):
            self.assertIn("Video input isn't supported", inputs.refusal(msg(VIDEO), accepts, "m"))

    def test_while_loading_only_what_is_never_served_is_refused(self):
        self.assertIsNone(inputs.refusal(msg(IMAGE), None, "m"))

    def test_an_unknown_part_type_is_refused_rather_than_passed_on(self):
        why = inputs.refusal(msg({"type": "input_file", "file_id": "x"}), VISION, "m")
        self.assertIn("'input_file' parts", why)

    def test_parts_in_any_message_count(self):
        messages = [{"role": "system", "content": "be brief"}, {"role": "user", "content": [IMAGE]},
                    {"role": "assistant", "content": "a cat"}, {"role": "user", "content": "and now?"}]
        self.assertIsNotNone(inputs.refusal(messages, TEXT_ONLY, "m"))
        self.assertEqual(inputs.kinds_in([None, {"content": None}, {"content": [None, "x"]}]), set())


if __name__ == "__main__":
    unittest.main()
