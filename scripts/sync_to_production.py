"""Seed the production layer (Supabase) from the local record run.

Reads the local database and storage from .env (DATABASE_URL, LOCAL_STORAGE_DIR) and the
production targets from SUPABASE_DATABASE_URL, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY and
SUPABASE_BUCKET. Ids are preserved so the committed samples/export.json matches the live site.
"""

from __future__ import annotations

import re
import sys
import urllib.parse
from pathlib import Path

from dotenv import dotenv_values
from supabase import create_client

from fkl.db import make_engine
from fkl.storage import LocalDirStorage, SupabaseStorage
from fkl.sync import copy_layer


def encoded_password(url: str) -> str:
    match = re.match(r"^(postgres(?:ql)?://)([^:]+):(.*)@([^@]+)$", url)
    if match and any(ch in match.group(3) for ch in "@#%/?"):
        quoted = urllib.parse.quote(match.group(3), safe="")
        return f"{match.group(1)}{match.group(2)}:{quoted}@{match.group(4)}"
    return url


def main() -> int:
    env = dotenv_values(".env")
    required = ("SUPABASE_DATABASE_URL", "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY")
    missing = [name for name in required if not env.get(name)]
    if missing:
        print(f"missing in .env: {', '.join(missing)}")
        return 1
    source_engine = make_engine(env.get("DATABASE_URL") or "sqlite:///./fkl.db")
    source_storage = LocalDirStorage(Path(env.get("LOCAL_STORAGE_DIR") or "./local_storage"))
    target_engine = make_engine(encoded_password(str(env["SUPABASE_DATABASE_URL"])))
    client = create_client(
        str(env["SUPABASE_URL"]).rstrip("/"), str(env["SUPABASE_SERVICE_ROLE_KEY"])
    )
    target_storage = SupabaseStorage(client.storage, bucket=env.get("SUPABASE_BUCKET") or "pdfs")
    summary = copy_layer(source_engine, source_storage, target_engine, target_storage)
    print("synced:", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
