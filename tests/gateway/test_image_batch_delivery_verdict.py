"""Regression for #160210: a text link is not a delivered image."""

import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from plugins.platforms.mattermost.adapter import MattermostAdapter
from plugins.platforms.slack.adapter import SlackAdapter


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["slack", "mattermost"])
@pytest.mark.parametrize("with_image", [False, True])
async def test_batch_success_requires_image_delivery(platform, with_image, monkeypatch, tmp_path):
    from tools import url_safety

    monkeypatch.setattr(url_safety, "is_safe_url", lambda _url: False)
    config = PlatformConfig(enabled=True, token="test-token")
    if platform == "slack":
        adapter = SlackAdapter(config)
        adapter._app = MagicMock()
        adapter._dm_target = AsyncMock(return_value="channel")
        upload = AsyncMock()
        client = MagicMock()
        client.files_upload_v2 = upload
        adapter._client_for = MagicMock(return_value=client)
        adapter._record_uploaded_file_thread = MagicMock()
    else:
        adapter = MattermostAdapter(config)
        upload = AsyncMock(return_value="image-id")
        adapter._upload_file = upload
        adapter._post_message = AsyncMock(return_value={"id": "image-post"})
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="link"))
    url = "http://169.254.169.254/latest/meta-data/"

    images = [(url, "metadata")]
    if with_image:
        image = tmp_path / "image.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\n")
        images.append((image.as_uri(), "picture"))

    result = await adapter.send_multiple_images("channel", images)

    adapter.send.assert_awaited_once_with("channel", f"metadata\n{url}", metadata=None)
    if with_image:
        upload.assert_awaited_once()
    else:
        upload.assert_not_awaited()
    assert result.success is with_image
    assert result.error == (None if with_image else "all images failed to send")


class _LinkOnlyAdapter(BasePlatformAdapter):
    """No native ``send_image``: the URL as text IS this platform's image delivery."""

    async def connect(self, *, is_reconnect=False):
        return True

    async def disconnect(self):
        return None

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=True, message_id="text")


class _NativeAdapter(_LinkOnlyAdapter):
    """A native ``send_image`` whose upload was refused, falling back to the base link send."""

    async def send_image(self, chat_id, image_url, caption=None, reply_to=None, metadata=None):
        return await super().send_image(chat_id, image_url, caption, reply_to, metadata=metadata)


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_cls, delivered", [(_NativeAdapter, False), (_LinkOnlyAdapter, True)])
async def test_base_batch_counts_a_link_only_where_it_is_the_image(adapter_cls, delivered):
    adapter = adapter_cls(PlatformConfig(enabled=True, token="t"), Platform.SIGNAL)

    sent = await adapter.send_image("chat", "https://example.com/cat.png", "cat")
    result = await adapter.send_multiple_images("chat", [("https://example.com/cat.png", "cat")])

    assert sent.success is True  # the link itself went out either way
    assert result.success is delivered


@pytest.mark.asyncio
async def test_telegram_linked_photo_is_not_delivered_but_keeps_its_footer(monkeypatch):
    from gateway.runtime_footer import caption_landed, caption_with_footer
    from plugins.platforms.telegram.telegram_album import send_photo_album

    monkeypatch.setitem(sys.modules, "telegram", types.ModuleType("telegram"))  # no media groups
    adapter = types.SimpleNamespace(
        name="telegram", media_caption_limit=1024, _is_animation_url=lambda _url: False,
        send_image=AsyncMock(return_value=SendResult(success=True, message_id="text", link_fallback=True)))
    images, _ = caption_with_footer(adapter, [("https://example.com/cat.png", "cat")], "model · 5%")

    result = await send_photo_album(adapter, "chat", images)

    assert result.success is False
    assert caption_landed(result, images) is True  # the link text carried the footer: no second footer
