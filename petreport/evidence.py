import asyncio
import hashlib
import os

import httpx

from .config import settings
from .db import uid


class EvidenceError(Exception):
    pass


class FrigateClient:
    def __init__(self):
        cfg = settings().frigate
        self.client = httpx.AsyncClient(
            base_url=cfg.base_url.rstrip("/"),
            timeout=httpx.Timeout(cfg.timeout_seconds),
            follow_redirects=False,
            verify=True,
        )
        self.login_lock = asyncio.Lock()

    async def login(self):
        cfg = settings().frigate
        if not cfg.password.get_secret_value():
            raise EvidenceError("frigate_not_configured")
        async with self.login_lock:
            result = await self.client.post(
                "/api/login", json={"user": cfg.username, "password": cfg.password.get_secret_value()}
            )
            if result.status_code != 200:
                raise EvidenceError("frigate_auth_failed")

    async def download(self, event_id: str, variant: str, relative_path: str):
        cfg = settings().frigate
        if variant == "bbox" and not cfg.bbox_verified:
            raise EvidenceError("bbox_not_verified")
        if variant == "clip" and not cfg.clip_verified:
            raise EvidenceError("clip_not_verified")
        clip = variant == "clip"
        path = f"/api/events/{event_id}/" + ("clip.mp4" if clip else "snapshot.jpg")
        params = {"bbox": 1} if variant == "bbox" else {}
        limit = cfg.max_clip_bytes if clip else cfg.max_snapshot_bytes
        mime = "video/mp4" if clip else "image/jpeg"
        target = settings().media_root / relative_path
        temporary = target.with_name(".tmp-" + uid())
        try:
            for attempt in range(2):
                async with self.client.stream("GET", path, params=params) as response:
                    if response.status_code == 401 and attempt == 0:
                        await self.login()
                        continue
                    if response.status_code != 200:
                        raise EvidenceError(
                            "source_missing" if response.status_code == 404 else "source_unavailable"
                        )
                    if response.headers.get("content-type", "").split(";")[0].strip() != mime:
                        raise EvidenceError("invalid_media_type")
                    length = response.headers.get("content-length")
                    if length and (not length.isdigit() or int(length) > limit):
                        raise EvidenceError("media_too_large")
                    size = 0
                    signature = b""
                    digest = hashlib.sha256()
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with temporary.open("xb") as output:
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > limit:
                                raise EvidenceError("media_too_large")
                            signature = (signature + chunk)[:32]
                            digest.update(chunk)
                            output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                    if (
                        size < 12
                        or (clip and signature[4:8] != b"ftyp")
                        or (not clip and not signature.startswith(b"\xff\xd8\xff"))
                    ):
                        raise EvidenceError("invalid_media_signature")
                    os.replace(temporary, target)
                    return {"sha256": digest.hexdigest(), "bytes": size, "mime": mime}
            raise EvidenceError("frigate_auth_failed")
        except (httpx.HTTPError, OSError):
            raise EvidenceError("media_transport_or_storage_failure") from None
        finally:
            temporary.unlink(missing_ok=True)

    async def close(self):
        await self.client.aclose()


def media_path(relative_path):
    root = settings().media_root.resolve()
    path = (root / relative_path).resolve()
    if not path.is_relative_to(root):
        raise EvidenceError("invalid_media_path")
    return path
