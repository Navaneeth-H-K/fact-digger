"""Where uploaded PDFs live.

Production uses Supabase Storage: the API hands the browser a signed upload URL so the file never
passes through a Vercel function (whose body limit is 4.5 MB). Local runs and tests use a
directory, where "signed upload" simply means the API's own multipart upload route.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote


@dataclass(frozen=True)
class UploadTarget:
    """Where and how a client should send the file bytes."""

    url: str
    method: str
    token: str | None


class Storage(Protocol):
    def create_upload_target(self, path: str) -> UploadTarget: ...

    def put(self, path: str, data: bytes) -> None: ...

    def get(self, path: str) -> bytes: ...

    def exists(self, path: str) -> bool: ...


class LocalDirStorage:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _resolve(self, path: str) -> Path:
        target = (self.root / path).resolve()
        if self.root not in target.parents:
            raise ValueError(f"storage path escapes root: {path}")
        return target

    def create_upload_target(self, path: str) -> UploadTarget:
        return UploadTarget(
            url=f"/documents/upload?path={quote(path, safe='')}", method="POST", token=None
        )

    def put(self, path: str, data: bytes) -> None:
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def get(self, path: str) -> bytes:
        return self._resolve(path).read_bytes()

    def exists(self, path: str) -> bool:
        return self._resolve(path).is_file()


class SupabaseStorage:
    """Thin wrapper over supabase-py's storage client (duck-typed so tests can stub it)."""

    def __init__(self, storage_client: Any, bucket: str) -> None:
        self._bucket = storage_client.from_(bucket)

    def create_upload_target(self, path: str) -> UploadTarget:
        signed = self._bucket.create_signed_upload_url(path)
        return UploadTarget(url=signed["signed_url"], method="PUT", token=signed.get("token"))

    def put(self, path: str, data: bytes) -> None:
        self._bucket.upload(path, data, {"content-type": "application/pdf", "upsert": "true"})

    def get(self, path: str) -> bytes:
        data: bytes = self._bucket.download(path)
        return data

    def exists(self, path: str) -> bool:
        folder, _, name = path.rpartition("/")
        return any(entry.get("name") == name for entry in self._bucket.list(folder))
