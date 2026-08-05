import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bot import chat_context
from bot.channel_context import fetch_recent_channel_context
from services import memory_queries


def _author(user_id, display_name, *, bot=False):
    return SimpleNamespace(id=user_id, display_name=display_name, name=display_name, bot=bot)


def _message(message_id, author, content, *, reply_to=None):
    reference = SimpleNamespace(message_id=reply_to) if reply_to else None
    return SimpleNamespace(
        id=message_id,
        author=author,
        content=content,
        attachments=[],
        reference=reference,
    )


class _Channel:
    def __init__(self, messages):
        self.id = 20
        self.messages = messages
        self.history_kwargs = None

    async def history(self, **kwargs):
        self.history_kwargs = kwargs
        for message in self.messages:
            yield message


class EphemeralChannelContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_preserves_speakers_and_shared_order_without_other_bots(self):
        multivac = _author(999, "Multivac", bot=True)
        alice = _author(101, "Alice")
        bob = _author(202, "Bob")
        noise_bot = _author(303, "OtherBot", bot=True)
        channel = _Channel(
            [
                _message(4, bob, "yes, use Alice's plan", reply_to=3),
                _message(3, multivac, "That plan is sound", reply_to=2),
                _message(2, noise_bot, "automated noise"),
                _message(1, alice, "Let's use blue-green deployment"),
            ]
        )
        current = _message(5, bob, "what do you think?")
        current.channel = channel

        context = await fetch_recent_channel_context(current, multivac)

        self.assertEqual([item["role"] for item in context], ["user", "assistant", "user"])
        self.assertIn("Alice; user_id=101", context[0]["content"])
        self.assertIn("Multivac in shared channel", context[1]["content"])
        self.assertIn("Bob; user_id=202", context[2]["content"])
        self.assertNotIn("automated noise", "\n".join(item["content"] for item in context))
        self.assertIs(channel.history_kwargs["before"], current)


class IndexedChannelFallbackTests(unittest.TestCase):
    @patch("services.memory_queries.search_raw")
    def test_channel_window_filters_channel_not_requesting_user(self, search_raw):
        search_raw.return_value = {
            "hits": {
                "hits": [
                    {
                        "_source": {
                            "message_id": "current",
                            "role": "user",
                            "user_id": "202",
                            "content": "current request",
                        }
                    },
                    {
                        "_source": {
                            "message_id": "2",
                            "role": "assistant",
                            "user_id": "101",
                            "content": "Earlier answer to Alice",
                        }
                    },
                    {
                        "_source": {
                            "message_id": "1",
                            "role": "user",
                            "user_id": "101",
                            "content": "Alice's earlier request",
                        }
                    },
                ]
            }
        }

        window = memory_queries.build_channel_message_window(
            guild_id="g",
            channel_id="c",
            current_user_id="202",
            current_display_name="Bob",
            exclude_message_id="current",
        )

        filters = search_raw.call_args.args[0]["bool"]["filter"]
        self.assertEqual(
            filters,
            [{"term": {"guild_id": "g"}}, {"term": {"channel_id": "c"}}],
        )
        self.assertNotIn({"term": {"user_id": "202"}}, filters)
        self.assertIn("user_id=101", window[0]["content"])
        self.assertIn("Earlier answer to Alice", window[1]["content"])
        self.assertNotIn("current request", "\n".join(item["content"] for item in window))


class SharedPromptIsolationTests(unittest.TestCase):
    def test_group_context_is_shared_but_only_current_profile_is_loaded(self):
        message = SimpleNamespace(
            id=5,
            guild=SimpleNamespace(id=10),
            channel=SimpleNamespace(id=20),
            author=_author(202, "Bob"),
        )
        shared = [
            {"role": "user", "content": "[Channel speaker: Alice; user_id=101]\nUse blue-green"},
            {"role": "assistant", "content": "[Multivac in shared channel]\nGood plan"},
        ]

        with patch.object(
            chat_context, "build_timeline_prompt_block", return_value="personal timeline"
        ), patch.object(
            chat_context, "search_history_for_context", return_value=None
        ), patch(
            "bot.response_policy.build_user_awareness_block",
            side_effect=lambda user_id, display_name=None: f"private-profile-for-{user_id}",
        ), patch(
            "bot.response_policy.get_user_instruction", return_value=None
        ), patch(
            "bot.persona.get_conversation_persona_enabled", return_value=False
        ):
            messages = chat_context.build_chat_context(
                message,
                202,
                "should we do that?",
                channel_context_messages=shared,
            )

        payload = "\n".join(str(item.get("content")) for item in messages)
        self.assertIn("multi-person Discord channel", payload)
        self.assertIn("Current requester: Bob; user_id=202", payload)
        self.assertIn("Channel speaker: Alice; user_id=101", payload)
        self.assertIn("private-profile-for-202", payload)
        self.assertNotIn("private-profile-for-101", payload)
        self.assertLess(payload.index("Use blue-green"), payload.index("should we do that?"))


if __name__ == "__main__":
    unittest.main()
