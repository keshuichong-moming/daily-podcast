#!/usr/bin/env python3
"""每日开源播客云端化音频合成 + Apple Podcasts RSS / 网页构建脚本。

特性：
1. 路径与工具参数化：REPO_DIR 环境变量 + shutil.which("ffmpeg"/"ffprobe")，全量改用 requests 替换 curl；
2. 环境变量优先：GEMINI_API_KEY / WTL_BASE_URL / WTL_API_KEY / WTL_DAILY_BUDGET_RMB 优先读 os.environ，
   找不到再回退 ~/.config/daily-podcast/；
3. 三级 TTS 降级：
   - 第一级：Google 官方免费 gemini-3.8-flash-tts（22s 节流保护 3 RPM）
   - 第二级：梧桐林网关 gemini-3.8-flash-tts（cost_first 低价策略、不传 instructions、瞬时错误重试 2 次、
             单日累计预算熔断默认 ¥3.0）
   - 第三级：免费无限 edge-tts 终极兜底
4. FFmpeg 统一后期：24kHz mono PCM + 0.45s 段间静音 + loudnorm (-16 LUFS) + 44.1kHz 96kbps CBR MP3；
5. 去重状态落仓库 .state/built_hashes.json，修复未来 pubDate 时间戳、data=list AttributeError 等坑；
6. 支持 channels/ 多频道配置与 storage.py 存储层抽象（默认 Git，预留 Cloudflare R2）；
7. 日志严格脱敏，适用于公开 GitHub 仓库 Actions 日志。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import datetime
import email.utils
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from channel_config import ChannelConfig, load_channel_config, resolve_repo_dir  # noqa: E402
from storage import AudioAsset, AudioStorage, create_audio_storage  # noqa: E402

TZ_BJ = datetime.timezone(datetime.timedelta(hours=8))
LOCAL_KEY_FILE = Path.home() / ".config" / "daily-podcast" / "gemini_api_key"
LOCAL_WTL_ENV_FILE = Path.home() / ".config" / "daily-podcast" / "wutonglin.env"
LEGACY_CACHE_HASH_DB = Path.home() / ".cache" / "daily-podcast" / "built_hashes.json"

USER_AGENT = "daily-podcast-builder/2.0 (+https://github.com/keshuichong-moming/daily-podcast)"


def log(msg: str) -> None:
    ts = datetime.datetime.now(TZ_BJ).strftime("%Y-%m-%d %H:%M:%S+08")
    print(f"[{ts}] {msg}", flush=True)


def find_binary(name: str) -> str:
    """动态定位 ffmpeg / ffprobe，适配 Linux 云端与 macOS 本机。"""
    found = shutil.which(name)
    if found:
        return found
    for candidate in (
        Path.home() / ".local" / "bin" / name,
        Path("/opt/homebrew/bin") / name,
        Path("/usr/local/bin") / name,
        Path("/usr/bin") / name,
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise RuntimeError(f"未找到可执行文件 {name}，请先安装 ffmpeg。")


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def load_gemini_api_key() -> str:
    env_val = os.environ.get("GEMINI_API_KEY", "").strip()
    if env_val:
        return env_val
    if LOCAL_KEY_FILE.exists():
        return LOCAL_KEY_FILE.read_text(encoding="utf-8").strip()
    return ""


def load_wtl_config() -> tuple[str, str, float]:
    """环境变量优先读取梧桐林配置与预算上限，缺失时回退本机配置文件。"""
    file_env: dict[str, str] = {}
    if LOCAL_WTL_ENV_FILE.exists():
        for raw in LOCAL_WTL_ENV_FILE.read_text(encoding="utf-8").splitlines():
            s = raw.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            file_env[k.strip()] = v.strip().strip('"').strip("'")

    base_url = (
        os.environ.get("WTL_BASE_URL")
        or file_env.get("WTL_BASE_URL")
        or "https://hao.token6688.com"
    ).strip().rstrip("/")
    api_key = (
        os.environ.get("WTL_API_KEY")
        or os.environ.get("API_KEY")
        or file_env.get("WTL_API_KEY")
        or file_env.get("API_KEY")
        or ""
    ).strip()

    budget_raw = (
        os.environ.get("WTL_DAILY_BUDGET_RMB")
        or file_env.get("WTL_DAILY_BUDGET_RMB")
        or "3.0"
    ).strip()
    try:
        budget_rmb = max(0.0, float(budget_raw))
    except ValueError:
        budget_rmb = 3.0

    allow_raw = os.environ.get("ALLOW_WTL_FALLBACK", "true").strip().lower()
    if allow_raw in ("0", "false", "no", "off"):
        api_key = ""

    return base_url, api_key, budget_rmb


def load_state_db(state_path: Path) -> dict[str, dict[str, Any]]:
    """加载落仓库的去重状态（兼容旧版 {stem: sha256} 扁平结构）。"""
    raw_obj: dict[str, Any] = {}
    src = state_path if state_path.exists() else (LEGACY_CACHE_HASH_DB if LEGACY_CACHE_HASH_DB.exists() else None)
    if src:
        try:
            loaded = json.loads(src.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw_obj = loaded
        except Exception:
            raw_obj = {}

    normalized: dict[str, dict[str, Any]] = {}
    for stem, val in raw_obj.items():
        if isinstance(val, str):
            normalized[stem] = {"hash": val}
        elif isinstance(val, dict):
            normalized[stem] = dict(val)
    return normalized


def save_state_db(state_path: Path, db: dict[str, dict[str, Any]]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    ordered = {k: db[k] for k in sorted(db.keys())}
    state_path.write_text(json.dumps(ordered, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def clean_spoken_text(raw: str) -> str:
    """清理偶发 Markdown 残留，保留纯口语文本。"""
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\[([^\]]+)\]\(https?://[^)]+\)", r"\1", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"^\s*#{1,6}\s+", "", text, flags=re.M)
    text = re.sub(r"[`*_~]+", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_segments(text: str, max_chars: int = 1200) -> list[tuple[str, str]]:
    """解析文稿为 [(role, chunk_text), ...]，自动兼容双人对谈与单人主播。"""
    speaker_re = re.compile(r"^\s*(男|女|主播A|主播B|嘉宾|主持|A|B)\s*[：:]\s*(.+)$")
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    tagged = [speaker_re.match(l) for l in lines]
    is_dialog = sum(1 for m in tagged if m) >= max(2, len(lines) // 2)

    if is_dialog:
        turns: list[tuple[str, str]] = []
        for l, m in zip(lines, tagged):
            if m:
                sp, content = m.group(1), m.group(2).strip()
                role = "cohost" if sp in ("女", "主播B", "嘉宾", "B") else "main"
                if turns and turns[-1][0] == role and len(turns[-1][1]) + len(content) < max_chars:
                    turns[-1] = (role, turns[-1][1] + "\n" + content)
                else:
                    turns.append((role, content))
            elif turns:
                turns[-1] = (turns[-1][0], turns[-1][1] + "\n" + l)
            else:
                turns.append(("main", l))
        return turns

    chunks: list[str] = []
    buf = ""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    for para in paragraphs:
        if len(para) > max_chars:
            sentences = re.split(r"(?<=[。！？!?])", para)
        else:
            sentences = [para]
        for s in sentences:
            s = s.strip()
            if not s:
                continue
            if len(buf) + len(s) + 1 <= max_chars:
                buf = f"{buf}\n{s}".strip() if buf else s
            else:
                if buf:
                    chunks.append(buf)
                while len(s) > max_chars:
                    chunks.append(s[:max_chars])
                    s = s[max_chars:]
                buf = s
    if buf:
        chunks.append(buf)
    return [("main", c) for c in chunks]


def gemini_tts_chunk(
    session: requests.Session,
    channel: ChannelConfig,
    text: str,
    role: str,
    api_key: str,
    out_wav: Path,
) -> bool:
    """第一级通道：调用 Google 官方免费 Gemini 3.8 Flash TTS（使用 requests，不在命令行暴露密钥）。"""
    voice = channel.tts.gemini_voice_cohost if role == "cohost" else channel.tts.gemini_voice_main
    body = {
        "model": channel.tts.gemini_model,
        "input": [{
            "type": "user_input",
            "content": [{
                "type": "text",
                "text": text,
                "annotations": [{"type": "speech_metadata", "style": channel.tts.gemini_style}],
            }],
        }],
        "response_format": {"type": "audio"},
        "generation_config": {"speech_config": [{"voice": voice}]},
    }

    url = "https://generativelanguage.googleapis.com/v1beta/interactions"
    headers = {
        "x-goog-api-key": api_key,
        "Content-Type": "application/json",
    }

    for attempt in range(1, 4):
        try:
            resp = session.post(url, headers=headers, json=body, timeout=180)
            if resp.status_code != 200:
                log(f"  [gemini-free] HTTP {resp.status_code} (attempt {attempt}/3)")
                if resp.status_code == 429:
                    time.sleep(26)
                    continue
                if resp.status_code in (500, 502, 503, 504):
                    time.sleep(6 * attempt)
                    continue
                return False

            data = resp.json()
            found: list[dict[str, Any]] = []

            def walk(o: Any) -> None:
                if isinstance(o, dict):
                    if "data" in o and isinstance(o["data"], str) and len(o["data"]) > 1000:
                        found.append(o)
                    for v in o.values():
                        walk(v)
                elif isinstance(o, list):
                    for v in o:
                        walk(v)

            walk(data)
            if not found:
                log(f"  [gemini-free] 未返回音频载荷 (attempt {attempt}/3)")
                time.sleep(8)
                continue

            raw_audio = base64.b64decode(found[0]["data"])
            if len(raw_audio) < 1000:
                log(f"  [gemini-free] 音频过短 (attempt {attempt}/3)")
                time.sleep(6)
                continue
            out_wav.write_bytes(raw_audio)
            return True
        except requests.RequestException as e:
            log(f"  [gemini-free] 网络异常 (attempt {attempt}/3): {type(e).__name__}")
            time.sleep(5 * attempt)
        except Exception as e:
            log(f"  [gemini-free] 解析异常 (attempt {attempt}/3): {type(e).__name__}")
            time.sleep(5)
    return False


def _extract_wtl_status_and_final(tr: Any) -> tuple[str, bool]:
    """安全提取任务状态，兼容 data 为 dict / list / None（修复 AttributeError）。"""
    if not isinstance(tr, dict):
        return "", False
    td = tr.get("data")
    status = tr.get("status")
    if not status and isinstance(td, dict):
        status = td.get("status")
    is_final = tr.get("is_final")
    if is_final is None and isinstance(td, dict):
        is_final = td.get("is_final")
    return str(status or "").lower(), bool(is_final is True)


def _extract_wtl_audio_url(tr: Any) -> str:
    """安全提取梧桐林完成后的音频下载 URL，兼容顶层、dict data 与 list data。"""
    if not isinstance(tr, dict):
        return ""
    for k in ("output_url", "result_url", "url", "audio_url"):
        v = tr.get(k)
        if isinstance(v, str) and v.startswith("http"):
            return v
    td = tr.get("data")
    if isinstance(td, dict):
        for k in ("output_url", "result_url", "url", "audio_url"):
            v = td.get(k)
            if isinstance(v, str) and v.startswith("http"):
                return v
    elif isinstance(td, list):
        for item in td:
            if isinstance(item, dict):
                for k in ("url", "output_url", "result_url", "audio_url"):
                    v = item.get(k)
                    if isinstance(v, str) and v.startswith("http"):
                        return v
    return ""


def _extract_wtl_cost_rmb(tr: Any) -> float:
    """安全提取单段实扣人民币费用（支持 actual_cost_credits 与 micro_usd）。"""
    if not isinstance(tr, dict):
        return 0.0
    billing = tr.get("billing") if isinstance(tr.get("billing"), dict) else {}
    credits_val = billing.get("actual_cost_credits")
    if isinstance(credits_val, (int, float)):
        return max(0.0, float(credits_val))
    micro = tr.get("actual_cost_micro_usd")
    if micro is None:
        micro = billing.get("actual_cost_micro_usd")
    if isinstance(micro, (int, float)):
        return max(0.0, float(micro) / 1_000_000.0 * 7.2)
    return 0.0


def wtl_tts_chunk(
    session: requests.Session,
    channel: ChannelConfig,
    text: str,
    role: str,
    base_url: str,
    api_key: str,
    out_wav: Path,
) -> tuple[bool, float]:
    """第二级通道：梧桐林 Gemini 3.8 Flash TTS（cost_first、不传 instructions、瞬时错误重试 2 次）。

    返回: (是否成功, 本段实际扣费人民币)
    """
    if not api_key:
        return False, 0.0

    voice = channel.tts.gemini_voice_cohost if role == "cohost" else channel.tts.gemini_voice_main
    # 切记：梧桐林网关调用 gemini-3.8-flash-tts 时绝不能传 instructions 字段，否则会直接朗读指令文本
    submit_body = {
        "model": channel.tts.gemini_model,
        "prompt": text.strip(),
        "params": {
            "voice": voice,
            "quality": "standard",
            "response_format": "wav",
        },
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
        "X-Schedule-Strategy": "cost_first",
    }

    # 阶段 1：提交任务（遇到瞬时网络抖动或 5xx/429 最多重试 2 次，总共最多 3 次）
    tid = ""
    for attempt in range(1, 4):
        try:
            resp = session.post(f"{base_url}/v1/media/generate", headers=headers, json=submit_body, timeout=60)
            if resp.status_code in (429, 500, 502, 503, 504):
                log(f"  [wtl-gemini] 提交遇到瞬时 HTTP {resp.status_code} (attempt {attempt}/3)")
                if attempt < 3:
                    time.sleep(3 * attempt)
                    continue
                return False, 0.0
            if resp.status_code != 200:
                log(f"  [wtl-gemini] 提交失败 HTTP {resp.status_code}")
                return False, 0.0
            res = resp.json()
            data = res.get("data") if isinstance(res.get("data"), dict) else res
            tid = str((data.get("task_id") if isinstance(data, dict) else "") or res.get("task_id") or "").strip()
            if not tid:
                log("  [wtl-gemini] 响应缺少 task_id")
                if attempt < 3:
                    time.sleep(3)
                    continue
                return False, 0.0
            break
        except requests.RequestException as e:
            log(f"  [wtl-gemini] 提交网络异常 (attempt {attempt}/3): {type(e).__name__}")
            if attempt < 3:
                time.sleep(3 * attempt)
                continue
            return False, 0.0
        except Exception as e:
            log(f"  [wtl-gemini] 提交解析异常: {type(e).__name__}")
            return False, 0.0

    # 阶段 2：轮询任务终态（最长 240s，单次轮询允许网络/5xx 容错重试）
    poll_headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    t0 = time.time()
    audio_url = ""
    cost_rmb = 0.0
    consecutive_poll_err = 0

    while time.time() - t0 < 240:
        time.sleep(2.5)
        try:
            p = session.get(f"{base_url}/v1/tasks/{tid}", headers=poll_headers, timeout=30)
            if p.status_code in (429, 500, 502, 503, 504):
                consecutive_poll_err += 1
                if consecutive_poll_err <= 4:
                    time.sleep(2)
                    continue
                log(f"  [wtl-gemini] 轮询连续遇到 HTTP {p.status_code}")
                return False, cost_rmb
            if p.status_code != 200:
                log(f"  [wtl-gemini] 轮询 HTTP {p.status_code}")
                return False, cost_rmb

            consecutive_poll_err = 0
            tr = p.json()
            status, is_final = _extract_wtl_status_and_final(tr)
            if status in ("completed", "succeeded") or (is_final and status not in ("failed", "cancelled")):
                audio_url = _extract_wtl_audio_url(tr)
                cost_rmb = _extract_wtl_cost_rmb(tr)
                break
            if status in ("failed", "cancelled"):
                cost_rmb = _extract_wtl_cost_rmb(tr)
                log(f"  [wtl-gemini] 任务终态失败 (status={status})")
                return False, cost_rmb
        except requests.RequestException:
            consecutive_poll_err += 1
            if consecutive_poll_err > 5:
                log("  [wtl-gemini] 轮询网络连续中断")
                return False, cost_rmb
            time.sleep(2)
        except Exception:
            consecutive_poll_err += 1
            if consecutive_poll_err > 3:
                log("  [wtl-gemini] 轮询响应解析失败")
                return False, cost_rmb

    if not audio_url:
        log("  [wtl-gemini] 等待超时或未返回音频链接")
        return False, cost_rmb

    # 阶段 3：下载音频（最多 2 次瞬时重试）
    for attempt in range(1, 4):
        try:
            with session.get(audio_url, stream=True, timeout=120) as dl:
                if dl.status_code in (429, 500, 502, 503, 504):
                    if attempt < 3:
                        time.sleep(3 * attempt)
                        continue
                    return False, cost_rmb
                dl.raise_for_status()
                with out_wav.open("wb") as f:
                    for chunk in dl.iter_content(chunk_size=65536):
                        if chunk:
                            f.write(chunk)
            if out_wav.exists() and out_wav.stat().st_size >= 1000:
                return True, cost_rmb
            log(f"  [wtl-gemini] 音频文件过小 (attempt {attempt}/3)")
        except requests.RequestException as e:
            log(f"  [wtl-gemini] 下载网络异常 (attempt {attempt}/3): {type(e).__name__}")
            if attempt < 3:
                time.sleep(3 * attempt)
                continue
    return False, cost_rmb


async def _edge_tts_save(text: str, voice: str, out_mp3: Path) -> None:
    import edge_tts

    comm = edge_tts.Communicate(text, voice=voice)
    await comm.save(str(out_mp3))


def edge_tts_chunk(
    channel: ChannelConfig,
    ffmpeg_bin: str,
    text: str,
    role: str,
    out_wav: Path,
) -> bool:
    """第三级通道：免费无限 Edge-TTS 兜底，合成后转为统一 WAV。"""
    voice = channel.tts.edge_voice_cohost if role == "cohost" else channel.tts.edge_voice_main
    tmp_mp3 = out_wav.with_suffix(".edge.mp3")
    for attempt in range(1, 4):
        try:
            asyncio.run(_edge_tts_save(text, voice, tmp_mp3))
            subprocess.run(
                [ffmpeg_bin, "-y", "-i", str(tmp_mp3), "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", str(out_wav)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if out_wav.exists() and out_wav.stat().st_size >= 1000:
                return True
        except Exception as e:
            log(f"  [edge-tts] 合成异常 (attempt {attempt}/3): {type(e).__name__}")
            time.sleep(3 * attempt)
        finally:
            tmp_mp3.unlink(missing_ok=True)
    return False


def probe_duration_seconds(ffprobe_bin: str, mp3_path: Path) -> int:
    try:
        out = subprocess.check_output(
            [
                ffprobe_bin,
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(mp3_path),
            ],
            text=True,
        ).strip()
        return max(1, int(round(float(out))))
    except Exception:
        return 60


def synthesize_episode(
    session: requests.Session,
    channel: ChannelConfig,
    storage: AudioStorage,
    ffmpeg_bin: str,
    ffprobe_bin: str,
    script_path: Path,
    stem: str,
    api_key: str,
    wtl_base: str,
    wtl_key: str,
    wtl_budget_state: dict[str, Any],
) -> AudioAsset | None:
    raw = script_path.read_text(encoding="utf-8")
    cleaned = clean_spoken_text(raw)
    if not cleaned:
        log(f"跳过空口播稿: {script_path.name}")
        return None

    segments = parse_segments(cleaned)
    t_start = time.time()
    log(f"开始合成 {stem}（共 {len(segments)} 段，总字数 {len(cleaned)}）")

    with tempfile.TemporaryDirectory(prefix="podcast_tts_") as td:
        tmp_dir = Path(td)
        pause_wav = tmp_dir / "pause.wav"
        subprocess.run(
            [ffmpeg_bin, "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-t", "0.45", "-c:a", "pcm_s16le", str(pause_wav)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        wav_files: list[Path] = []
        free_gemini_exhausted = not bool(api_key)
        wtl_exhausted = not bool(wtl_key)
        last_gemini_call = 0.0
        channel_counts: dict[str, int] = {"gemini-free": 0, "wtl-gemini": 0, "edge-tts": 0}

        for idx, (role, chunk_text) in enumerate(segments):
            raw_wav = tmp_dir / f"chunk_{idx:03d}_raw.wav"
            norm_wav = tmp_dir / f"chunk_{idx:03d}.wav"
            ok = False
            seg_t0 = time.time()

            # 第一级：Google 官方免费 Gemini 3.8 Flash TTS（间隔 22s 保护 3 RPM）
            if not free_gemini_exhausted:
                if last_gemini_call > 0:
                    elapsed = time.time() - last_gemini_call
                    if elapsed < channel.tts.gemini_pace_seconds:
                        time.sleep(channel.tts.gemini_pace_seconds - elapsed)
                last_gemini_call = time.time()
                ok = gemini_tts_chunk(session, channel, chunk_text, role, api_key, raw_wav)
                if ok:
                    channel_counts["gemini-free"] += 1
                    log(f"  [{idx+1}/{len(segments)}] 通道=gemini-free 成功 ({len(chunk_text)} 字, {time.time()-seg_t0:.1f}s)")
                else:
                    log(f"  [{idx+1}/{len(segments)}] gemini-free 不可用，切换下一通道")
                    free_gemini_exhausted = True

            # 第二级：梧桐林同款 Gemini 3.8 Flash TTS（含单日预算熔断）
            if not ok and not wtl_exhausted:
                if wtl_budget_state["spent_rmb"] >= wtl_budget_state["limit_rmb"]:
                    if not wtl_budget_state["tripped"]:
                        wtl_budget_state["tripped"] = True
                        log("  触发梧桐林单日预算护栏，自动熔断并切换 edge-tts")
                    wtl_exhausted = True
                else:
                    seg_wtl_t0 = time.time()
                    ok, seg_cost = wtl_tts_chunk(session, channel, chunk_text, role, wtl_base, wtl_key, raw_wav)
                    wtl_budget_state["spent_rmb"] += seg_cost
                    if wtl_budget_state["spent_rmb"] >= wtl_budget_state["limit_rmb"]:
                        wtl_budget_state["tripped"] = True
                    if ok:
                        channel_counts["wtl-gemini"] += 1
                        log(f"  [{idx+1}/{len(segments)}] 通道=wtl-gemini 成功 ({len(chunk_text)} 字, {time.time()-seg_wtl_t0:.1f}s)")
                    else:
                        log(f"  [{idx+1}/{len(segments)}] wtl-gemini 不可用，切换 edge-tts 兜底")
                        wtl_exhausted = True

            # 第三级：免费无限 Edge-TTS 终极兜底
            if not ok:
                seg_edge_t0 = time.time()
                ok = edge_tts_chunk(channel, ffmpeg_bin, chunk_text, role, raw_wav)
                if not ok:
                    log(f"  [{idx+1}/{len(segments)}] 所有语音通道均失败，中止本期合成")
                    return None
                channel_counts["edge-tts"] += 1
                log(f"  [{idx+1}/{len(segments)}] 通道=edge-tts 兜底成功 ({len(chunk_text)} 字, {time.time()-seg_edge_t0:.1f}s)")

            # 统一采样率 24kHz mono 16-bit PCM
            subprocess.run(
                [ffmpeg_bin, "-y", "-i", str(raw_wav), "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", str(norm_wav)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            wav_files.append(norm_wav)

        concat_list = tmp_dir / "concat.txt"
        concat_lines: list[str] = []
        for i, wf in enumerate(wav_files):
            concat_lines.append(f"file '{wf}'")
            if i < len(wav_files) - 1:
                concat_lines.append(f"file '{pause_wav}'")
        concat_list.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")

        tmp_out = tmp_dir / f"{stem}.mp3"
        # 播客响度标准 -16 LUFS，CBR 96kbps 44.1kHz mono（苹果播客进度条最稳）
        subprocess.run(
            [
                ffmpeg_bin, "-y",
                "-f", "concat", "-safe", "0", "-i", str(concat_list),
                "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
                "-ar", "44100", "-ac", "1", "-c:a", "libmp3lame", "-b:a", "96k",
                str(tmp_out),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        duration_sec = probe_duration_seconds(ffprobe_bin, tmp_out)
        asset = storage.publish(tmp_out, stem=stem, duration_sec=duration_sec)
        total_elapsed = time.time() - t_start
        used_summary = ", ".join(f"{k}:{v}" for k, v in channel_counts.items() if v > 0)
        # 日志脱敏：仅输出通道分布、耗时和是否触发预算护栏，绝不打印余额或密钥
        log(
            f"合成完成: {asset.filename} ({asset.size / 1024:.1f} KB, {format_hhmmss(duration_sec)}) | "
            f"通道=[{used_summary}] | 总耗时={total_elapsed:.1f}s | "
            f"预算护栏触发={wtl_budget_state['tripped']}"
        )
        return asset


def format_hhmmss(secs: int) -> str:
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def resolve_pub_datetime(
    stem: str,
    asset: AudioAsset,
    state_db: dict[str, dict[str, Any]],
    now_bj: datetime.datetime,
) -> datetime.datetime:
    """确定节目发布时间（绝不产生未来时间戳，且对历史期次保持稳定）。"""
    rec = state_db.get(stem) or {}
    saved_iso = rec.get("pub_date")
    if isinstance(saved_iso, str) and saved_iso.strip():
        try:
            dt = datetime.datetime.fromisoformat(saved_iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=TZ_BJ)
            dt = dt.astimezone(TZ_BJ)
            return min(dt, now_bj)
        except ValueError:
            pass

    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", stem)
    if m:
        ep_date = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if ep_date == now_bj.date():
            # 当天现跑现发：使用实际生成时间（例如 06:25+08），避免写死 08:00 产生未来时间戳
            dt = now_bj.replace(microsecond=0)
        elif ep_date < now_bj.date():
            # 历史补录期次：取该日期 08:00+08 与当前时间中较早者
            dt = min(
                datetime.datetime(ep_date.year, ep_date.month, ep_date.day, 8, 0, 0, tzinfo=TZ_BJ),
                now_bj.replace(microsecond=0),
            )
        else:
            # 未来日期文件名兜底：压回当前时间，防止 Apple Podcasts 延迟抓取
            dt = now_bj.replace(microsecond=0)
    elif asset.local_path and asset.local_path.exists():
        dt = min(
            datetime.datetime.fromtimestamp(asset.local_path.stat().st_mtime, tz=TZ_BJ).replace(microsecond=0),
            now_bj.replace(microsecond=0),
        )
    else:
        dt = now_bj.replace(microsecond=0)

    state_db.setdefault(stem, {})["pub_date"] = dt.isoformat()
    return dt


def extract_episode_meta(
    channel: ChannelConfig,
    ffprobe_bin: str,
    asset: AudioAsset,
    state_db: dict[str, dict[str, Any]],
    now_bj: datetime.datetime,
) -> dict[str, Any]:
    """从同日 notes.md 提取标题和简介，并计算不超前的合规 pubDate。"""
    stem = asset.stem
    notes_path = channel.episodes_path / f"{stem}-notes.md"
    title = f"{channel.title} {stem}"
    summary = f"{stem} 期每日科技热点与 GitHub 飙升项目拆解。"
    notes_md = ""

    if notes_path.exists():
        notes_md = notes_path.read_text(encoding="utf-8").strip()
        for line in notes_md.splitlines():
            s = line.strip()
            if s.startswith("# "):
                t = s[2:].strip()
                if t:
                    title = t
                    break
        summary = notes_md[:3800]

    pub_dt = resolve_pub_datetime(stem, asset, state_db, now_bj)
    duration_sec = asset.duration_sec
    if not duration_sec:
        saved_dur = (state_db.get(stem) or {}).get("duration_sec")
        if isinstance(saved_dur, int) and saved_dur > 0:
            duration_sec = saved_dur
        elif asset.local_path and asset.local_path.exists():
            duration_sec = probe_duration_seconds(ffprobe_bin, asset.local_path)
            state_db.setdefault(stem, {})["duration_sec"] = duration_sec
        else:
            duration_sec = 60

    return {
        "stem": stem,
        "title": title,
        "summary": summary,
        "notes_md": notes_md,
        "pub_dt": pub_dt,
        "pub_rfc2822": email.utils.format_datetime(pub_dt),
        "size": asset.size,
        "duration_sec": duration_sec,
        "url": asset.url,
    }


def build_feed_and_index(
    channel: ChannelConfig,
    storage: AudioStorage,
    ffprobe_bin: str,
    state_db: dict[str, dict[str, Any]],
) -> bool:
    """根据当前存储层的全部 MP3 重新生成 feed.xml 和 index.html，返回是否有变动。"""
    now_bj = datetime.datetime.now(TZ_BJ).replace(microsecond=0)
    assets = storage.list_assets()
    episodes = [extract_episode_meta(channel, ffprobe_bin, asset, state_db, now_bj) for asset in assets]
    episodes.sort(key=lambda e: (e["pub_dt"], e["stem"]), reverse=True)

    last_build_dt = episodes[0]["pub_dt"] if episodes else now_bj
    last_build = email.utils.format_datetime(last_build_dt)
    explicit_str = "true" if channel.explicit else "false"

    items_xml: list[str] = []
    for ep in episodes:
        desc_esc = html.escape(ep["summary"])
        title_esc = html.escape(ep["title"])
        dur_str = format_hhmmss(ep["duration_sec"])
        guid = f"{channel.guid_prefix}{ep['stem']}"
        items_xml.append(f"""    <item>
      <title>{title_esc}</title>
      <description>{desc_esc}</description>
      <itunes:summary>{desc_esc}</itunes:summary>
      <pubDate>{ep['pub_rfc2822']}</pubDate>
      <guid isPermaLink="false">{html.escape(guid)}</guid>
      <enclosure url="{ep['url']}" length="{ep['size']}" type="audio/mpeg" />
      <itunes:duration>{dur_str}</itunes:duration>
      <itunes:explicit>{explicit_str}</itunes:explicit>
      <itunes:episodeType>full</itunes:episodeType>
    </item>""")

    feed_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
     xmlns:atom="http://www.w3.org/2005/Atom"
     xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>{html.escape(channel.title)}</title>
    <link>{channel.site_base}/</link>
    <atom:link href="{channel.feed_url}" rel="self" type="application/rss+xml" />
    <language>{html.escape(channel.language)}</language>
    <description>{html.escape(channel.description)}</description>
    <lastBuildDate>{last_build}</lastBuildDate>
    <itunes:author>{html.escape(channel.author)}</itunes:author>
    <itunes:summary>{html.escape(channel.description)}</itunes:summary>
    <itunes:type>episodic</itunes:type>
    <itunes:explicit>{explicit_str}</itunes:explicit>
    <itunes:image href="{channel.cover_url}" />
    <itunes:category text="{html.escape(channel.category)}" />
{chr(10).join(items_xml)}
  </channel>
</rss>
"""
    # 校验 XML 合法性
    ET.fromstring(feed_xml.encode("utf-8"))

    cards: list[str] = []
    for ep in episodes:
        dur_min = max(1, round(ep["duration_sec"] / 60))
        notes_html = html.escape(ep["summary"][:450]).replace("\n", "<br>")
        cards.append(f"""    <article class="card">
      <div class="meta">{html.escape(ep['stem'])} · 约 {dur_min} 分钟</div>
      <h2>{html.escape(ep['title'])}</h2>
      <audio controls preload="none" src="{ep['url']}"></audio>
      <p class="notes">{notes_html}</p>
    </article>""")
    if not cards:
        cards.append('    <p class="empty">暂无节目，云端流水线生成第一期播客稿后将自动合成音频并上架。</p>')

    index_html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{html.escape(channel.title)}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", sans-serif; max-width: 760px; margin: 0 auto; padding: 28px 18px 60px; background: #0f172a; color: #e2e8f0; line-height: 1.65; }}
    header {{ display: flex; gap: 18px; align-items: center; margin-bottom: 24px; }}
    header img {{ width: 96px; height: 96px; border-radius: 16px; }}
    h1 {{ margin: 0 0 6px; font-size: 24px; }}
    .sub {{ margin: 0; color: #94a3b8; font-size: 14px; }}
    .rss {{ background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 14px 16px; margin-bottom: 24px; font-size: 14px; }}
    .rss code {{ display: block; margin-top: 6px; padding: 8px 10px; background: #0b1220; border-radius: 8px; color: #7dd3fc; word-break: break-all; }}
    .card {{ background: #1e293b; border: 1px solid #334155; border-radius: 14px; padding: 18px; margin-bottom: 16px; }}
    .meta {{ color: #7dd3fc; font-size: 13px; margin-bottom: 4px; }}
    .card h2 {{ margin: 0 0 12px; font-size: 18px; }}
    audio {{ width: 100%; margin-bottom: 10px; }}
    .notes {{ color: #cbd5e1; font-size: 14px; margin: 0; }}
    .empty {{ color: #94a3b8; }}
  </style>
</head>
<body>
  <header>
    <img src="{html.escape(channel.cover_file)}" alt="{html.escape(channel.title)}" />
    <div>
      <h1>{html.escape(channel.title)}</h1>
      <p class="sub">{html.escape(channel.description)}</p>
    </div>
  </header>
  <section class="rss">
    <strong>苹果播客（Apple Podcasts）订阅地址（RSS）：</strong>
    <code>{channel.feed_url}</code>
  </section>
  <main>
{chr(10).join(cards)}
  </main>
</body>
</html>
"""

    channel.output_path.mkdir(parents=True, exist_ok=True)
    changed = False
    if not channel.feed_path.exists() or channel.feed_path.read_text(encoding="utf-8") != feed_xml:
        channel.feed_path.write_text(feed_xml, encoding="utf-8")
        changed = True
    if not channel.index_path.exists() or channel.index_path.read_text(encoding="utf-8") != index_html:
        channel.index_path.write_text(index_html, encoding="utf-8")
        changed = True
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description="每日开源播客云端音频合成与 RSS 构建脚本")
    parser.add_argument("--repo-dir", default="", help="仓库根目录（优先使用 REPO_DIR）")
    parser.add_argument("--channel", default="", help="频道配置路径（默认 channels/daily-open-source.json）")
    parser.add_argument("--force", action="store_true", help="忽略哈希缓存强制重合成")
    parser.add_argument("--only-stem", default="", help="仅合成指定期次（如 2026-11-26）")
    parser.add_argument("--skip-tts", action="store_true", help="跳过音频合成，仅重建 feed.xml 与 index.html")
    args = parser.parse_args()

    repo_dir = resolve_repo_dir(args.repo_dir)
    channel = load_channel_config(repo_dir, args.channel or None)
    storage = create_audio_storage(channel)

    ffmpeg_bin = find_binary("ffmpeg")
    ffprobe_bin = find_binary("ffprobe")

    api_key = load_gemini_api_key()
    wtl_base, wtl_key, wtl_budget_rmb = load_wtl_config()
    wtl_budget_state: dict[str, Any] = {
        "spent_rmb": 0.0,
        "limit_rmb": wtl_budget_rmb,
        "tripped": False,
    }

    state_db = load_state_db(channel.state_path)
    state_before = json.dumps(state_db, ensure_ascii=False, sort_keys=True)
    session = make_session()
    any_built = False

    if not args.skip_tts and channel.episodes_path.exists():
        for script_path in sorted(channel.episodes_path.glob("*-script.txt")):
            stem = script_path.name[: -len("-script.txt")]
            if args.only_stem and stem != args.only_stem:
                continue
            cur_hash = sha256_file(script_path)
            prev_hash = (state_db.get(stem) or {}).get("hash")
            if not args.force and storage.exists(stem) and prev_hash == cur_hash:
                continue

            asset = synthesize_episode(
                session=session,
                channel=channel,
                storage=storage,
                ffmpeg_bin=ffmpeg_bin,
                ffprobe_bin=ffprobe_bin,
                script_path=script_path,
                stem=stem,
                api_key=api_key,
                wtl_base=wtl_base,
                wtl_key=wtl_key,
                wtl_budget_state=wtl_budget_state,
            )
            if asset is None:
                return 1

            now_bj = datetime.datetime.now(TZ_BJ).replace(microsecond=0)
            entry = state_db.setdefault(stem, {})
            entry["hash"] = cur_hash
            entry["duration_sec"] = asset.duration_sec
            # 若本期之前没有 pub_date，或本次强制重做当天新期次，记录不超前的真实发布时间
            if "pub_date" not in entry or args.force:
                entry.pop("pub_date", None)
                resolve_pub_datetime(stem, asset, state_db, now_bj)
            save_state_db(channel.state_path, state_db)
            any_built = True

    feed_changed = build_feed_and_index(channel, storage, ffprobe_bin, state_db)
    state_after = json.dumps(state_db, ensure_ascii=False, sort_keys=True)
    if state_after != state_before or not channel.state_path.exists():
        save_state_db(channel.state_path, state_db)

    log(
        f"播客构建完成 (channel={channel.id}, any_built={any_built}, "
        f"feed_changed={feed_changed}, budget_guard_triggered={wtl_budget_state['tripped']})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
