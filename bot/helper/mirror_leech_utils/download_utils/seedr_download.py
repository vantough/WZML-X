from asyncio import sleep
from contextlib import suppress
from time import monotonic
from urllib.parse import urlsplit
from secrets import token_hex

import aiohttp
from aioqbt.api import AddFormBuilder
from .... import LOGGER, task_dict, task_dict_lock, user_data
from ....core.config_manager import Config
from ....core.seedr_api import SeedrApiClient, SeedrApiError
from ....core.seedr_client import SeedrClient
from ....core.torrent_manager import TorrentManager
from ...ext_utils.torrent_metadata import torrent_total_size
from ...ext_utils.task_manager import (
    check_running_tasks,
    stop_duplicate_check,
    limit_checker,
)
from ...ext_utils.links_utils import is_magnet, is_url
from ...listeners.direct_listener import DirectListener
from ...mirror_leech_utils.status_utils.direct_status import DirectStatus
from ...mirror_leech_utils.status_utils.queue_status import QueueStatus
from ...mirror_leech_utils.status_utils.seedr_status import SeedrStatus
from ...telegram_helper.message_utils import send_status_message


MAX_TORRENT_FILE_SIZE = 12 * 1024 * 1024
MAGNET_METADATA_TIMEOUT = 45


def should_delete_seedr_after_upload(delete_enabled, upload_succeeded):
    """Delete the Seedr source only after the destination confirms success."""
    return bool(delete_enabled and upload_succeeded)


def _find_numeric(payload, names):
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key).lower() in names and isinstance(value, (int, float)):
                return int(value)
        for value in payload.values():
            found = _find_numeric(value, names)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _find_numeric(value, names)
            if found is not None:
                return found
    return None


def _quota_space(quota):
    storage = quota
    if isinstance(quota, dict):
        for key in ("storage", "storage_quota", "disk", "space"):
            if isinstance(quota.get(key), dict):
                storage = quota[key]
                break
    available = _find_numeric(
        storage, {"available", "available_bytes", "free", "free_bytes", "space_free"}
    )
    if available is not None and available >= 0:
        return available, 0
    total = _find_numeric(
        storage,
        {
            "storage_total",
            "total_storage",
            "storage_limit",
            "total_bytes",
            "limit_bytes",
            "space_max",
        },
    )
    used = _find_numeric(
        storage, {"storage_used", "used_storage", "used_bytes", "space_used"}
    )
    if total is None:
        total = _find_numeric(storage, {"total", "limit", "max"})
    if used is None:
        used = _find_numeric(storage, {"used"})
    if total is None or used is None or total < 0 or used < 0:
        raise ValueError("Seedr quota response did not contain storage usage")
    return total, used


def _task_payload(payload):
    if isinstance(payload, dict):
        nested = payload.get("task")
        if isinstance(nested, dict):
            return nested
    return payload if isinstance(payload, dict) else {}


def _task_id(payload):
    """Accept the task ID key variants returned by Seedr API deployments."""
    if not isinstance(payload, dict):
        return None
    for key in ("id", "task_id", "torrent_id", "user_torrent_id"):
        value = payload.get(key)
        if value is not None and not isinstance(value, (dict, list)) and str(value).strip():
            return str(value).strip()
    for key in ("task", "data", "result"):
        nested = _task_id(payload.get(key))
        if nested is not None:
            return nested
    return None


def _task_progress(payload):
    task = _task_payload(payload)
    raw = task.get("progress", task.get("percentage", 0))
    try:
        progress = float(raw or 0)
    except (TypeError, ValueError):
        progress = 0.0
    if 0 < progress <= 1:
        progress *= 100
    return task, max(0.0, min(progress, 100.0))


def _task_finished(task, progress):
    state = str(task.get("status") or task.get("state") or "").lower()
    if state in {"failed", "error", "cancelled", "canceled", "removed"}:
        raise ValueError(task.get("error") or f"Seedr task ended with status {state}")
    return state in {"complete", "completed", "finished", "done"} or progress >= 100


def _content_files(payload):
    if isinstance(payload, list):
        for item in payload:
            yield from _content_files(item)
    elif isinstance(payload, dict):
        collection_keys = {
            "files",
            "folders",
            "items",
            "contents",
            "torrents",
            "children",
            "entries",
            "data",
            "result",
        }
        is_container = any(key in payload for key in collection_keys)
        kind = str(payload.get("type") or payload.get("kind") or "").lower()
        has_file_id = any(
            key in payload for key in ("file_id", "folder_file_id")
        ) or (
            "id" in payload
            and not is_container
            and kind not in {"torrent", "task", "folder", "directory", "collection"}
        )
        if has_file_id and any(key in payload for key in ("name", "filename")):
            yield payload
        for value in payload.values():
            if isinstance(value, (dict, list)):
                yield from _content_files(value)


async def _read_torrent_url(url):
    timeout = aiohttp.ClientTimeout(total=20, connect=8)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, allow_redirects=True) as response:
            response.raise_for_status()
            if response.content_length and response.content_length > MAX_TORRENT_FILE_SIZE:
                raise ValueError("Torrent file is too large to inspect")
            data = await response.content.read(MAX_TORRENT_FILE_SIZE + 1)
            if len(data) > MAX_TORRENT_FILE_SIZE:
                raise ValueError("Torrent file is too large to inspect")
            return data


async def _probe_magnet_size(listener, path):
    client = TorrentManager.qbittorrent
    tag = f"seedr-meta-{listener.mid}-{token_hex(4)}"
    probe_path = f"{path}/.seedr-metadata"
    torrent_hash = None
    form = (
        AddFormBuilder.with_client(client)
        .include_url(listener.link)
        .savepath(probe_path)
        .tags([tag])
    )
    try:
        await client.torrents.add(form.build())
        deadline = monotonic() + MAGNET_METADATA_TIMEOUT
        while monotonic() < deadline and not listener.is_cancelled:
            torrents = await client.torrents.info(tag=tag)
            if torrents:
                torrent = torrents[0]
                torrent_hash = getattr(torrent, "hash", None)
                size = int(
                    getattr(torrent, "size", 0)
                    or getattr(torrent, "total_size", 0)
                    or 0
                )
                name = getattr(torrent, "name", "")
                if size > 0:
                    return size, name
            await sleep(2)
        if listener.is_cancelled:
            return None, ""
        raise TimeoutError("Could not resolve magnet metadata in time")
    finally:
        if not torrent_hash:
            with suppress(Exception):
                torrents = await client.torrents.info(tag=tag)
                if torrents:
                    torrent_hash = getattr(torrents[0], "hash", None)
        if torrent_hash:
            with suppress(Exception):
                await client.torrents.stop([torrent_hash])
            with suppress(Exception):
                await client.torrents.delete([torrent_hash], True)
        with suppress(Exception):
            await client.torrents.delete_tags([tag])


async def _resolve_seedr_input_size(listener, path):
    if is_magnet(listener.link):
        return await _probe_magnet_size(listener, path)
    if not isinstance(listener.link, str) or not is_url(listener.link):
        return None, ""
    if not urlsplit(listener.link).path.lower().endswith(".torrent"):
        return None, ""
    data = await _read_torrent_url(listener.link)
    from asyncio import to_thread

    return await to_thread(torrent_total_size, data), ""


async def _seedr_api_files(client, task_id):
    payload = await client.get_task_contents(task_id)
    files = list(_content_files(payload))
    if not files:
        if isinstance(payload, dict):
            shape = {
                key: (len(value) if isinstance(value, (list, dict)) else type(value).__name__)
                for key, value in payload.items()
            }
            LOGGER.warning(
                f"Seedr task {task_id} contents had no file records (shape={shape})"
            )
        else:
            LOGGER.warning(
                f"Seedr task {task_id} contents had no file records "
                f"(payload_type={type(payload).__name__})"
            )
    contents = []
    total_size = 0
    for file_info in files:
        file_ids = []
        for key in ("file_id", "folder_file_id", "id"):
            value = file_info.get(key)
            if value is not None and str(value) not in {str(item) for item in file_ids}:
                file_ids.append(value)

        url = (
            file_info.get("url")
            or file_info.get("download_url")
            or file_info.get("downloadUrl")
        )
        last_error = None
        if not url:
            for index, file_id in enumerate(file_ids):
                attempts = 1 if index < len(file_ids) - 1 else 3
                for attempt in range(attempts):
                    try:
                        url_payload = await client.get_file_url(file_id)
                        url = (
                            url_payload.get("url")
                            or url_payload.get("download_url")
                            or url_payload.get("downloadUrl")
                            if isinstance(url_payload, dict)
                            else None
                        )
                        if url:
                            break
                        last_error = ValueError("Seedr did not return a file download URL")
                    except SeedrApiError as exc:
                        last_error = exc
                        if exc.status_code != 404 or attempt + 1 >= attempts:
                            break
                        await sleep(2)
                if url:
                    break

        if not url:
            LOGGER.warning(
                f"Seedr file URL lookup failed for task {task_id} "
                f"(content_keys={sorted(file_info.keys())}, id_fields={len(file_ids)})"
            )
            if last_error:
                raise last_error
            raise ValueError("Seedr task contents did not contain a file ID or URL")

        name = file_info.get("name") or file_info.get("filename") or str(file_ids[-1] if file_ids else "file")
        size = int(file_info.get("size") or file_info.get("length") or 0)
        total_size += size
        contents.append(
            {
                "url": url,
                "filename": name,
                "path": str(file_info.get("path") or "").strip("/"),
                "size": size,
            }
        )
    return contents, total_size, files


async def _cleanup_seedr_attempt(client, task_id):
    if task_id is None:
        return
    try:
        payload = await client.get_task_contents(task_id)
        for file_info in _content_files(payload):
            file_id = file_info.get("file_id") or file_info.get("id")
            if file_id is not None:
                with suppress(Exception):
                    await client.delete_file(file_id)
    except Exception:
        pass
    with suppress(Exception):
        await client.delete_task(task_id)


async def add_seedr_first_qb(listener, path, ratio, seed_time, qb_fallback):
    """Try the documented Seedr API for up to the configured deadline, then qB."""
    token = Config.SEEDR_API_TOKEN
    if (
        Config.DISABLE_SEEDR
        or not Config.SEEDR_QBMIRROR_ENABLED
        or not token
        or not isinstance(listener.link, str)
        or not (
            is_magnet(listener.link)
            or (
                is_url(listener.link)
                and urlsplit(listener.link).path.lower().endswith(".torrent")
            )
        )
    ):
        await qb_fallback(listener, path, ratio, seed_time)
        return

    client = SeedrApiClient(token)
    task_id = None
    status = None
    fallback_reason = ""
    handoff_started = False
    try:
        size, metadata_name = await _resolve_seedr_input_size(listener, path)
        if listener.is_cancelled:
            return
        if not size:
            raise ValueError("Torrent size could not be resolved")
        if not listener.name:
            listener.name = metadata_name

        total, used = _quota_space(await client.get_quota())
        free = max(0, total - used)
        if size > free:
            fallback_reason = "Seedr does not have enough free space"
            raise ValueError(fallback_reason)

        duplicate, button = await stop_duplicate_check(listener)
        if duplicate:
            await listener.on_download_error(duplicate, button)
            return
        if limit_exceeded := await limit_checker(listener):
            await listener.on_download_error(limit_exceeded, is_limit=True)
            return

        result = await client.add_task(listener.link)
        task_id = _task_id(result)
        if task_id is None:
            keys = sorted(result.keys()) if isinstance(result, dict) else type(result).__name__
            LOGGER.warning(
                f"Seedr POST /tasks returned an unrecognized success payload (shape={keys})"
            )
            raise SeedrApiError("Seedr did not return a task ID")
        status = SeedrStatus(listener, task_id, client)
        status._info.update({"name": listener.name, "size": size, "progress": 0})
        async with task_dict_lock:
            task_dict[listener.mid] = status
        if listener.multi <= 1 and not listener.is_rss:
            await send_status_message(listener.message)

        deadline = monotonic() + max(1, int(Config.SEEDR_FIRST_TIMEOUT))
        while not listener.is_cancelled and monotonic() < deadline:
            task = _task_payload(await client.get_task(task_id))
            task, progress = _task_progress(task)
            status._info.update(
                {
                    "name": task.get("name") or listener.name,
                    "size": int(task.get("size") or size),
                    "progress": progress,
                    "speed": float(task.get("download_rate") or task.get("speed") or 0),
                    "stopped": int(bool(task.get("paused"))),
                }
            )
            if _task_finished(task, progress):
                break
            await sleep(max(1, int(Config.SEEDR_POLL_INTERVAL)))
        if listener.is_cancelled:
            return
        if monotonic() >= deadline:
            fallback_reason = "Seedr did not finish within the configured time"
            raise TimeoutError(fallback_reason)

        contents, total_size, _ = await _seedr_api_files(client, task_id)
        if not contents:
            raise ValueError("Seedr task completed without any downloadable files")
        if total_size:
            listener.size = total_size
        elif not listener.size:
            listener.size = size
        if not listener.name:
            listener.name = status._info.get("name", "")

        add_to_queue, event = await check_running_tasks(listener)
        if add_to_queue:
            async with task_dict_lock:
                task_dict[listener.mid] = QueueStatus(listener, str(task_id), "dl")
            if listener.multi <= 1 and not listener.is_rss:
                await send_status_message(listener.message)
            await event.wait()
            if listener.is_cancelled:
                return

        await listener.on_download_start()
        output_path = f"{path}/{listener.name}"
        direct_listener = DirectListener(
            output_path, listener, {"follow-torrent": "false", "follow-metalink": "false"}
        )
        async with task_dict_lock:
            task_dict[listener.mid] = DirectStatus(
                listener, direct_listener, token_hex(5)
            )
        listener.upload_succeeded = False
        handoff_started = True
        await direct_listener.download(contents)
        if should_delete_seedr_after_upload(
            Config.SEEDR_DELETE_FOLDER, listener.upload_succeeded
        ):
            await _cleanup_seedr_attempt(client, task_id)
        elif not listener.upload_succeeded:
            LOGGER.warning(
                f"Retaining Seedr task {task_id}: destination upload did not complete successfully"
            )
        return
    except Exception as exc:
        if task_id is not None and not handoff_started:
            await _cleanup_seedr_attempt(client, task_id)
        if handoff_started:
            LOGGER.error(
                f"Seedr transfer for task {listener.mid} did not finish; retaining the Seedr source: {exc}"
            )
            return
        if listener.is_cancelled:
            return
        fallback_reason = fallback_reason or str(exc)
        LOGGER.warning(f"Seedr-first attempt for task {listener.mid} failed: {fallback_reason}")
        async with task_dict_lock:
            if task_dict.get(listener.mid) is status:
                task_dict.pop(listener.mid, None)
        if fallback_reason:
            with suppress(Exception):
                await listener.message.reply_text(
                    "Seedr could not complete this torrent. Continuing with qBittorrent."
                )
        await qb_fallback(listener, path, ratio, seed_time)


def _match_folder(folders, names, known_ids, torrent_gone):
    new_folders = [f for f in folders if f.get("id") not in known_ids]
    for folder in new_folders:
        if folder.get("name") in names:
            return folder
    for folder in folders:
        if folder.get("name") in names:
            return folder
    if torrent_gone and len(new_folders) == 1:
        return new_folders[0]
    return None


async def _build_contents(seedr_client, torrent_download_dir):
    contents = []
    total_size = 0

    async def walk(folder_id, prefix=""):
        nonlocal total_size
        result = await seedr_client.list_contents(folder_id)
        for folder in result.get("folders", []):
            await walk(folder["id"], f"{prefix}/{folder['name']}")
        for file_ in result.get("files", []):
            url = await seedr_client.fetch_file(file_["folder_file_id"])
            file_size = int(file_.get("size", 0) or 0)
            total_size += file_size
            contents.append(
                {
                    "url": url,
                    "filename": file_["name"],
                    "path": prefix.strip("/"),
                    "size": file_size,
                }
            )

    await walk(torrent_download_dir)
    return contents, total_size


async def _delete_seedr_folder(seedr_client, torrent_download_dir):
    if not torrent_download_dir:
        return
    try:
        await seedr_client.delete("folder", torrent_download_dir)
        LOGGER.info(f"Deleted Seedr folder: {torrent_download_dir}")
    except Exception as e:
        LOGGER.error(f"Failed to delete Seedr folder {torrent_download_dir}: {e}")


async def add_seedr_download(listener, path):
    if not isinstance(listener.link, str) or not (
        is_magnet(listener.link) or is_url(listener.link)
    ):
        await listener.on_download_error(
            "Seedr only accepts magnet links or .torrent URLs!"
        )
        return
    torrent_id = None
    torrent_download_dir = None
    gid = token_hex(5)
    user_dict = user_data.get(listener.user_id, {})
    email = user_dict.get("SEEDR_EMAIL") or Config.SEEDR_EMAIL
    password = user_dict.get("SEEDR_PASSWORD") or Config.SEEDR_PASSWORD
    delete_folder = user_dict.get("SEEDR_DELETE_FOLDER", Config.SEEDR_DELETE_FOLDER)
    seedr_client = SeedrClient(email, password)
    try:
        await seedr_client.login()
        log_link = (
            f"{listener.link[:60]}..." if is_magnet(listener.link) else listener.link
        )
        LOGGER.info(f"Adding Seedr Torrent: {log_link}")
        result = await seedr_client.add_torrent(listener.link)
        torrent_id = result.get("torrent_id") or result.get("user_torrent_id")
        if not torrent_id:
            raise ValueError("Failed to obtain Seedr torrent ID!")
        title = result.get("title") or ""
        LOGGER.info(f"Seedr Torrent Added: {torrent_id}")

        status = SeedrStatus(listener, torrent_id, seedr_client)
        async with task_dict_lock:
            task_dict[listener.mid] = status

        await listener.on_download_start()
        if listener.multi <= 1 and not listener.is_rss:
            await send_status_message(listener.message)

        known_folders = {
            f.get("id")
            for f in (await seedr_client.list_contents("0")).get("folders", [])
        }
        folder_names = {title} if title else set()
        torrent_download_dir = None
        not_found_count = 0
        while not listener.is_cancelled:
            await sleep(5)
            result = await seedr_client.list_contents("0")

            torrent = next(
                (
                    t
                    for t in result.get("torrents", [])
                    if t.get("id") == torrent_id
                    or t.get("user_torrent_id") == torrent_id
                ),
                None,
            )
            if torrent is not None:
                not_found_count = 0
                status._info.update(
                    {
                        "name": torrent.get("name", listener.name),
                        "size": torrent.get("size", 0) or 0,
                        "progress": float(torrent.get("progress", 0) or 0),
                        "speed": float(torrent.get("download_rate", 0) or 0),
                        "stopped": int(torrent.get("stopped", 0) or 0),
                    }
                )
                if torrent.get("name"):
                    folder_names.add(torrent["name"])
                warn = str(torrent.get("warnings") or "").strip()
                if warn and warn not in ("[]", "{}"):
                    LOGGER.warning(f"Seedr torrent {torrent_id} warning: {warn}")

            folder = _match_folder(
                result.get("folders", []),
                folder_names,
                known_folders,
                torrent is None,
            )

            if folder is not None:
                folder_contents = await seedr_client.list_contents(folder["id"])
                if folder_contents.get("files") or folder_contents.get("folders"):
                    torrent_download_dir = folder["id"]
                    status._info.update(
                        {
                            "name": title or listener.name,
                            "size": folder.get("size", 0) or 0,
                            "progress": 100.0,
                        }
                    )
                    break
            if torrent is None:
                not_found_count += 1
                if not_found_count >= 36:
                    raise ValueError("Seedr torrent not found in the account!")
                if not_found_count == 1:
                    LOGGER.info(
                        f"Seedr torrent {torrent_id} not visible yet, keeping an eye on it"
                    )

        if not torrent_download_dir or listener.is_cancelled:
            await _delete_seedr_folder(seedr_client, torrent_download_dir)
            return

        contents, total_size = await _build_contents(seedr_client, torrent_download_dir)
        if not contents:
            raise ValueError("Seedr torrent has no files to download!")

        if total_size > 0:
            listener.size = total_size
        else:
            listener.size = status._info.get("size", 0) or 0
        if not listener.name:
            listener.name = status._info.get("name", "")

        msg, button = await stop_duplicate_check(listener)
        if msg:
            await _delete_seedr_folder(seedr_client, torrent_download_dir)
            await listener.on_download_error(msg, button)
            return

        if limit_exceeded := await limit_checker(listener):
            await _delete_seedr_folder(seedr_client, torrent_download_dir)
            await listener.on_download_error(limit_exceeded, is_limit=True)
            return

        add_to_queue, event = await check_running_tasks(listener)
        if add_to_queue:
            LOGGER.info(f"Added to Queue/Download: {listener.name}")
            async with task_dict_lock:
                task_dict[listener.mid] = QueueStatus(listener, gid, "dl")
            if listener.multi <= 1 and not listener.is_rss:
                await send_status_message(listener.message)
            await event.wait()
            if listener.is_cancelled:
                await _delete_seedr_folder(seedr_client, torrent_download_dir)
                return

        path = f"{path}/{listener.name}"
        a2c_opt = {"follow-torrent": "false", "follow-metalink": "false"}
        directListener = DirectListener(path, listener, a2c_opt)

        async with task_dict_lock:
            task_dict[listener.mid] = DirectStatus(listener, directListener, gid)

        if add_to_queue:
            LOGGER.info(f"Start Queued Download from Seedr: {listener.name}")
        else:
            LOGGER.info(f"Download from Seedr: {listener.name}")

        await directListener.download(contents)

        if delete_folder or listener.is_cancelled:
            await _delete_seedr_folder(seedr_client, torrent_download_dir)
    except Exception as e:
        if torrent_id:
            try:
                await seedr_client.delete("torrent", torrent_id)
            except Exception:
                pass
        await _delete_seedr_folder(seedr_client, torrent_download_dir)
        await listener.on_download_error(f"{e}".strip())
