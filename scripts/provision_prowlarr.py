#!/usr/bin/env python3
"""Provision Prowlarr auth and sync its generated API key into WZML config."""

import asyncio
import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.core.config_manager import Config
from bot.core.tg_client import db_partition_id
from pymongo import AsyncMongoClient
from pymongo.server_api import ServerApi

ENV_PATH = Path(os.getenv("PROWLARR_CONFIG_FILE", "config.env"))
XML_PATH = Path(os.getenv("PROWLARR_CONFIG_XML", "configs/prowlarr/config.xml"))


def read_env(path):
    values = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def update_env(path, key, value):
    lines = path.read_text(encoding="utf-8").splitlines()
    output = []
    found = False
    for line in lines:
        if line.strip().split("=", 1)[0].strip() == key:
            output.append(f"{key}={value}")
            found = True
        else:
            output.append(line)
    if not found:
        output.append(f"{key}={value}")
    # config.env is bind-mounted as a single file in the app container, so
    # replace(2) fails with EBUSY there. Keep its mode private and rewrite in place.
    path.chmod(0o600)
    with path.open("w", encoding="utf-8") as stream:
        stream.write("\n".join(output) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    path.chmod(0o600)


def api_request(base, api_key, route, method="GET", payload=None, timeout=8):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"X-Api-Key": api_key, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = Request(f"{base.rstrip('/')}{route}", data=data, headers=headers, method=method)
    with urlopen(request, timeout=timeout) as response:
        body = response.read()
        return response.status, json.loads(body) if body else None


async def sync_bot_config(api_key, base):
    Config.load()
    client = AsyncMongoClient(Config.DATABASE_URL, server_api=ServerApi("1"))
    try:
        bot_id = (Config.BOT_TOKEN or "").split(":", 1)[0]
        if not bot_id:
            raise RuntimeError("Bot identity is unavailable for Prowlarr config sync")
        partition = db_partition_id(bot_id)
        await client.wzmlx.settings.config.update_one(
            {"_id": partition},
            {"$set": {"PROWLARR_API_KEY": api_key, "PROWLARR_URL": base}},
            upsert=True,
        )
    finally:
        await client.close()


def main():
    if not ENV_PATH.is_file():
        raise RuntimeError(f"Missing protected Prowlarr settings file: {ENV_PATH}")
    env = read_env(ENV_PATH)
    base = env.get("PROWLARR_URL", "http://prowlarr:9696").rstrip("/")
    username = env.get("PROWLARR_USERNAME", "").strip()
    password = env.get("PROWLARR_PASSWORD", "")
    if not username or not password:
        raise RuntimeError("Set PROWLARR_USERNAME and PROWLARR_PASSWORD in config.env")

    deadline = time.monotonic() + 120
    last_error = None
    while time.monotonic() < deadline:
        try:
            with urlopen(base + "/ping", timeout=5):
                break
        except (OSError, URLError) as error:
            last_error = error
            time.sleep(3)
    else:
        raise RuntimeError(f"Prowlarr did not become reachable: {type(last_error).__name__}")

    root = ET.parse(XML_PATH).getroot()
    api_key = (root.findtext("ApiKey") or "").strip()
    if not api_key:
        raise RuntimeError("Prowlarr has not generated an API key yet")

    _, host = api_request(base, api_key, "/api/v1/config/host")
    host.update(
        {
            "authenticationMethod": "forms",
            "authenticationRequired": "enabled",
            "username": username,
            "password": password,
            "passwordConfirmation": password,
        }
    )
    api_request(base, api_key, f"/api/v1/config/host/{host['id']}", "PUT", host)
    status, _ = api_request(base, api_key, "/api/v1/system/status")
    if status != 200:
        raise RuntimeError(f"Prowlarr API health check returned HTTP {status}")

    update_env(ENV_PATH, "PROWLARR_API_KEY", api_key)
    asyncio.run(sync_bot_config(api_key, base))
    print("Prowlarr auth is provisioned; generated API key is synced to config.env and bot settings.")


if __name__ == "__main__":
    try:
        main()
    except (HTTPError, URLError, OSError, RuntimeError, ValueError) as error:
        print(f"Prowlarr provisioning failed: {type(error).__name__}: {error}")
        raise SystemExit(1)
