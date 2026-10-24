from asyncio import TimeoutError as AsyncTimeout
from copy import copy
from html import escape
from time import time
from uuid import uuid4

from aiohttp import ClientSession, ClientTimeout
from bot import LOGGER
from bot.core.config_manager import Config
from bot.helper.ext_utils.bot_utils import new_task
from bot.helper.ext_utils.status_utils import get_readable_file_size
from bot.helper.telegram_helper.button_build import ButtonMaker
from bot.helper.telegram_helper.message_utils import edit_message, send_message
from plugins.nzb_search.url_utils import (
    authenticated_download_url,
    nzb_mirror_command,
)

RESULT_PAGE_SIZE = 8
RESULT_CACHE_TTL = 1800
RESULT_CACHE_MAX = 128
_RESULT_CACHE = {}


def _prune_cache():
    now = time()
    for token in [k for k, v in _RESULT_CACHE.items() if now - v["created"] > RESULT_CACHE_TTL]:
        _RESULT_CACHE.pop(token, None)
    while len(_RESULT_CACHE) > RESULT_CACHE_MAX:
        oldest = min(_RESULT_CACHE, key=lambda k: _RESULT_CACHE[k]["created"])
        _RESULT_CACHE.pop(oldest, None)


def _authenticated_download_url(url):
    return authenticated_download_url(
        url, Config.PROWLARR_URL, Config.PROWLARR_API_KEY
    )

def _buttons(owner, token, entry, page=0):
    maker = ButtonMaker()
    start = page * RESULT_PAGE_SIZE
    stop = min(start + RESULT_PAGE_SIZE, len(entry["results"]))
    for index in range(start, stop):
        maker.data_button(str(index + 1), f"nzbsearch {owner} {token} {index}")
    if page:
        maker.data_button("Prev", f"nzbsearch {owner} {token} p{page - 1}")
    if stop < len(entry["results"]):
        maker.data_button("Next", f"nzbsearch {owner} {token} p{page + 1}")
    return maker.build_menu(4)


def _page_text(query, entry, page=0):
    start = page * RESULT_PAGE_SIZE
    stop = min(start + RESULT_PAGE_SIZE, len(entry["results"]))
    pages = (len(entry["results"]) + RESULT_PAGE_SIZE - 1) // RESULT_PAGE_SIZE
    lines = [
        f"<b>Usenet results for {escape(query)}</b>",
        f"<i>Page {page + 1} of {pages}. Tap a numbered button to send that NZB to the Google Drive task queue.</i>",
        "",
    ]
    for i in range(start, stop):
        result = entry["results"][i]
        title = escape(result["title"][:220])
        size = escape(get_readable_file_size(result["size"]))
        indexer = escape(result.get("indexer", "Unknown")[:80])
        lines.append(f"<b>{i + 1}.</b> {title}")
        lines.append(f"   <b>Size:</b> {size}  <b>Indexer:</b> {indexer}")
    return "\n".join(lines)


async def search_prowlarr(query, limit=50):
    base = (Config.PROWLARR_URL or "http://prowlarr:9696").rstrip("/")
    key = (Config.PROWLARR_API_KEY or "").strip()
    if not key:
        return None, "Prowlarr API key is not configured. Set PROWLARR_API_KEY in /bsetting."
    headers = {"X-Api-Key": key, "Accept": "application/json"}
    params = {"query": query, "type": "search", "limit": limit}
    try:
        async with ClientSession(timeout=ClientTimeout(total=45)) as session:
            async with session.get(f"{base}/api/v1/search", params=params, headers=headers) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:200]
                    LOGGER.warning(f"Prowlarr search HTTP {resp.status}: {body}")
                    return None, f"Prowlarr returned HTTP {resp.status}. Check its indexers and API settings."
                data = await resp.json(content_type=None)
        if not isinstance(data, list):
            return None, "Prowlarr returned an unexpected response."
        results = []
        for item in data:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            url = str(item.get("downloadUrl") or item.get("downloadURL") or "").strip()
            protocol = str(item.get("protocol") or "").lower()
            if protocol and protocol != "usenet":
                continue
            if not title or not url:
                continue
            try:
                size = max(0, int(item.get("size") or 0))
            except (ValueError, TypeError):
                size = 0
            results.append({
                "title": title,
                "size": size,
                "indexer": str(item.get("indexer") or item.get("indexerName") or "Unknown"),
                "url": _authenticated_download_url(url),
            })
            if len(results) >= limit:
                break
        return results, None
    except AsyncTimeout:
        return None, "Prowlarr search timed out."
    except Exception as error:
        LOGGER.exception(f"Prowlarr search failed: {error}")
        return None, "Could not reach Prowlarr. Check the configured URL and service status."


@new_task
async def nzbsearch(_, message):
    if Config.DISABLE_NZB:
        await send_message(message, "SABnzbd/Usenet downloads are disabled by the Bot Owner.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await send_message(message, "Use <code>/nzbsearch &lt;query&gt;</code> (alias: <code>/nzs</code>).")
        return
    query = parts[1].strip()
    status = await send_message(message, f"Searching Prowlarr for <b>{escape(query)}</b>…")
    results, error = await search_prowlarr(query)
    if error:
        await edit_message(status, f"<b>NZB search failed:</b> {escape(error)}")
        return
    if not results:
        await edit_message(status, f"No NZB results found for <i>{escape(query)}</i>.")
        return
    owner = message.from_user.id
    token = uuid4().hex[:10]
    _prune_cache()
    _RESULT_CACHE[token] = {
        "created": time(), "owner": owner, "chat_id": status.chat.id,
        "message_id": status.id, "query": query, "results": results, "used": set(),
    }
    _prune_cache()
    LOGGER.info(f"Prowlarr returned {len(results)} NZB results for user {owner}")
    await edit_message(status, _page_text(query, _RESULT_CACHE[token]), _buttons(owner, token, _RESULT_CACHE[token]))


async def nzbsearch_result_update(client, query):
    _prune_cache()
    parts = query.data.split()
    if len(parts) != 4:
        await query.answer("This result button is invalid.", show_alert=True)
        return
    _, owner_raw, token, selection = parts
    entry = _RESULT_CACHE.get(token)
    try:
        owner = int(owner_raw)
    except ValueError:
        await query.answer("This result button is invalid.", show_alert=True)
        return
    if not entry or time() - entry["created"] > RESULT_CACHE_TTL:
        await query.answer("These NZB results expired. Run /nzs again.", show_alert=True)
        return
    if query.from_user.id != owner or entry["owner"] != owner:
        await query.answer("These search buttons belong to another user.", show_alert=True)
        return
    if query.message.chat.id != entry["chat_id"] or query.message.id != entry["message_id"]:
        await query.answer("These NZB results are no longer active.", show_alert=True)
        return
    if selection.startswith("p"):
        try:
            page = int(selection[1:])
        except ValueError:
            page = -1
        if page < 0 or page * RESULT_PAGE_SIZE >= len(entry["results"]):
            await query.answer("That results page is unavailable.", show_alert=True)
            return
        await query.answer()
        await edit_message(query.message, _page_text(entry["query"], entry, page), _buttons(owner, token, entry, page))
        return
    try:
        index = int(selection)
        result = entry["results"][index]
    except (ValueError, IndexError):
        await query.answer("That NZB result is unavailable.", show_alert=True)
        return
    if index in entry["used"]:
        await query.answer("This result was already queued.", show_alert=True)
        return
    entry["used"].add(index)
    try:
        from bot.modules.mirror_leech import nzb_mirror

        mirror_message = copy(query.message)
        mirror_message._client = client
        mirror_message._task_id = uuid4().int
        mirror_message.text = nzb_mirror_command(result["url"], name=result["title"])
        mirror_message.from_user = query.from_user
        await query.answer(f"Adding result {index + 1} to the Google Drive NZB queue.")
        await nzb_mirror(client, mirror_message)
        LOGGER.info(f"NZB result {index + 1} dispatched to nzbmirror by user {owner}: {result['title']}")
    except Exception as error:
        entry["used"].discard(index)
        LOGGER.exception(f"NZB result dispatch failed: {error}")
        await query.answer("Could not start nzbmirror for this result.", show_alert=True)
