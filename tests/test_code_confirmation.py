import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord_bot


class CodeGenerationConfirmationTests(unittest.IsolatedAsyncioTestCase):
    async def test_natural_code_change_cancels_before_proposal_or_model_call(self):
        message = SimpleNamespace(
            author=SimpleNamespace(id=123),
            reply=AsyncMock(),
        )
        status = SimpleNamespace(delete=AsyncMock())

        with (
            patch.object(discord_bot.bot, "is_owner", new=AsyncMock(return_value=False)),
            patch.object(discord_bot, "list_code_proposals", return_value=[]),
            patch.object(
                discord_bot,
                "_confirm_code_generation",
                new=AsyncMock(return_value=False),
            ) as confirm,
            patch.object(discord_bot, "create_code_proposal") as create,
            patch.object(
                discord_bot,
                "generate_code_patch",
                new=AsyncMock(),
            ) as generate,
        ):
            await discord_bot._natural_code_proposal(
                message,
                "code_change",
                "change your code to add diagnostics",
                status_msg=status,
            )

        confirm.assert_awaited_once()
        create.assert_not_called()
        generate.assert_not_awaited()

    async def test_code_generate_cancels_before_model_call(self):
        ctx = SimpleNamespace(author=SimpleNamespace(id=456), reply=AsyncMock())
        proposal = {
            "id": 7,
            "status": "draft",
            "request": "Add bounded diagnostics.",
        }

        with (
            patch.object(discord_bot, "get_code_proposal", return_value=proposal),
            patch.object(
                discord_bot,
                "_confirm_code_generation",
                new=AsyncMock(return_value=False),
            ) as confirm,
            patch.object(
                discord_bot,
                "generate_code_patch",
                new=AsyncMock(),
            ) as generate,
        ):
            await discord_bot.code_generate.callback(ctx, 7)

        confirm.assert_awaited_once()
        generate.assert_not_awaited()

    async def test_reflection_propose_cancels_before_proposal_or_model_call(self):
        ctx = SimpleNamespace(author=SimpleNamespace(id=789), reply=AsyncMock())
        idea = {
            "id": 3,
            "status": "active",
            "title": "Bound retries",
            "problem": "A retry can run too long.",
        }
        store = SimpleNamespace(get_idea=MagicMock(return_value=idea))
        worker = SimpleNamespace(store=store)

        with (
            patch.object(discord_bot, "_get_reflection_worker", return_value=worker),
            patch.object(discord_bot, "list_code_proposals", return_value=[]),
            patch.object(
                discord_bot,
                "_confirm_code_generation",
                new=AsyncMock(return_value=False),
            ) as confirm,
            patch.object(discord_bot, "create_code_proposal") as create,
            patch.object(
                discord_bot,
                "generate_planned_idea_patch",
                new=AsyncMock(),
            ) as generate,
        ):
            await discord_bot.reflection_propose.callback(ctx, 3)

        confirm.assert_awaited_once()
        create.assert_not_called()
        generate.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
