"""PDF storage backends: a local directory for tests/dev, Supabase Storage in production."""

from pathlib import Path
from typing import Any

import pytest

from fkl.storage import LocalDirStorage, SupabaseStorage, UploadTarget


def test_local_storage_round_trips_bytes_and_reports_existence(tmp_path: Path) -> None:
    storage = LocalDirStorage(tmp_path)
    assert storage.exists("pdfs/x.pdf") is False
    storage.put("pdfs/x.pdf", b"%PDF-1.4 test")
    assert storage.exists("pdfs/x.pdf") is True
    assert storage.get("pdfs/x.pdf") == b"%PDF-1.4 test"


def test_local_storage_rejects_paths_escaping_the_root(tmp_path: Path) -> None:
    storage = LocalDirStorage(tmp_path)
    with pytest.raises(ValueError):
        storage.put("../outside.pdf", b"x")


def test_local_storage_signed_upload_points_at_the_api_upload_route(tmp_path: Path) -> None:
    target = LocalDirStorage(tmp_path).create_upload_target("pdfs/x.pdf")
    assert target == UploadTarget(
        url="/documents/upload?path=pdfs%2Fx.pdf", method="POST", token=None
    )


class _FakeBucket:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.blobs: dict[str, bytes] = {}

    def create_signed_upload_url(self, path: str) -> dict[str, str]:
        self.calls.append(("signed", path))
        return {
            "signed_url": f"https://supa.test/upload/{path}?token=t",
            "token": "t",
            "path": path,
        }

    def upload(self, path: str, file: bytes, file_options: dict[str, str]) -> None:
        self.calls.append(("upload", path))
        self.blobs[path] = file

    def download(self, path: str) -> bytes:
        self.calls.append(("download", path))
        return self.blobs[path]

    def list(self, folder: str) -> list[dict[str, str]]:
        return [{"name": p.split("/")[-1]} for p in self.blobs if p.startswith(folder + "/")]


class _FakeStorageClient:
    def __init__(self, bucket: _FakeBucket) -> None:
        self.bucket = bucket

    def from_(self, name: str) -> _FakeBucket:
        assert name == "pdfs"
        return self.bucket


def test_supabase_storage_issues_signed_upload_targets_and_reads_back() -> None:
    bucket = _FakeBucket()
    storage = SupabaseStorage(_FakeStorageClient(bucket), bucket="pdfs")
    target = storage.create_upload_target("pdfs/x.pdf")
    assert (
        target.method == "PUT"
        and target.token == "t"
        and target.url.startswith("https://supa.test/")
    )
    storage.put("pdfs/x.pdf", b"data")
    assert storage.get("pdfs/x.pdf") == b"data"
    assert storage.exists("pdfs/x.pdf") is True
    assert storage.exists("pdfs/missing.pdf") is False
    assert [c[0] for c in bucket.calls] == ["signed", "upload", "download"]
