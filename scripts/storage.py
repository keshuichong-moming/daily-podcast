#!/usr/bin/env python3
"""音频存储抽象层。

当前默认使用 GitLocalStorage（直接提交 MP3 到仓库并由 GitHub Pages 分发），
同时预留 CloudflareR2Storage 接口，后续容量增长后可零改造切换对象存储。
"""
from __future__ import annotations

import json
import os
import shutil
import urllib.parse
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from channel_config import ChannelConfig


@dataclass
class AudioAsset:
    stem: str
    filename: str
    url: str
    size: int
    duration_sec: int | None = None
    local_path: Path | None = None


class AudioStorage(ABC):
    """音频存储后端抽象接口。"""

    @abstractmethod
    def exists(self, stem: str) -> bool:
        """判断某期音频是否已存在。"""

    @abstractmethod
    def publish(self, local_mp3: Path, stem: str, duration_sec: int) -> AudioAsset:
        """发布已合成的 MP3 音频并返回公开分发信息。"""

    @abstractmethod
    def list_assets(self) -> list[AudioAsset]:
        """列出当前频道全部可上架的音频资产（按 stem 降序）。"""


class GitLocalStorage(AudioStorage):
    """默认后端：将 MP3 保存在仓库 episodes/ 目录，由 GitHub Pages 直接提供 Range 访问。"""

    def __init__(self, channel: ChannelConfig):
        self.channel = channel
        self.episodes_dir = channel.episodes_path
        self.url_prefix = channel.episodes_url_prefix.rstrip("/")

    def _mp3_path(self, stem: str) -> Path:
        return self.episodes_dir / f"{stem}.mp3"

    def exists(self, stem: str) -> bool:
        p = self._mp3_path(stem)
        return p.exists() and p.stat().st_size > 1000

    def publish(self, local_mp3: Path, stem: str, duration_sec: int) -> AudioAsset:
        self.episodes_dir.mkdir(parents=True, exist_ok=True)
        dest = self._mp3_path(stem)
        if local_mp3.resolve() != dest.resolve():
            shutil.copy2(local_mp3, dest)
        return AudioAsset(
            stem=stem,
            filename=dest.name,
            url=f"{self.url_prefix}/{urllib.parse.quote(dest.name)}",
            size=dest.stat().st_size,
            duration_sec=duration_sec,
            local_path=dest,
        )

    def list_assets(self) -> list[AudioAsset]:
        if not self.episodes_dir.exists():
            return []
        mp3s = sorted(self.episodes_dir.glob("*.mp3"), key=lambda p: p.name, reverse=True)
        assets: list[AudioAsset] = []
        for p in mp3s:
            assets.append(
                AudioAsset(
                    stem=p.stem,
                    filename=p.name,
                    url=f"{self.url_prefix}/{urllib.parse.quote(p.name)}",
                    size=p.stat().st_size,
                    duration_sec=None,
                    local_path=p,
                )
            )
        return assets


class CloudflareR2Storage(AudioStorage):
    """预留后端：将 MP3 上传到 Cloudflare R2，仅在仓库 .state/audio_manifest.json 保存元数据。

    启用方式（未来切换时）：
    1. 安装 boto3；
    2. 配置环境变量 R2_ENDPOINT_URL / R2_BUCKET / R2_PUBLIC_BASE_URL / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY；
    3. 设置 AUDIO_STORAGE_BACKEND=r2 或在频道 JSON 中把 storage.backend 改为 "r2"。
    """

    def __init__(self, channel: ChannelConfig):
        self.channel = channel
        self.manifest_path = channel.state_path.parent / f"{channel.id}-audio-manifest.json"
        self.endpoint_url = os.environ.get("R2_ENDPOINT_URL", "").strip()
        self.bucket = (os.environ.get("R2_BUCKET", "").strip() or channel.storage.bucket)
        self.public_base = (
            os.environ.get("R2_PUBLIC_BASE_URL", "").strip() or channel.storage.public_base_url
        ).rstrip("/")
        self.key_prefix = channel.storage.key_prefix.strip("/")

    def _load_manifest(self) -> dict[str, dict[str, Any]]:
        if self.manifest_path.exists():
            try:
                data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception:
                return {}
        return {}

    def _save_manifest(self, data: dict[str, dict[str, Any]]) -> None:
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _s3_client(self):
        try:
            import boto3  # type: ignore
        except ImportError as e:
            raise RuntimeError("启用 Cloudflare R2 存储需要安装 boto3（pip install boto3）。") from e
        ak = os.environ.get("R2_ACCESS_KEY_ID", "").strip()
        sk = os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()
        if not (self.endpoint_url and self.bucket and self.public_base and ak and sk):
            raise RuntimeError("Cloudflare R2 配置不完整，请检查 R2_* 环境变量。")
        return boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            aws_access_key_id=ak,
            aws_secret_access_key=sk,
            region_name="auto",
        )

    def exists(self, stem: str) -> bool:
        return stem in self._load_manifest()

    def publish(self, local_mp3: Path, stem: str, duration_sec: int) -> AudioAsset:
        filename = f"{stem}.mp3"
        key = f"{self.key_prefix}/{filename}" if self.key_prefix else filename
        client = self._s3_client()
        client.upload_file(
            str(local_mp3),
            self.bucket,
            key,
            ExtraArgs={"ContentType": "audio/mpeg", "CacheControl": "public, max-age=31536000"},
        )
        size = local_mp3.stat().st_size
        url = f"{self.public_base}/{urllib.parse.quote(key)}"
        manifest = self._load_manifest()
        manifest[stem] = {
            "filename": filename,
            "url": url,
            "size": size,
            "duration_sec": duration_sec,
        }
        self._save_manifest(manifest)
        return AudioAsset(
            stem=stem,
            filename=filename,
            url=url,
            size=size,
            duration_sec=duration_sec,
            local_path=None,
        )

    def list_assets(self) -> list[AudioAsset]:
        manifest = self._load_manifest()
        assets: list[AudioAsset] = []
        for stem in sorted(manifest.keys(), reverse=True):
            item = manifest[stem]
            assets.append(
                AudioAsset(
                    stem=stem,
                    filename=str(item.get("filename") or f"{stem}.mp3"),
                    url=str(item["url"]),
                    size=int(item["size"]),
                    duration_sec=int(item["duration_sec"]) if item.get("duration_sec") else None,
                    local_path=None,
                )
            )
        return assets


def create_audio_storage(channel: ChannelConfig) -> AudioStorage:
    """根据频道配置构造对应存储后端。"""
    backend = channel.storage.backend
    if backend == "r2":
        return CloudflareR2Storage(channel)
    return GitLocalStorage(channel)
