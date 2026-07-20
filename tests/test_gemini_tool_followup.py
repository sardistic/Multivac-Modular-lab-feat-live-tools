import unittest
from types import SimpleNamespace
from unittest.mock import patch

from providers import gemini_text


@unittest.skipIf(gemini_text.types is None, "google-genai is unavailable")
class GeminiToolFollowupTests(unittest.TestCase):
    def test_search_tool_followup_preserves_thought_signature(self):
        types = gemini_text.types
        tool_part = types.Part(
            function_call=types.FunctionCall(
                name="search_elasticsearch_resource",
                args={"query_string": "old request"},
            ),
            thought_signature=b"signed-call",
        )
        first_chunk = SimpleNamespace(
            usage_metadata=None,
            candidates=[
                SimpleNamespace(
                    finish_reason="STOP",
                    content=SimpleNamespace(parts=[tool_part]),
                )
            ],
        )
        final_response = SimpleNamespace(
            usage_metadata=None,
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=[types.Part(text="found it")]))],
        )

        class _Models:
            def __init__(self):
                self.follow_contents = None

            def generate_content_stream(self, **kwargs):
                return iter([first_chunk])

            def generate_content(self, **kwargs):
                self.follow_contents = kwargs["contents"]
                return final_response

        models = _Models()
        client = SimpleNamespace(models=models)
        with patch.object(gemini_text, "get_gemini_client", return_value=client), patch.object(
            gemini_text, "search_elasticsearch_resource", return_value="result"
        ):
            text, artifacts = gemini_text.generate_gemini_text("remember this")

        self.assertEqual(text, "found it")
        self.assertEqual(artifacts, [])
        returned_part = models.follow_contents[-2].parts[0]
        self.assertEqual(returned_part.thought_signature, b"signed-call")


if __name__ == "__main__":
    unittest.main()
