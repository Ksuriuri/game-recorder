#!/usr/bin/env python3
"""Baidu Netdisk helpers for S3 upload skip-if-complete checks (stdlib only).

Credentials are baked in for one-click distribution. Optional local
keys.txt / token.json under this pack override defaults and receive
refreshed tokens.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

TOKEN_URL = "https://openapi.baidu.com/oauth/2.0/token"
FILES_URL = "https://pan.baidu.com/rest/2.0/xpan/file"
DEFAULT_GAME_DATA_DIR = "/game-data"

# Bundled for recording operators — same account that holds /game-data.
# Local s3-upload/keys.txt + token.json override these when present.
DEFAULT_BAIDU_APP_ID = "123949723"
DEFAULT_BAIDU_APP_KEY = "4FFcAk3syKdpdRkeC1tW1m5dHezNbZpX"
DEFAULT_BAIDU_SECRET_KEY = "eiROaEoOA6VpFtuoWQ6lJ2JW0NtsHGtm"
DEFAULT_BAIDU_SIGN_KEY = "D~wze=F*UbAbSmqHSj=+f=lfLD$+LMWp"
DEFAULT_BAIDU_TOKEN: dict[str, Any] = {
    "expires_in": 2592000,
    "refresh_token": "122.b4d9a55272bdd6f73f8b2633d0d50b8c.YgS4-X5zyevnkSrOko6A4KxXMXeMe03Uo5rZxu8.s7uaOg",
    "access_token": "121.94766ea274eda397478dd1af6f365841.YHqS7coSXZ83eVnWT7fN2iqlHFGRHEJl980Pp7S.lk3UDA",
    "session_secret": "",
    "session_key": "",
    "scope": "basic netdisk",
    "obtained_at": 1783755959,
}


def default_keys() -> dict[str, str]:
    return {
        "appid": DEFAULT_BAIDU_APP_ID,
        "appkey": DEFAULT_BAIDU_APP_KEY,
        "secretkey": DEFAULT_BAIDU_SECRET_KEY,
        "signkey": DEFAULT_BAIDU_SIGN_KEY,
    }


def resolve_cred_dir(pack_root: Path, game_root: Path) -> Path:
    """Directory used to read/write optional credential overrides."""
    for path in (pack_root, game_root / "baiducloud"):
        if (path / "keys.txt").is_file() or (path / "token.json").is_file():
            return path
    return pack_root


def load_keys(cred_dir: Path) -> dict[str, str]:
    keys_path = cred_dir / "keys.txt"
    if not keys_path.is_file():
        return default_keys()

    lines = [
        line.strip()
        for line in keys_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if len(lines) % 2:
        raise RuntimeError(f"{keys_path} 应为每个名称后紧跟一行对应值")
    keys = {lines[index].lower(): lines[index + 1] for index in range(0, len(lines), 2)}
    if not keys.get("appkey") or not keys.get("secretkey"):
        raise RuntimeError(f"{keys_path} 缺少 AppKey 或 Secretkey")
    return keys


def _request_json(url: str, params: dict[str, Any], *, post: bool = False) -> dict[str, Any]:
    encoded = urllib.parse.urlencode(params).encode()
    request = urllib.request.Request(
        url if post else f"{url}?{encoded.decode()}",
        data=encoded if post else None,
        headers={"User-Agent": "pan.baidu.com"},
        method="POST" if post else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {error.code}: {body}") from error


def _save_token(cred_dir: Path, token: dict[str, Any]) -> dict[str, Any]:
    token["obtained_at"] = int(time.time())
    token_path = cred_dir / "token.json"
    token_path.write_text(
        json.dumps(token, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    try:
        os.chmod(token_path, 0o600)
    except OSError:
        pass
    return token


def load_token(cred_dir: Path, keys: dict[str, str]) -> dict[str, Any]:
    token_path = cred_dir / "token.json"
    if token_path.is_file():
        token = json.loads(token_path.read_text(encoding="utf-8"))
    else:
        token = dict(DEFAULT_BAIDU_TOKEN)

    expires_at = int(token.get("obtained_at", 0)) + int(token.get("expires_in", 0))
    if expires_at > int(time.time()) + 60 and token.get("access_token"):
        return token

    refresh_token = token.get("refresh_token")
    if not refresh_token:
        raise RuntimeError("百度访问令牌已过期且无法刷新，请更新内置令牌后重新分发")

    refreshed = _request_json(
        TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": keys["appkey"],
            "client_secret": keys["secretkey"],
        },
        post=True,
    )
    if "access_token" not in refreshed:
        message = refreshed.get("error_description") or refreshed.get("error") or refreshed
        raise RuntimeError(f"刷新百度令牌失败：{message}")
    # Always persist refreshed token next to the upload pack for later runs.
    return _save_token(cred_dir, refreshed)


def get_access_token(*, pack_root: Path, game_root: Path, cred_dir: Path | None = None) -> tuple[str, Path]:
    """Return (access_token, credential_dir). Uses bundled defaults when needed."""
    resolved = cred_dir.resolve() if cred_dir is not None else resolve_cred_dir(pack_root, game_root)
    keys = load_keys(resolved)
    token = load_token(resolved, keys)
    access_token = str(token.get("access_token") or "")
    if not access_token:
        raise RuntimeError("百度 access_token 为空")
    return access_token, resolved


def list_directory(access_token: str, directory: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    start = 0
    while True:
        result = _request_json(
            FILES_URL,
            {
                "method": "list",
                "access_token": access_token,
                "dir": directory,
                "start": start,
                "limit": 1000,
                "order": "name",
            },
        )
        if result.get("errno") != 0:
            errno = result.get("errno")
            if errno in (-9, 2, 31066):
                return []
            raise RuntimeError(
                f"读取百度目录 {directory} 失败：errno={errno}, "
                f"request_id={result.get('request_id')}"
            )
        page = result.get("list", [])
        entries.extend(page)
        if not result.get("has_more") or not page:
            return entries
        start += len(page)


def list_session_folders(access_token: str, *, game_data_dir: str = DEFAULT_GAME_DATA_DIR) -> set[str]:
    remote: set[str] = set()
    for entry in list_directory(access_token, game_data_dir):
        if not entry.get("isdir"):
            continue
        name = str(entry.get("server_filename") or "").strip()
        if name:
            remote.add(name)
    return remote


def list_session_files(
    access_token: str,
    session_name: str,
    *,
    game_data_dir: str = DEFAULT_GAME_DATA_DIR,
) -> dict[str, int]:
    """Return relative_path -> size for all files under /game-data/{session}/."""
    root = f"{game_data_dir.rstrip('/')}/{session_name}"
    files: dict[str, int] = {}
    pending = [root]
    while pending:
        directory = pending.pop()
        for entry in list_directory(access_token, directory):
            path = str(entry.get("path") or "")
            if entry.get("isdir"):
                if path:
                    pending.append(path)
                continue
            if not path.startswith(root + "/"):
                name = str(entry.get("server_filename") or "")
                if name:
                    files[name] = int(entry.get("size") or 0)
                continue
            relative = path[len(root) + 1 :]
            if relative:
                files[relative] = int(entry.get("size") or 0)
    return files
