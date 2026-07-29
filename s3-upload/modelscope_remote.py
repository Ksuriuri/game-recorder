#!/usr/bin/env python3
"""ModelScope helpers for S3 upload skip-if-complete checks.

Uses the same dataset/credentials as modelscope-upload. Only lists remote
metadata — never downloads session videos.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

os.environ.setdefault("MODELSCOPE_LOG_LEVEL", str(logging.ERROR))

DEFAULT_REPO_ID = "kusriri/world-game-data"
MODELSCOPE_TOKEN = "ms-54fac99a-5958-42d4-879d-b9445227cb51"
DATASET_RECORDINGS_DIR = "recordings"


@dataclass(frozen=True)
class RemoteFile:
    size: int
    in_check: bool


def make_api(token: str = MODELSCOPE_TOKEN) -> Any:
    from modelscope.hub.api import HubApi

    api = HubApi()
    api.login(token)
    return api


def _entry_name(item: dict) -> str | None:
    path = (item.get("Path") or item.get("Name") or "").strip().strip("/")
    if not path:
        return None
    return path.split("/")[-1]


def list_session_folders(
    api,
    repo_id: str,
    token: str,
    *,
    dataset_dir: str = DATASET_RECORDINGS_DIR,
) -> set[str]:
    from modelscope.utils.constant import DEFAULT_DATASET_REVISION

    remote: set[str] = set()
    page = 1
    page_size = 100
    root_path = f"/{dataset_dir.strip('/')}"
    while True:
        batch = api.get_dataset_files(
            repo_id=repo_id,
            revision=DEFAULT_DATASET_REVISION,
            root_path=root_path,
            recursive=False,
            page_number=page,
            page_size=page_size,
            token=token,
        )
        if not batch:
            break
        for item in batch:
            if item.get("Type") != "tree":
                continue
            name = _entry_name(item)
            if name:
                remote.add(name)
        if len(batch) < page_size:
            break
        page += 1
    return remote


def list_session_files(
    api,
    repo_id: str,
    token: str,
    session_name: str,
    *,
    dataset_dir: str = DATASET_RECORDINGS_DIR,
) -> dict[str, RemoteFile]:
    from modelscope.utils.constant import DEFAULT_DATASET_REVISION

    dataset_dir = dataset_dir.strip("/")
    session_root = f"{dataset_dir}/{session_name}"
    root_path = f"/{session_root}"
    prefix = f"{session_root}/"
    remote: dict[str, RemoteFile] = {}
    page = 1
    page_size = 100

    while True:
        batch = api.get_dataset_files(
            repo_id=repo_id,
            revision=DEFAULT_DATASET_REVISION,
            root_path=root_path,
            recursive=True,
            page_number=page,
            page_size=page_size,
            token=token,
        )
        if not batch:
            break
        for item in batch:
            if item.get("Type") != "blob":
                continue
            path = (item.get("Path") or item.get("Name") or "").strip().strip("/")
            if not path.startswith(prefix):
                continue
            relative_path = path[len(prefix) :]
            if not relative_path:
                continue
            remote[relative_path] = RemoteFile(
                size=int(item.get("Size") or 0),
                in_check=bool(item.get("InCheck")),
            )
        if len(batch) < page_size:
            break
        page += 1
    return remote


def matches_after_crlf_normalize(local_path: Path, local_size: int, remote_size: int) -> bool:
    """True when local CRLF text would match a remote LF blob of remote_size."""
    if local_size > 1024 * 1024:
        return False
    data = local_path.read_bytes()
    if b"\r\n" not in data:
        return False
    return len(data.replace(b"\r\n", b"\n")) == remote_size
