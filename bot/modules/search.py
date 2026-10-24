from niquests import AsyncSession
from copy import copy
from html import escape, unescape
from re import search as re_search
from time import time
from urllib.parse import quote, urlencode
from uuid import uuid4
from xml.etree import ElementTree
from pyrogram.enums import ButtonStyle

from .. import LOGGER
from ..core.config_manager import Config
from ..core.torrent_manager import TorrentManager
from ..helper.ext_utils.bot_utils import new_task
from ..helper.ext_utils.status_utils import get_readable_file_size
from ..helper.ext_utils.telegraph_helper import telegraph
from ..helper.telegram_helper.button_build import ButtonMaker
from ..helper.telegram_helper.message_utils import edit_message, send_message

PLUGINS = []
SITES = None
TELEGRAPH_LIMIT = 300
BT4GPRX_SOURCE = "bt4gprx"
BT4GPRX_URL = "https://bt4gprx.com/search"
RESULT_BUTTON_PAGE_SIZE = 10
RESULT_CACHE_TTL = 1800
RESULT_CACHE_MAX = 256
_RESULT_CACHE = {}


async def initiate_search_tools():
    if Config.DISABLE_TORRENTS or Config.DISABLE_SEARCH:
        LOGGER.warning("Torrents are disabled. Skipping search plugin initialization.")
        return
    PLUGINS.clear()
    PLUGINS.append(BT4GPRX_SOURCE)
    qb_plugins = await TorrentManager.qbittorrent.search.plugins()
    if qb_plugins:
        names = [plugin.name for plugin in qb_plugins]
        await TorrentManager.qbittorrent.search.uninstall_plugin(names)
    if Config.SEARCH_PLUGINS:
        await TorrentManager.qbittorrent.search.install_plugin(Config.SEARCH_PLUGINS)
        qb_plugins = await TorrentManager.qbittorrent.search.plugins()
        PLUGINS.extend(
            plugin.name for plugin in qb_plugins
            if plugin.name.lower() != BT4GPRX_SOURCE
        )

    if Config.SEARCH_API_LINK:
        global SITES
        try:
            async with AsyncSession() as client:
                response = await client.get(f"{Config.SEARCH_API_LINK}/api/v1/sites")
                data = response.json()
            SITES = {
                str(site): str(site).capitalize() for site in data["supported_sites"]
            }
            SITES["all"] = "All"
        except Exception as e:
            LOGGER.error(
                f"{e} Can't fetching sites from SEARCH_API_LINK make sure use latest version of API"
            )
            SITES = None


async def search(key, site, message, method, owner_id=None):
    if method == "plugin" and site.lower() == BT4GPRX_SOURCE:
        await search_bt4gprx(key, message, owner_id)
        return
    if method.startswith("api"):
        if method == "apisearch":
            LOGGER.info(f"API Searching: {key} from {site}")
            if site == "all":
                api = f"{Config.SEARCH_API_LINK}/api/v1/all/search?query={key}&limit={Config.SEARCH_LIMIT}"
            else:
                api = f"{Config.SEARCH_API_LINK}/api/v1/search?site={site}&query={key}&limit={Config.SEARCH_LIMIT}"
        elif method == "apitrend":
            LOGGER.info(f"API Trending from {site}")
            if site == "all":
                api = f"{Config.SEARCH_API_LINK}/api/v1/all/trending?limit={Config.SEARCH_LIMIT}"
            else:
                api = f"{Config.SEARCH_API_LINK}/api/v1/trending?site={site}&limit={Config.SEARCH_LIMIT}"
        elif method == "apirecent":
            LOGGER.info(f"API Recent from {site}")
            if site == "all":
                api = f"{Config.SEARCH_API_LINK}/api/v1/all/recent?limit={Config.SEARCH_LIMIT}"
            else:
                api = f"{Config.SEARCH_API_LINK}/api/v1/recent?site={site}&limit={Config.SEARCH_LIMIT}"
        try:
            async with AsyncSession() as client:
                response = await client.get(api)
                search_results = response.json()
            if "error" in search_results or search_results["total"] == 0:
                await edit_message(
                    message,
                    f"No result found for <i>{key}</i>\nTorrent Site:- <i>{SITES.get(site)}</i>",
                )
                return
            msg = f"<b>Found {min(search_results['total'], TELEGRAPH_LIMIT)}</b>"
            if method == "apitrend":
                msg += f" <b>trending result(s)\nTorrent Site:- <i>{SITES.get(site)}</i></b>"
            elif method == "apirecent":
                msg += (
                    f" <b>recent result(s)\nTorrent Site:- <i>{SITES.get(site)}</i></b>"
                )
            else:
                msg += f" <b>result(s) for <i>{key}</i>\nTorrent Site:- <i>{SITES.get(site)}</i></b>"
            search_results = search_results["data"]
        except Exception as e:
            await edit_message(message, str(e))
            return
    else:
        LOGGER.info(f"PLUGINS Searching: {key} from {site}")
        search = await TorrentManager.qbittorrent.search.start(
            pattern=key, plugins=[site], category="all"
        )
        search_id = search.id
        while True:
            result_status = await TorrentManager.qbittorrent.search.status(search_id)
            status = result_status[0].status
            if status != "Running":
                break
        dict_search_results = await TorrentManager.qbittorrent.search.results(
            id=search_id, limit=TELEGRAPH_LIMIT
        )
        search_results = dict_search_results.results
        total_results = dict_search_results.total
        if total_results == 0:
            await edit_message(
                message,
                f"No result found for <i>{key}</i>\nTorrent Site:- <i>{site.capitalize()}</i>",
            )
            return
        msg = f"<b>Found {min(total_results, TELEGRAPH_LIMIT)}</b>"
        msg += f" <b>result(s) for <i>{key}</i>\nTorrent Site:- <i>{site.capitalize()}</i></b>"
        await TorrentManager.qbittorrent.search.delete(search_id)
    link = await get_result(search_results, key, message, method)
    buttons = ButtonMaker()
    buttons.url_button("🔎 VIEW", link, style=ButtonStyle.PRIMARY)
    button = buttons.build_menu(1)
    await edit_message(message, msg, button)



def _prune_result_cache():
    now = time()
    expired = [
        token for token, entry in _RESULT_CACHE.items()
        if now - entry["created"] > RESULT_CACHE_TTL
    ]
    for token in expired:
        _RESULT_CACHE.pop(token, None)
    while len(_RESULT_CACHE) > RESULT_CACHE_MAX:
        oldest = min(_RESULT_CACHE, key=lambda token: _RESULT_CACHE[token]["created"])
        _RESULT_CACHE.pop(oldest, None)


def _result_buttons(user_id, token, entry, page=0):
    buttons = ButtonMaker()
    total = len(entry["results"])
    buttons.url_button("VIEW RESULTS", entry["result_url"], style=ButtonStyle.PRIMARY)
    start = page * RESULT_BUTTON_PAGE_SIZE
    stop = min(start + RESULT_BUTTON_PAGE_SIZE, total)
    for index in range(start, stop):
        buttons.data_button(str(index + 1), f"tsearch {user_id} {token} {index}")
    if page > 0:
        buttons.data_button("Prev", f"tsearch {user_id} {token} p{page - 1}")
    if stop < total:
        buttons.data_button("Next", f"tsearch {user_id} {token} p{page + 1}")
    return buttons.build_menu(5)



def _result_page_text(key, entry, page=0):
    results = entry["results"]
    start = page * RESULT_BUTTON_PAGE_SIZE
    stop = min(start + RESULT_BUTTON_PAGE_SIZE, len(results))
    page_count = (len(results) + RESULT_BUTTON_PAGE_SIZE - 1) // RESULT_BUTTON_PAGE_SIZE
    lines = [
        f"<b>BT4G PRX: {len(results)} results for <i>{escape(key)}</i></b>",
        f"<i>Page {page + 1} of {page_count}. Tap a number to qbmirror that result.</i>",
        "",
    ]
    for index in range(start, stop):
        result = results[index]
        title = escape(result["title"][:240])
        size = escape(result["size"])
        lines.append(f"<b>{index + 1}.</b> {title}")
        lines.append(f"   <b>Size:</b> {size}")
    return "\n".join(lines)


async def search_bt4gprx(key, message, owner_id=None):
    LOGGER.info(f"BT4GPRX searching: {key}")
    try:
        async with AsyncSession() as client:
            response = await client.get(
                f"{BT4GPRX_URL}?{urlencode({'q': key, 'page': 'rss', 'category': 'movie'})}",
                timeout=30,
            )
            response.raise_for_status()
            root = ElementTree.fromstring(response.text)
        results = []
        for item in root.findall("./channel/item"):
            magnet = (item.findtext("link") or "").strip()
            title = (item.findtext("title") or "").strip()
            if not magnet.lower().startswith("magnet:?") or not title:
                continue
            description = unescape(item.findtext("description") or "")
            plain_description = description.replace("<br>", "\n").replace("<br/>", "\n")
            size_match = re_search(
                r"(?i)\b\d+(?:\.\d+)?\s*(?:bytes?|[kmgt]i?b)\b",
                plain_description,
            )
            results.append({
                "title": title,
                "magnet": magnet,
                "size": size_match.group(0) if size_match else "Unknown",
            })
            if len(results) >= TELEGRAPH_LIMIT:
                break
    except Exception as error:
        LOGGER.exception(f"BT4GPRX search failed: {error}")
        await edit_message(message, "BT4G PRX search failed. Please try again later.")
        return

    if not results:
        await edit_message(message, f"No BT4G PRX results found for <i>{escape(key)}</i>.")
        return

    content = [f"<h4>BT4G PRX Results For {escape(key)}</h4>"]
    for index, result in enumerate(results, start=1):
        title = escape(result["title"])
        magnet = escape(result["magnet"], quote=True)
        content.append(
            f"<b>{index}.</b> <a href='{magnet}'>{title}</a><br>"
            f"<b>Size: </b>{escape(result['size'])}<br><br>"
        )
    telegraph_pages = []
    current = ""
    for item in content:
        if len((current + item).encode("utf-8")) > 39000:
            telegraph_pages.append(current)
            current = ""
        current += item
    if current:
        telegraph_pages.append(current)

    try:
        paths = [
            (await telegraph.create_page(
                title="BT4G PRX Torrent Search",
                content=page,
            ))["path"]
            for page in telegraph_pages
        ]
        if len(paths) > 1:
            await telegraph.edit_telegraph(paths, telegraph_pages)
        result_url = f"https://telegra.ph/{paths[0]}"
    except Exception as error:
        LOGGER.exception(f"BT4G PRX Telegraph creation failed: {error}")
        await edit_message(message, "Search succeeded, but the results page could not be created.")
        return

    if owner_id is None:
        original = getattr(message, "reply_to_message", None)
        original_user = getattr(original, "from_user", None)
        owner_id = getattr(original_user, "id", None)
    if owner_id is None:
        owner_id = message.from_user.id
    user_id = owner_id
    token = uuid4().hex[:8]
    _prune_result_cache()
    _RESULT_CACHE[token] = {
        "created": time(),
        "user_id": user_id,
        "chat_id": message.chat.id,
        "message_id": message.id,
        "results": results,
        "result_url": result_url,
        "query": key,
        "used": set(),
    }
    _prune_result_cache()

    await edit_message(
        message,
        _result_page_text(key, _RESULT_CACHE[token]),
        _result_buttons(user_id, token, _RESULT_CACHE[token]),
    )


async def torrent_search_result_update(client, query):
    _prune_result_cache()
    parts = query.data.split()
    if len(parts) != 4:
        await query.answer("This result button is invalid.", show_alert=True)
        return
    _, owner, token, selection = parts
    entry = _RESULT_CACHE.get(token)
    if not entry or time() - entry["created"] > RESULT_CACHE_TTL:
        await query.answer("These search results have expired. Run /search again.", show_alert=True)
        return
    if query.from_user.id != int(owner) or entry["user_id"] != int(owner):
        await query.answer("These search buttons belong to another user.", show_alert=True)
        return
    if (
        query.message.chat.id != entry["chat_id"]
        or query.message.id != entry["message_id"]
    ):
        await query.answer("These search results are no longer active.", show_alert=True)
        return

    if selection.startswith("p"):
        page = int(selection[1:])
        total = len(entry["results"])
        if page < 0 or page * RESULT_BUTTON_PAGE_SIZE >= total:
            await query.answer("That results page is unavailable.", show_alert=True)
            return
        await query.answer()
        await edit_message(
            query.message,
            _result_page_text(entry["query"], entry, page),
            _result_buttons(int(owner), token, entry, page),
        )
        return

    try:
        index = int(selection)
        result = entry["results"][index]
    except (ValueError, IndexError):
        await query.answer("That result is unavailable.", show_alert=True)
        return
    if index in entry["used"]:
        await query.answer("This result was already queued.", show_alert=True)
        return

    entry["used"].add(index)
    try:
        from .mirror_leech import qb_mirror
        from ..helper.telegram_helper.bot_commands import BotCommands

        mirror_message = copy(query.message)
        # The copied Pyrogram Message loses the client reference used by
        # reply()/reply_photo() during task status updates.
        mirror_message._client = client
        # A result message can launch several tasks, so each task needs its
        # own internal ID while retaining the real Telegram message ID for replies.
        mirror_message._task_id = uuid4().int
        command_names = BotCommands.QbMirrorCommand
        command_name = (
            command_names[0]
            if isinstance(command_names, (list, tuple))
            else command_names
        )
        mirror_message.text = f"/{command_name} {result['magnet']}"
        mirror_message.from_user = query.from_user
        await query.answer(f"Starting result {index + 1} in qBittorrent.")
        await qb_mirror(client, mirror_message)
        LOGGER.info(
            f"BT4G PRX result {index + 1} queued by user {query.from_user.id}: "
            f"{result['title']}"
        )
    except Exception as error:
        entry["used"].discard(index)
        LOGGER.exception(f"BT4G PRX result dispatch failed: {error}")
        await query.answer("Could not start qbmirror for this result.", show_alert=True)


async def get_result(search_results, key, message, method):
    telegraph_content = []
    if method == "apirecent":
        msg = "<h4>API Recent Results</h4>"
    elif method == "apisearch":
        msg = f"<h4>API Search Result(s) For {key}</h4>"
    elif method == "apitrend":
        msg = "<h4>API Trending Results</h4>"
    else:
        msg = f"<h4>PLUGINS Search Result(s) For {key}</h4>"
    for index, result in enumerate(search_results, start=1):
        if method.startswith("api"):
            try:
                if "name" in result.keys():
                    msg += f"<code><a href='{result['url']}'>{escape(result['name'])}</a></code><br>"
                if "torrents" in result.keys():
                    for subres in result["torrents"]:
                        msg += f"<b>Quality: </b>{subres['quality']} | <b>Type: </b>{subres['type']} | "
                        msg += f"<b>Size: </b>{subres['size']}<br>"
                        if "torrent" in subres.keys():
                            msg += f"<a href='{subres['torrent']}'>Direct Link</a><br>"
                        elif "magnet" in subres.keys():
                            msg += "<b>Share Magnet to</b> "
                            msg += f"<a href='http://t.me/share/url?url={subres['magnet']}'>Telegram</a><br>"
                    msg += "<br>"
                else:
                    msg += f"<b>Size: </b>{result['size']}<br>"
                    try:
                        msg += f"<b>Seeders: </b>{result['seeders']} | <b>Leechers: </b>{result['leechers']}<br>"
                    except Exception:
                        pass
                    if "torrent" in result.keys():
                        msg += f"<a href='{result['torrent']}'>Direct Link</a><br><br>"
                    elif "magnet" in result.keys():
                        msg += "<b>Share Magnet to</b> "
                        msg += f"<a href='http://t.me/share/url?url={quote(result['magnet'])}'>Telegram</a><br><br>"
                    else:
                        msg += "<br>"
            except Exception:
                continue
        else:
            msg += f"<a href='{result.descrLink}'>{escape(result.fileName)}</a><br>"
            msg += f"<b>Size: </b>{get_readable_file_size(result.fileSize)}<br>"
            msg += f"<b>Seeders: </b>{result.nbSeeders} | <b>Leechers: </b>{result.nbLeechers}<br>"
            link = result.fileUrl
            if link.startswith("magnet:"):
                msg += f"<b>Share Magnet to</b> <a href='http://t.me/share/url?url={quote(link)}'>Telegram</a><br><br>"
            else:
                msg += f"<a href='{link}'>Direct Link</a><br><br>"

        if len(msg.encode("utf-8")) > 39000:
            telegraph_content.append(msg)
            msg = ""

        if index == TELEGRAPH_LIMIT:
            break

    if msg != "":
        telegraph_content.append(msg)

    await edit_message(
        message, f"<b>Creating</b> {len(telegraph_content)} <b>Telegraph pages.</b>"
    )
    path = [
        (
            await telegraph.create_page(
                title="Mirror-leech-bot Torrent Search", content=content
            )
        )["path"]
        for content in telegraph_content
    ]
    if len(path) > 1:
        await edit_message(
            message, f"<b>Editing</b> {len(telegraph_content)} <b>Telegraph pages.</b>"
        )
        await telegraph.edit_telegraph(path, telegraph_content)
    return f"https://telegra.ph/{path[0]}"


def api_buttons(user_id, method):
    buttons = ButtonMaker()
    for data, name in SITES.items():
        buttons.data_button(name, f"torser {user_id} {data} {method}")
    buttons.data_button("Cancel", f"torser {user_id} cancel")
    return buttons.build_menu(2)


async def plugin_buttons(user_id):
    buttons = ButtonMaker()
    if not PLUGINS:
        PLUGINS.append(BT4GPRX_SOURCE)
        pl = await TorrentManager.qbittorrent.search.plugins()
        PLUGINS.extend(
            i.name for i in pl if i.name.lower() != BT4GPRX_SOURCE
        )
    sources = list(dict.fromkeys(PLUGINS))
    for site_name in sources:
        buttons.data_button(
            "BT4G PRX" if site_name == BT4GPRX_SOURCE else site_name.capitalize(),
            f"torser {user_id} {site_name} plugin",
        )
    if len(sources) > 1 and BT4GPRX_SOURCE not in sources:
        buttons.data_button("All", f"torser {user_id} all plugin")
    buttons.data_button("Cancel", f"torser {user_id} cancel")
    return buttons.build_menu(2)


@new_task
async def torrent_search(_, message):
    if Config.DISABLE_SEARCH:
        await send_message(
            message, "Torrent search is currently disabled by the Bot Owner."
        )
        return
    user_id = message.from_user.id
    buttons = ButtonMaker()
    key = message.text.split()
    if SITES is None and not PLUGINS:
        await send_message(
            message, "No API link or search PLUGINS added for this function"
        )
    elif len(key) == 1 and SITES is None:
        await send_message(message, "Send a search key along with command")
    elif len(key) == 1:
        buttons.data_button("Trending", f"torser {user_id} apitrend")
        buttons.data_button("Recent", f"torser {user_id} apirecent")
        buttons.data_button("Cancel", f"torser {user_id} cancel")
        button = buttons.build_menu(2)
        await send_messae(message, "Send a search key along with command", button)
    elif SITES is not None and PLUGINS:
        buttons.data_button("Api", f"torser {user_id} apisearch")
        buttons.data_button("Plugins", f"torser {user_id} plugin")
        buttons.data_button("Cancel", f"torser {user_id} cancel")
        button = buttons.build_menu(2)
        await send_message(message, "Choose tool to search:", button)
    elif SITES is not None:
        button = api_buttons(user_id, "apisearch")
        await send_message(message, "Choose site to search | API:", button)
    elif len(PLUGINS) == 1:
        status = await send_message(
            message, f"Searching BT4G PRX for <i>{escape(' '.join(key[1:]))}</i>..."
        )
        await search(" ".join(key[1:]), PLUGINS[0], status, "plugin", user_id)
    else:
        button = await plugin_buttons(user_id)
        await send_message(message, "Choose site to search | Plugins:", button)


@new_task
async def torrent_search_update(_, query):
    user_id = query.from_user.id
    message = query.message
    key = message.reply_to_message.text.split(maxsplit=1)
    key = key[1].strip() if len(key) > 1 else None
    data = query.data.split()
    if user_id != int(data[1]):
        await query.answer("Not Yours!", show_alert=True)
    elif data[2].startswith("api"):
        await query.answer()
        button = api_buttons(user_id, data[2])
        await edit_message(message, "Choose site:", button)
    elif data[2] == "plugin":
        await query.answer()
        button = await plugin_buttons(user_id)
        await edit_message(message, "Choose site:", button)
    elif data[2] != "cancel":
        await query.answer()
        site = data[2]
        method = data[3]
        if method.startswith("api"):
            if key is None:
                if method == "apirecent":
                    endpoint = "Recent"
                elif method == "apitrend":
                    endpoint = "Trending"
                await edit_message(
                    message,
                    f"<b>Listing {endpoint} Items...\nTorrent Site:- <i>{SITES.get(site)}</i></b>",
                )
            else:
                await edit_message(
                    message,
                    f"<b>Searching for <i>{key}</i>\nTorrent Site:- <i>{SITES.get(site)}</i></b>",
                )
        else:
            await edit_message(
                message,
                f"<b>Searching for <i>{key}</i>\nTorrent Site:- <i>{site.capitalize()}</i></b>",
            )
        await search(key, site, message, method, user_id)
    else:
        await query.answer()
        await edit_message(message, "Search has been canceled!")
