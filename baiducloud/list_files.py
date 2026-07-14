#!/usr/bin/env python3
"""Authorize with Baidu Netdisk and list files without printing credentials."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import deque
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
KEYS_PATH = ROOT / "keys.txt"
TOKEN_PATH = ROOT / "token.json"
TOKEN_URL = "https://openapi.baidu.com/oauth/2.0/token"
FILES_URL = "https://pan.baidu.com/rest/2.0/xpan/file"


def load_keys() -> dict[str, str]:
    lines = [line.strip() for line in KEYS_PATH.read_text().splitlines() if line.strip()]
    if len(lines) % 2:
        raise RuntimeError("keys.txt 应为每个名称后紧跟一行对应值")

    keys = {lines[index].lower(): lines[index + 1] for index in range(0, len(lines), 2)}
    if not keys.get("appkey") or not keys.get("secretkey"):
        raise RuntimeError("keys.txt 缺少 AppKey 或 Secretkey")
    return keys


def request_json(url: str, params: dict[str, Any], *, post: bool = False) -> dict[str, Any]:
    encoded = urllib.parse.urlencode(params).encode()
    request = urllib.request.Request(
        url if post else f"{url}?{encoded.decode()}",
        data=encoded if post else None,
        headers={"User-Agent": "pan.baidu.com"},
        method="POST" if post else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {error.code}: {body}") from error


def exchange_code(code: str, keys: dict[str, str]) -> dict[str, Any]:
    token = request_json(
        TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": keys["appkey"],
            "client_secret": keys["secretkey"],
            "redirect_uri": "oob",
        },
        post=True,
    )
    if "access_token" not in token:
        message = token.get("error_description") or token.get("error") or token
        raise RuntimeError(f"授权失败：{message}")

    return save_token(token)


def authorization_url(keys: dict[str, str]) -> str:
    return "https://openapi.baidu.com/oauth/2.0/authorize?" + urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": keys["appkey"],
            "redirect_uri": "oob",
            "scope": "basic,netdisk",
        }
    )


def save_token(token: dict[str, Any]) -> dict[str, Any]:
    token["obtained_at"] = int(time.time())
    TOKEN_PATH.write_text(json.dumps(token, ensure_ascii=False, indent=2) + "\n")
    os.chmod(TOKEN_PATH, 0o600)
    return token


def load_token(keys: dict[str, str]) -> dict[str, Any]:
    if not TOKEN_PATH.exists():
        raise RuntimeError("缺少本地令牌，请通过 --code 提供一次性授权码")

    token = json.loads(TOKEN_PATH.read_text())
    expires_at = int(token.get("obtained_at", 0)) + int(token.get("expires_in", 0))
    if expires_at > int(time.time()) + 60:
        return token

    refresh_token = token.get("refresh_token")
    if not refresh_token:
        raise RuntimeError("访问令牌已过期且无法刷新，请重新授权")

    refreshed = request_json(
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
        raise RuntimeError(f"刷新令牌失败：{message}")
    return save_token(refreshed)


def list_directory(access_token: str, directory: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    start = 0
    while True:
        result = request_json(
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
            raise RuntimeError(
                f"读取目录 {directory} 失败：errno={result.get('errno')}, "
                f"request_id={result.get('request_id')}"
            )

        page = result.get("list", [])
        entries.extend(page)
        if not result.get("has_more") or not page:
            return entries
        start += len(page)


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    raise AssertionError("unreachable")


def print_entries(
    access_token: str, recursive: bool, start_directory: str = "/"
) -> None:
    pending = deque([start_directory])
    total_files = 0
    total_directories = 0

    while pending:
        directory = pending.popleft()
        for entry in list_directory(access_token, directory):
            path = entry.get("path", entry.get("server_filename", ""))
            if entry.get("isdir"):
                total_directories += 1
                print(f"[目录] {path}")
                if recursive:
                    pending.append(path)
            else:
                total_files += 1
                print(f"[文件] {path} ({format_size(int(entry.get('size', 0)))})")

    scope = f"{start_directory}（递归）" if recursive else start_directory
    print(f"\n{scope}：{total_directories} 个目录，{total_files} 个文件")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--authorize",
        action="store_true",
        help="打开 OAuth 授权页面，不读取文件",
    )
    parser.add_argument("--code", help="OAuth 页面返回的一次性授权码")
    parser.add_argument("--dir", default="/", help="要列出的云盘目录（默认 /）")
    parser.add_argument("--recursive", action="store_true", help="递归列出所有目录")
    args = parser.parse_args()

    try:
        keys = load_keys()
        if args.authorize:
            url = authorization_url(keys)
            webbrowser.open(url)
            print("已打开百度 OAuth 授权页面。若浏览器未打开，请访问：")
            print(url)
            return 0
        if not args.dir.startswith("/"):
            raise RuntimeError("--dir 必须是以 / 开头的云盘绝对路径")
        token = exchange_code(args.code, keys) if args.code else load_token(keys)
        print_entries(token["access_token"], args.recursive, args.dir)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
