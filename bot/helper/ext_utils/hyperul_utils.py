from asyncio import sleep

from PIL import Image
from pyrogram import StopTransmission
from pyrogram.errors import (
    ChannelInvalid,
    FloodWait,
    PeerIdInvalid,
    PhotoInvalidDimensions,
)
from pyrogram.types import ReplyParameters

try:
    from pyrogram.errors import FloodPremiumWait
except ImportError:
    FloodPremiumWait = FloodWait

from os import path as ospath
from aiofiles.os import path as aiopath, remove

from ... import LOGGER
from ...core.config_manager import Config
from ...core.tg_client import TgClient
from ..telegram_helper.tg_transfer import HypertgTransfer
from ..ext_utils.media_utils import (
    get_audio_thumbnail,
    get_document_type,
    get_media_info,
    get_multiple_frames_thumbnail,
    get_video_thumbnail,
)
from ..ext_utils.bot_utils import parse_dest
from ..ext_utils.tmdb_utils import get_auto_thumbnail


class HypertgUpload(HypertgTransfer):
    def __init__(self, obj):
        super().__init__(obj)
        self._up_file = ""
        self._file_progress = {}

    async def _progress(self, current, total, file_path):
        if self._listener.is_cancelled:
            raise StopTransmission()
        self._file_progress[file_path] = current
        self._obj._processed_bytes = sum(self._file_progress.values())

    async def upload(
        self,
        file_path,
        cap_mono,
        reply_target,
        reply_msg_id,
        force_document=False,
        user_thumb=None,
        user_session=False,
    ):
        self._cancel.clear()
        self._up_file = ospath.basename(file_path)

        is_video, is_audio, is_image = await get_document_type(file_path)

        thumb = user_thumb if user_thumb and user_thumb != "none" else None

        if not is_image and thumb is None:
            file_name = ospath.splitext(self._up_file)[0]
            base_path = getattr(self._obj, "_path", "")
            thumb_path = f"{base_path}/yt-dlp-thumb/{file_name}.jpg"
            if await aiopath.isfile(thumb_path):
                thumb = thumb_path
            elif await aiopath.isfile(thumb_path.replace("/yt-dlp-thumb", "")):
                thumb = thumb_path.replace("/yt-dlp-thumb", "")
            elif is_audio and not is_video:
                thumb = await get_audio_thumbnail(file_path)

        if not is_image and thumb is None and user_thumb is None:
            user_dict = self._listener.user_dict
            if user_dict.get("AUTO_THUMBNAIL") or (
                "AUTO_THUMBNAIL" not in user_dict and Config.AUTO_THUMBNAIL
            ):
                try:
                    thumb = await get_auto_thumbnail(
                        self._up_file or self._listener.name,
                        force_document or self._listener.as_doc,
                    )
                except Exception as e:
                    LOGGER.warning(f"Auto thumbnail failed: {e}")

        duration = 0
        width = 480
        height = 320
        artist = ""
        title = ""

        if (
            force_document
            or self._listener.as_doc
            or (not is_video and not is_audio and not is_image)
        ):
            key = "documents"
            if is_video and thumb is None:
                thumb = await get_video_thumbnail(file_path, None)
        elif is_video:
            key = "videos"
            duration = (await get_media_info(file_path))[0]
            if thumb is None and self._listener.thumbnail_layout:
                thumb = await get_multiple_frames_thumbnail(
                    file_path,
                    self._listener.thumbnail_layout,
                    self._listener.screen_shots,
                )
            if thumb is None:
                thumb = await get_video_thumbnail(file_path, duration)
            if thumb is not None and thumb != "none":
                with Image.open(thumb) as img:
                    width, height = img.size
        elif is_audio:
            key = "audios"
            duration, artist, title = await get_media_info(file_path)
        else:
            key = "photos"

        if thumb == "none":
            thumb = None

        custom_thumb_requested = bool(user_thumb and user_thumb != "none")
        up_size = ospath.getsize(file_path)
        hyper_user_only = False
        if up_size > 2097152000 and any(k < 0 for k in self.clients):
            if TgClient.user:
                use_hyper = False
                user_session = True
            else:
                use_hyper = True
                hyper_user_only = True
                user_session = False
        else:
            use_hyper = Config.USE_HYPER and self.clients and up_size > 10 * 1024 * 1024
        if self._listener.up_dest:
            upload_chat_id = self._listener.up_dest
            thread_id = self._listener.chat_thread_id
        elif Config.LEECH_LOG_CHAT:
            upload_chat_id, thread_id = parse_dest(Config.LEECH_LOG_CHAT)
        else:
            upload_chat_id, thread_id = reply_target.chat.id, None
        route = "hyper" if use_hyper else "direct"
        thumb_info = ""
        if thumb and await aiopath.isfile(thumb):
            try:
                thumb_bytes = ospath.getsize(thumb)
                with Image.open(thumb) as thumb_image:
                    thumb_info = (
                        f" thumb_bytes={thumb_bytes}"
                        f" thumb_dimensions={thumb_image.size}"
                        f" thumb_format={thumb_image.format}"
                    )
            except Exception as thumb_error:
                thumb_info = f" thumb_inspect_error={type(thumb_error).__name__}"
        LOGGER.info(
            f"HypertgUL prepare {self._up_file}: media={key} size={up_size} "
            f"route={route} custom_thumb={custom_thumb_requested} "
            f"selected_thumb={bool(thumb)}{thumb_info}"
        )
        try:
            if use_hyper:
                hyper_rply = (
                    reply_msg_id if upload_chat_id == reply_target.chat.id else None
                )
                sent = await self._hyper_send(
                    file_path,
                    key,
                    thumb,
                    cap_mono,
                    upload_chat_id,
                    hyper_rply,
                    thread_id,
                    duration=duration,
                    width=width,
                    height=height,
                    artist=artist,
                    title=title,
                    user_only=hyper_user_only,
                    fallback_user_session=user_session,
                )
            else:
                direct_rply = (
                    reply_msg_id if upload_chat_id == reply_target.chat.id else None
                )
                sent = await self._direct_send(
                    file_path,
                    key,
                    thumb,
                    cap_mono,
                    upload_chat_id,
                    direct_rply,
                    thread_id,
                    duration=duration,
                    width=width,
                    height=height,
                    artist=artist,
                    title=title,
                    user_session=user_session,
                )

            LOGGER.info(f"HypertgUL uploaded {self._up_file}")
            if sent:
                media_attr = {
                    "videos": "video",
                    "audios": "audio",
                    "photos": "photo",
                    "documents": "document",
                }[key]
                media = getattr(sent, media_attr, None)
                thumbs = getattr(media, "thumbs", None) if media else None
                LOGGER.info(
                    f"HypertgUL thumbnail result {self._up_file}: "
                    f"custom_requested={custom_thumb_requested} "
                    f"selected={bool(thumb)} returned={len(thumbs or [])}"
                )
            return sent

        except StopTransmission:
            LOGGER.warning(f"HypertgUL cancelled {self._up_file}")
            raise
        except Exception as e:
            LOGGER.error(f"HypertgUL fail {self._up_file}: {type(e).__name__}: {e}")
            raise
        finally:
            if user_thumb is None and thumb is not None and await aiopath.exists(thumb):
                try:
                    await remove(thumb)
                except Exception:
                    pass

    async def _send_with_retry(self, send_func, **kwargs):
        while True:
            try:
                return await send_func(**kwargs)
            except (FloodWait, FloodPremiumWait) as f:
                LOGGER.warning(f"HypertgUL flood {f.value}s on {self._up_file}")
                await sleep(f.value + 1)

    async def _try_send(self, key, client, kwargs):
        if key == "videos":
            send = client.send_video
        elif key == "audios":
            send = client.send_audio
        elif key == "photos":
            send = client.send_photo
        else:
            send = client.send_document

        try:
            return await self._send_with_retry(send, **kwargs)
        except PhotoInvalidDimensions:
            if kwargs.get("thumb") is None:
                raise

            LOGGER.warning(
                f"HypertgUL rejected thumbnail for {self._up_file}; "
                "retrying without thumb"
            )
            kwargs.pop("thumb", None)
            return await self._send_with_retry(send, **kwargs)

    async def _hyper_send(
        self,
        file_path,
        key,
        thumb,
        cap_mono,
        chat_id,
        reply_msg_id,
        thread_id=None,
        duration=0,
        width=0,
        height=0,
        artist="",
        title="",
        user_only=False,
        fallback_user_session=False,
    ):
        if user_only:
            candidates = {
                k: self.work_loads.get(k, 0) for k in self.clients if k < 0
            }
            if not candidates:
                idx = self._pick_client()
            else:
                idx = min(candidates, key=candidates.get)
        else:
            idx = self._pick_client()
        client = self.clients[idx]
        self.work_loads[idx] += 1

        try:
            kwargs = {
                "chat_id": chat_id,
                "disable_notification": True,
                "progress": self._progress,
                "progress_args": (file_path,),
            }
            if cap_mono:
                kwargs["caption"] = cap_mono
            if reply_msg_id:
                kwargs["reply_parameters"] = ReplyParameters(message_id=reply_msg_id)
            elif thread_id:
                kwargs["message_thread_id"] = thread_id

            if key == "videos":
                if duration:
                    kwargs["duration"] = duration
                if width:
                    kwargs["width"] = width
                if height:
                    kwargs["height"] = height
                if thumb:
                    # `thumb` is the upload thumbnail shown by Telegram.
                    # `video_cover` triggers a separate UploadMedia call and can
                    # fail peer resolution on Hyper clients, silently losing
                    # both custom and generated thumbnails in the old fallback.
                    kwargs["thumb"] = thumb
            elif key == "audios":
                if duration:
                    kwargs["duration"] = duration
                if artist:
                    kwargs["performer"] = artist
                if title:
                    kwargs["title"] = title
                if thumb:
                    kwargs["thumb"] = thumb
            elif key == "documents" and thumb:
                # Hyper uploads handle documents too; without this, their
                # custom thumbnail was silently omitted on the <2 GB route.
                kwargs["thumb"] = thumb

            if key == "videos":
                kwargs["video"] = file_path
            elif key == "audios":
                kwargs["audio"] = file_path
            elif key == "photos":
                kwargs["photo"] = file_path
            else:
                kwargs["document"] = file_path

            try:
                return await self._try_send(key, client, kwargs)
            except (ChannelInvalid, PeerIdInvalid) as err:
                client_id = self._client_idx(client)
                LOGGER.warning(
                    "Hyper helper client %s cannot resolve Telegram destination %s "
                    "for %s (%s); retrying through the primary leech client",
                    client_id,
                    chat_id,
                    self._up_file,
                    type(err).__name__,
                )
                return await self._direct_send(
                    file_path,
                    key,
                    thumb,
                    cap_mono,
                    chat_id,
                    reply_msg_id,
                    thread_id,
                    duration=duration,
                    width=width,
                    height=height,
                    artist=artist,
                    title=title,
                    user_session=fallback_user_session,
                )
        finally:
            self.work_loads[idx] -= 1

    async def _direct_send(
        self,
        file_path,
        key,
        thumb,
        cap_mono,
        chat_id,
        reply_msg_id,
        thread_id=None,
        duration=0,
        width=0,
        height=0,
        artist="",
        title="",
        user_session=False,
    ):
        client = (
            TgClient.user if user_session and TgClient.user else self._listener.client
        )
        kwargs = {
            "chat_id": chat_id,
            "disable_notification": True,
            "progress": self._progress,
            "progress_args": (file_path,),
        }
        if cap_mono:
            kwargs["caption"] = cap_mono
        if reply_msg_id:
            kwargs["reply_parameters"] = ReplyParameters(message_id=reply_msg_id)
        elif thread_id:
            kwargs["message_thread_id"] = thread_id

        if key == "videos":
            if thumb:
                kwargs["thumb"] = thumb
            if duration:
                kwargs["duration"] = duration
            if width:
                kwargs["width"] = width
            if height:
                kwargs["height"] = height
        elif key == "audios":
            if thumb:
                kwargs["thumb"] = thumb
            if duration:
                kwargs["duration"] = duration
            if artist:
                kwargs["performer"] = artist
            if title:
                kwargs["title"] = title
        else:
            if thumb:
                kwargs["thumb"] = thumb

        if key == "videos":
            kwargs["video"] = file_path
        elif key == "audios":
            kwargs["audio"] = file_path
        elif key == "photos":
            kwargs["photo"] = file_path
        else:
            kwargs["document"] = file_path

        return await self._try_send(key, client, kwargs)

    async def cancel(self):
        await super().cancel()
