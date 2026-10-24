"""Async client for the documented Seedr Personal Access Token API."""

import json
import re

from niquests import AsyncSession

from bot import LOGGER


def _safe_error_detail(value, access_token):
    detail = str(value or "")
    if access_token:
        detail = detail.replace(access_token, "[redacted]")
    detail = re.sub(r"(?i)bearer\s+[^\s,;]+", "Bearer [redacted]", detail)
    detail = re.sub(r"magnet:\?\S+", "magnet:[redacted]", detail)
    detail = re.sub(r"(https?://[^\s?\"']+)\?[^\s\"']+", r"\1?[redacted]", detail)
    return " ".join(detail.split())[:300]


class SeedrApiError(RuntimeError):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class SeedrApiClient:
    API_BASE = "https://api.seedr.cc/api/v0.1/p"

    def __init__(self, access_token):
        self.access_token = (access_token or "").strip()
        if not self.access_token:
            raise ValueError("Seedr API token is not configured")

    async def request(self, method, endpoint, *, data=None, params=None):
        url = f"{self.API_BASE}{endpoint}"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
        }
        try:
            async with AsyncSession(timeout=20) as client:
                response = await client.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    **({"data": data} if data is not None else {}),
                )
                status = getattr(response, "status_code", 0)
                if status == 204:
                    return {}
                try:
                    payload = response.json()
                except Exception:
                    payload = None
                content_type = (getattr(response, "headers", None) or {}).get("Content-Type", "unknown")
                if status < 200 or status >= 300:
                    detail = "Seedr API request failed"
                    if isinstance(payload, dict):
                        detail = (
                            payload.get("error_description")
                            or payload.get("error")
                            or payload.get("message")
                        )
                        if not detail:
                            detail = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                    elif payload is not None:
                        detail = payload
                    else:
                        detail = getattr(response, "text", "") or detail
                    detail = _safe_error_detail(detail, self.access_token) or "empty error response"
                    message = (
                        f"Seedr API {method.upper()} {endpoint} returned HTTP {status} "
                        f"({content_type}): {detail}"
                    )
                    LOGGER.warning(message)
                    raise SeedrApiError(message, status)
                if payload is None:
                    message = (
                        f"Seedr API {method.upper()} {endpoint} returned invalid JSON "
                        f"(HTTP {status}, {content_type})"
                    )
                    LOGGER.warning(message)
                    raise SeedrApiError(message, status)
                return payload
        except SeedrApiError:
            raise
        except Exception as exc:
            # Do not include request headers, URLs, or credentials in connection errors.
            message = (
                f"Seedr API {method.upper()} {endpoint} connection failed: "
                f"{type(exc).__name__}"
            )
            LOGGER.warning(message)
            raise SeedrApiError(message) from exc

    async def get_quota(self):
        return await self.request("GET", "/me/quota")

    async def add_task(self, link):
        data = {"folder_id": 0}
        if link.lower().startswith("magnet:"):
            data["torrent_magnet"] = link
        else:
            data["torrent_url"] = link
        return await self.request("POST", "/tasks", data=data)

    async def get_task(self, task_id):
        return await self.request("GET", f"/tasks/{task_id}")

    async def get_task_contents(self, task_id):
        return await self.request("GET", f"/tasks/{task_id}/contents")

    async def get_file_url(self, file_id):
        return await self.request("GET", f"/download/file/{file_id}/url")

    async def delete_task(self, task_id):
        return await self.request("DELETE", f"/tasks/{task_id}")

    async def delete_file(self, file_id):
        return await self.request("DELETE", f"/fs/file/{file_id}")

    # SeedrStatus uses this compatibility method when the user cancels a task.
    async def delete(self, kind, item_id):
        if kind == "torrent":
            return await self.delete_task(item_id)
        if kind == "file":
            return await self.delete_file(item_id)
        raise ValueError(f"Unsupported Seedr resource type: {kind}")
