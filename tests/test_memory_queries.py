import unittest
from unittest.mock import patch

from services import memory_queries


class MemoryQueryTests(unittest.TestCase):
    @patch("services.memory_queries.search_raw")
    def test_search_history_filters_user_role_for_self_recall(self, mock_search_raw):
        mock_search_raw.return_value = {"hits": {"hits": []}}

        memory_queries.search_history_for_context(
            guild_id="guild-1",
            channel_id="channel-1",
            user_id="user-1",
            query_text="when did i last mention gengar",
            limit=5,
        )

        query = mock_search_raw.call_args.args[0]
        self.assertIn({"term": {"role": "user"}}, query["bool"]["filter"])
        self.assertEqual(query["bool"]["must"], [{"match": {"content": "gengar"}}])

    @patch("services.memory_queries.search_raw")
    def test_search_history_filters_assistant_role_for_bot_recall(self, mock_search_raw):
        mock_search_raw.return_value = {"hits": {"hits": []}}

        memory_queries.search_history_for_context(
            guild_id="guild-1",
            channel_id="channel-1",
            user_id="user-1",
            query_text='when did you say "reset personality"',
            limit=5,
        )

        query = mock_search_raw.call_args.args[0]
        self.assertIn({"term": {"role": "assistant"}}, query["bool"]["filter"])
        self.assertEqual(query["bool"]["must"], [{"match": {"content": "reset personality"}}])

    @patch("services.memory_queries.search_raw")
    def test_fetch_matches_recent_uses_topic_instead_of_full_recall_question(self, mock_search_raw):
        mock_search_raw.return_value = {"hits": {"hits": [{"_source": {"content": "gengar"}}]}}

        memory_queries.fetch_matches_recent(
            guild_id="guild-1",
            channel_id="channel-1",
            user_id="user-1",
            query="did i mention gengar recently",
            size=3,
        )

        query = mock_search_raw.call_args_list[0].args[0]
        self.assertIn({"term": {"role": "user"}}, query["bool"]["filter"])
        self.assertEqual(
            query["bool"]["must"],
            [{"match": {"content": {"query": "gengar", "operator": "and"}}}],
        )


if __name__ == "__main__":
    unittest.main()
