import importlib.util
import sys
import unittest
from pathlib import Path

MODULE_PATH = (
    Path(__file__).parents[1] / "NHCogs" / "custom_commands" / "presentation.py"
)


def load_presentation_module():
    name = "custom_commands_presentation_subject"
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class ExactResponsePresentationTests(unittest.TestCase):
    def test_safe_content_uses_a_code_block_without_changing_whitespace(self):
        presentation = load_presentation_module()
        content = "  left   right\ntrailing  "

        result = presentation.present_exact_response(content)

        self.assertEqual(result.description, f"```\n{content}\n```")
        self.assertIsNone(result.attachment)

    def test_code_fence_content_falls_back_to_exact_utf8_bytes(self):
        presentation = load_presentation_module()
        content = "before\n```py\nvalue = 1\n```\nafter  "

        result = presentation.present_exact_response(content)

        self.assertIsNone(result.description)
        self.assertEqual(result.attachment, content.encode("utf-8"))

    def test_transcript_keeps_each_response_bytes_recoverable(self):
        presentation = load_presentation_module()
        responses = ("first  ", " second\nline ")

        transcript = presentation.build_response_transcript(responses)

        for index, content in enumerate(responses, start=1):
            encoded = content.encode("utf-8")
            marker = f"===== Response {index}: {len(encoded)} bytes =====\n".encode()
            start = transcript.index(marker) + len(marker)
            self.assertEqual(transcript[start : start + len(encoded)], encoded)


if __name__ == "__main__":
    unittest.main()
