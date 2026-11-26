#!/usr/bin/env python3
"""频道配置加载模块（支持默认单频道根目录输出，并预留多频道扩展）。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_CHANNEL_REL = "channels/daily-open-source.json"


def resolve_repo_dir(repo_dir_arg: str | None = None) -> Path:
    """解析仓库根目录：参数优先，其次 REPO_DIR 环境变量，最后当前目录或脚本上级目录。"""
    raw = (repo_dir_arg or os.environ.get("REPO_DIR", "")).strip()
    if raw:
        return Path(raw).expanduser().resolve()
    cwd = Path.cwd().resolve()
    if (cwd / "channels").exists() or (cwd / "episodes").exists() or (cwd / ".git").exists():
        return cwd
    return Path(__file__).resolve().parent.parent


@dataclass
class TTSConfig:
    gemini_model: str = "gemini-3.8-flash-tts"
    gemini_voice_main: str = "Charon"
    gemini_voice_cohost: str = "Sulafat"
    gemini_style: str = "像懂技术的朋友聊天，语速自然，吐字清晰，中英混读顺滑，不要僵硬播音腔"
    gemini_pace_seconds: float = 22.0
    edge_voice_main: str = "zh-CN-YunxiNeural"
    edge_voice_cohost: str = "zh-CN-XiaoxiaoNeural"


@dataclass
class StorageConfig:
    backend: str = "git"
    public_base_url: str = ""
    bucket: str = ""
    key_prefix: str = "episodes"


@dataclass
class ChannelConfig:
    repo_dir: Path
    id: str = "daily-open-source"
    title: str = "每日开源播客"
    author: str = "打瞌睡"
    description: str = "每天 10–15 分钟，用说人话的方式聊过去 24 小时 AI Agent、开源黑马与实用开发工具。"
    language: str = "zh-cn"
    site_base: str = "https://keshuichong-moming.github.io/daily-podcast"
    output_dir: str = "."
    episodes_dir: str = "episodes"
    feed_file: str = "feed.xml"
    index_file: str = "index.html"
    cover_file: str = "cover.jpg"
    guid_prefix: str = "daily-podcast-"
    preferences_file: str = "preferences.md"
    history_file: str = "history.md"
    state_file: str = ".state/built_hashes.json"
    category: str = "Technology"
    explicit: bool = False
    tts: TTSConfig = field(default_factory=TTSConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)

    @property
    def output_path(self) -> Path:
        return (self.repo_dir / self.output_dir).resolve()

    @property
    def episodes_path(self) -> Path:
        return (self.repo_dir / self.episodes_dir).resolve()

    @property
    def feed_path(self) -> Path:
        return self.output_path / self.feed_file

    @property
    def index_path(self) -> Path:
        return self.output_path / self.index_file

    @property
    def preferences_path(self) -> Path:
        return (self.repo_dir / self.preferences_file).resolve()

    @property
    def history_path(self) -> Path:
        return (self.repo_dir / self.history_file).resolve()

    @property
    def state_path(self) -> Path:
        return (self.repo_dir / self.state_file).resolve()

    @property
    def feed_url(self) -> str:
        base = self.site_base.rstrip("/")
        return f"{base}/{self.feed_file.lstrip('/')}"

    @property
    def cover_url(self) -> str:
        base = self.site_base.rstrip("/")
        return f"{base}/{self.cover_file.lstrip('/')}"

    @property
    def episodes_url_prefix(self) -> str:
        base = self.site_base.rstrip("/")
        rel = self.episodes_dir.strip("/")
        return f"{base}/{rel}" if rel else base


def load_channel_config(repo_dir: Path, channel_path: str | None = None) -> ChannelConfig:
    """加载频道 JSON 配置；若文件不存在则回退到内置默认配置，确保兼容根目录单频道。"""
    cfg_rel = (channel_path or os.environ.get("PODCAST_CHANNEL", "") or DEFAULT_CHANNEL_REL).strip()
    cfg_file = Path(cfg_rel)
    if not cfg_file.is_absolute():
        cfg_file = repo_dir / cfg_file

    data: dict[str, Any] = {}
    if cfg_file.exists():
        data = json.loads(cfg_file.read_text(encoding="utf-8"))

    tts_raw = data.get("tts") if isinstance(data.get("tts"), dict) else {}
    tts = TTSConfig(
        gemini_model=str(tts_raw.get("gemini_model") or TTSConfig.gemini_model),
        gemini_voice_main=str(tts_raw.get("gemini_voice_main") or TTSConfig.gemini_voice_main),
        gemini_voice_cohost=str(tts_raw.get("gemini_voice_cohost") or TTSConfig.gemini_voice_cohost),
        gemini_style=str(tts_raw.get("gemini_style") or TTSConfig.gemini_style),
        gemini_pace_seconds=float(tts_raw.get("gemini_pace_seconds", TTSConfig.gemini_pace_seconds)),
        edge_voice_main=str(tts_raw.get("edge_voice_main") or TTSConfig.edge_voice_main),
        edge_voice_cohost=str(tts_raw.get("edge_voice_cohost") or TTSConfig.edge_voice_cohost),
    )

    storage_raw = data.get("storage") if isinstance(data.get("storage"), dict) else {}
    storage = StorageConfig(
        backend=str(os.environ.get("AUDIO_STORAGE_BACKEND") or storage_raw.get("backend") or "git").strip().lower(),
        public_base_url=str(os.environ.get("R2_PUBLIC_BASE_URL") or storage_raw.get("public_base_url") or "").strip(),
        bucket=str(os.environ.get("R2_BUCKET") or storage_raw.get("bucket") or "").strip(),
        key_prefix=str(storage_raw.get("key_prefix") or "episodes").strip("/"),
    )

    return ChannelConfig(
        repo_dir=repo_dir,
        id=str(data.get("id") or ChannelConfig.id),
        title=str(data.get("title") or ChannelConfig.title),
        author=str(data.get("author") or ChannelConfig.author),
        description=str(data.get("description") or ChannelConfig.description),
        language=str(data.get("language") or ChannelConfig.language),
        site_base=str(data.get("site_base") or ChannelConfig.site_base).rstrip("/"),
        output_dir=str(data.get("output_dir") or "."),
        episodes_dir=str(data.get("episodes_dir") or "episodes"),
        feed_file=str(data.get("feed_file") or "feed.xml"),
        index_file=str(data.get("index_file") or "index.html"),
        cover_file=str(data.get("cover_file") or "cover.jpg"),
        guid_prefix=str(data.get("guid_prefix") or "daily-podcast-"),
        preferences_file=str(data.get("preferences_file") or "preferences.md"),
        history_file=str(data.get("history_file") or "history.md"),
        state_file=str(data.get("state_file") or ".state/built_hashes.json"),
        category=str(data.get("category") or "Technology"),
        explicit=bool(data.get("explicit", False)),
        tts=tts,
        storage=storage,
    )
