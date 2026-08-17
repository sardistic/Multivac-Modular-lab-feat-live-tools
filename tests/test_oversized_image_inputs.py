import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

from PIL import Image

from bot.message_inputs import (
    DOWNSCALE_MAX_DIM,
    DOWNSCALE_SOURCE_BYTES,
    MAX_ATTACHMENT_BYTES,
    _downscale_image_bytes,
    collect_image_inputs,
)


def _png_bytes(width: int, height: int) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (width, height), color="white").save(buf, format="PNG")
    return buf.getvalue()


def _attachment(size: int, data: bytes, filename: str = "20260817_115038.png"):
    return SimpleNamespace(
        content_type="image/png",
        filename=filename,
        size=size,
        url=f"https://cdn.discordapp.com/attachments/1/2/{filename}?ex=signed",
        read=AsyncMock(return_value=data),
    )


def _message(attachments):
    return SimpleNamespace(attachments=attachments, embeds=[], content="", message_snapshots=[])


class OversizedImageInputTests(unittest.IsolatedAsyncioTestCase):
    """A photo Discord accepted must never vanish between upload and model.

    Dropping it left image requests routed to plain chat with no image, so the
    bot asked for a picture the user had already attached.
    """

    def test_downscale_fits_budget_and_caps_dimensions(self):
        reduced = _downscale_image_bytes(_png_bytes(4000, 3000))

        self.assertIsNotNone(reduced)
        data, mime = reduced
        self.assertEqual(mime, "image/jpeg")
        self.assertLessEqual(len(data), MAX_ATTACHMENT_BYTES)
        with Image.open(BytesIO(data)) as img:
            self.assertLessEqual(max(img.size), DOWNSCALE_MAX_DIM)

    def test_downscale_rejects_undecodable_bytes(self):
        self.assertIsNone(_downscale_image_bytes(b"not an image"))

    async def test_oversized_attachment_is_downscaled_not_dropped(self):
        attachment = _attachment(MAX_ATTACHMENT_BYTES + 1, _png_bytes(4000, 3000))
        source_urls = []
        unusable = []
        convert = AsyncMock(return_value="data:image/png;base64,aW1hZ2U=")

        images = await collect_image_inputs(
            _message([attachment]), None, convert, source_urls, unusable
        )

        self.assertEqual(len(images), 1)
        self.assertTrue(images[0].startswith("data:image/jpeg;base64,"))
        self.assertEqual(source_urls, [attachment.url])
        self.assertEqual(unusable, [])
        convert.assert_not_awaited()

    async def test_oversized_attachment_in_reply_is_downscaled(self):
        attachment = _attachment(MAX_ATTACHMENT_BYTES + 1, _png_bytes(4000, 3000))
        unusable = []

        images = await collect_image_inputs(
            _message([]), _message([attachment]), AsyncMock(), [], unusable
        )

        self.assertEqual(len(images), 1)
        self.assertTrue(images[0].startswith("data:image/jpeg;base64,"))
        self.assertEqual(unusable, [])

    async def test_undecodable_oversized_attachment_is_reported_unusable(self):
        attachment = _attachment(MAX_ATTACHMENT_BYTES + 1, b"not an image")
        unusable = []

        images = await collect_image_inputs(
            _message([attachment]), None, AsyncMock(), [], unusable
        )

        self.assertEqual(images, [])
        self.assertEqual(unusable, [attachment.filename])

    async def test_attachment_past_downscale_ceiling_is_not_downloaded(self):
        attachment = _attachment(DOWNSCALE_SOURCE_BYTES + 1, _png_bytes(64, 64))
        unusable = []

        images = await collect_image_inputs(
            _message([attachment]), None, AsyncMock(), [], unusable
        )

        self.assertEqual(images, [])
        self.assertEqual(unusable, [attachment.filename])
        attachment.read.assert_not_awaited()

    async def test_normal_attachment_still_uses_the_url_path(self):
        attachment = _attachment(500_000, _png_bytes(64, 64))
        convert = AsyncMock(return_value="data:image/png;base64,aW1hZ2U=")
        unusable = []

        images = await collect_image_inputs(
            _message([attachment]), None, convert, [], unusable
        )

        self.assertEqual(images, ["data:image/png;base64,aW1hZ2U="])
        self.assertEqual(unusable, [])
        attachment.read.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
