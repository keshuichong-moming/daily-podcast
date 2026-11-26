#!/usr/bin/env python3
"""每日开源播客云端采集与写稿脚本。

功能：
1. 读取频道配置、preferences.md（口味档案）与 history.md（跳过近 14 天已播项目）；
2. 联网采集过去 24 小时的科技/AI/开发者热点（Hacker News + 精选技术 RSS）与 GitHub 飙升/热门开源项目；
3. 自动拉取候选开源项目的 README.md 原文，基于真实文档核对原理、适用场景与局限，减少幻觉；
4. 优先调用 Google Gemini API（支持 Google Search Grounding），失败自动回退梧桐林网关（cost_first + 流式读取）；
5. 输出北京时间命名的 episodes/YYYY-MM-DD-script.txt（纯口语中文 2500–3500 字）和
   episodes/YYYY-MM-DD-notes.md（首行 # 标题 + 真实项目链接），并更新 history.md；
6. 默认幂等：当天 script.txt 已存在则跳过，传 --force 强制重跑。
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import datetime
import email.utils
import html
import json
import os
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import requests

# 允许从仓库根目录或 scripts/ 目录直接运行
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from channel_config import ChannelConfig, load_channel_config, resolve_repo_dir  # noqa: E402

TZ_BJ = datetime.timezone(datetime.timedelta(hours=8))
LOCAL_GEMINI_KEY_FILE = Path.home() / ".config" / "daily-podcast" / "gemini_api_key"
LOCAL_WTL_ENV_FILE = Path.home() / ".config" / "daily-podcast" / "wutonglin.env"

USER_AGENT = "daily-podcast-bot/2.0 (+https://github.com/keshuichong-moming/daily-podcast)"

RSS_SOURCES = [
    ("Simon Willison", "https://simonwillison.net/atom/everything/"),
    ("Lobsters", "https://lobste.rs/rss"),
    ("Hugging Face Blog", "https://huggingface.co/blog/feed.xml"),
    ("GitHub Blog", "https://github.blog/feed/"),
    ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("Cloudflare Blog", "https://blog.cloudflare.com/rss/"),
]

# 口味档案过滤：明显偏向纯合集、面试题、纯 UI 模板的关键词
REPO_SKIP_PATTERNS = re.compile(
    r"(awesome-|interview|leetcode|roadmap|30-days|100-days|free-programming-books|"
    r"public-apis|system-design-primer|coding-interview|cheat-sheet|cheatsheet|"
    r"crack-interview|bagu|mianshi|resume-template|portfolio-template)",
    re.IGNORECASE,
)

# 重点偏好加权关键词
REPO_BOOST_PATTERNS = re.compile(
    r"(agent|mcp|llm|local|ollama|vllm|llama|cli|terminal|self-host|selfhost|"
    r"workflow|automation|coding|browser|sandbox|rag|voice|tts|whisper|devtools|"
    r"rust|go|sqlite|docker|memory|eval|runtime|sdk|proxy)",
    re.IGNORECASE,
)


def log(msg: str) -> None:
    ts = datetime.datetime.now(TZ_BJ).strftime("%Y-%m-%d %H:%M:%S+08")
    print(f"[{ts}] {msg}", flush=True)


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def load_gemini_api_key() -> str:
    env_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if env_key:
        return env_key
    if LOCAL_GEMINI_KEY_FILE.exists():
        return LOCAL_GEMINI_KEY_FILE.read_text(encoding="utf-8").strip()
    return ""


def load_wtl_config() -> tuple[str, str]:
    env: dict[str, str] = {}
    if LOCAL_WTL_ENV_FILE.exists():
        for raw in LOCAL_WTL_ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    base_url = (
        os.environ.get("WTL_BASE_URL")
        or env.get("WTL_BASE_URL")
        or "https://hao.token6688.com"
    ).strip().rstrip("/")
    api_key = (
        os.environ.get("WTL_API_KEY")
        or os.environ.get("API_KEY")
        or env.get("WTL_API_KEY")
        or env.get("API_KEY")
        or ""
    ).strip()
    return base_url, api_key


def is_wtl_fallback_allowed() -> bool:
    raw = os.environ.get("ALLOW_WTL_FALLBACK", "true").strip().lower()
    return raw not in ("0", "false", "no", "off")


def parse_recent_history(history_path: Path, today_bj: datetime.date, days: int = 14) -> tuple[set[str], list[str]]:
    """解析 history.md 中最近 N 天已播的 GitHub 仓库（owner/repo）和条目摘要。"""
    if not history_path.exists():
        return set(), []

    cutoff = today_bj - datetime.timedelta(days=days)
    recent_repos: set[str] = set()
    recent_lines: list[str] = []
    current_date: datetime.date | None = None

    repo_url_re = re.compile(r"github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", re.IGNORECASE)
    repo_slug_re = re.compile(r"(?<![A-Za-z0-9_.-/])([A-Za-z0-9][A-Za-z0-9_.-]{1,38}/[A-Za-z0-9_.-]{1,100})(?![A-Za-z0-9_.-/])")
    date_re = re.compile(r"(20\d{2}-\d{2}-\d{2})")

    for raw in history_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("# 已播"):
            continue
        dm = date_re.search(line)
        if dm:
            try:
                current_date = datetime.date.fromisoformat(dm.group(1))
            except ValueError:
                pass
        if line.startswith("## "):
            continue

        # 若没有显式日期标题，也保守视为近期已播避免重复
        in_window = current_date is None or current_date >= cutoff
        if not in_window:
            continue

        recent_lines.append(line)
        for m in repo_url_re.finditer(line):
            slug = m.group(1).rstrip(".,;)").lower()
            if slug.endswith(".git"):
                slug = slug[:-4]
            recent_repos.add(slug)
        for m in repo_slug_re.finditer(line):
            slug = m.group(1).lower()
            # 排除类似 YYYY/MM 或 http/https 误匹配
            if not slug.startswith(("http/", "https/", "www.")):
                recent_repos.add(slug)

    return recent_repos, recent_lines[:80]


def fetch_hn_stories(session: requests.Session, limit: int = 12) -> list[dict[str, Any]]:
    """抓取 Hacker News 近 36 小时内高热科技/AI/开发新闻。"""
    now_ts = time.time()
    cutoff_ts = now_ts - 36 * 3600
    try:
        r = session.get("https://hacker-news.firebaseio.com/v0/topstories.json", timeout=15)
        r.raise_for_status()
        ids = r.json()[:50]
    except Exception as e:
        log(f"HN 列表抓取失败（已忽略继续）: {type(e).__name__}")
        return []

    def _get_item(item_id: int) -> dict[str, Any] | None:
        try:
            resp = session.get(f"https://hacker-news.firebaseio.com/v0/item/{item_id}.json", timeout=10)
            if resp.status_code != 200:
                return None
            obj = resp.json()
            if not isinstance(obj, dict) or obj.get("type") != "story":
                return None
            if obj.get("dead") or obj.get("deleted"):
                return None
            created = int(obj.get("time") or 0)
            score = int(obj.get("score") or 0)
            title = str(obj.get("title") or "").strip()
            url = str(obj.get("url") or f"https://news.ycombinator.com/item?id={item_id}").strip()
            if not title or created < cutoff_ts or score < 50:
                return None
            # 过滤纯招聘贴
            if title.lower().startswith("ask hn: who is hiring") or "is hiring" in title.lower():
                return None
            boost = 1.35 if REPO_BOOST_PATTERNS.search(title) else 1.0
            return {
                "source": "Hacker News",
                "title": title,
                "url": url,
                "hn_url": f"https://news.ycombinator.com/item?id={item_id}",
                "score": score,
                "comments": int(obj.get("descendants") or 0),
                "published_utc": datetime.datetime.fromtimestamp(created, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                "rank_score": score * boost,
            }
        except Exception:
            return None

    items: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        for res in pool.map(_get_item, ids):
            if res:
                items.append(res)

    items.sort(key=lambda x: x["rank_score"], reverse=True)
    return items[:limit]


def _parse_pub_time(raw: str) -> datetime.datetime | None:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)
    except Exception:
        pass
    try:
        iso = s.replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)
    except Exception:
        return None


def _strip_html(text: str) -> str:
    clean = re.sub(r"<[^>]+>", " ", text or "")
    clean = html.unescape(clean)
    return re.sub(r"\s+", " ", clean).strip()


def fetch_rss_items(session: requests.Session, limit: int = 10) -> list[dict[str, Any]]:
    """抓取精选技术博客与社区近 48 小时 RSS/Atom 条目。"""
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=48)

    def _fetch_one(src: tuple[str, str]) -> list[dict[str, Any]]:
        name, url = src
        out: list[dict[str, Any]] = []
        try:
            resp = session.get(url, timeout=12)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
        except Exception:
            return []

        # 兼容 RSS <item> 与 Atom <entry>
        entries = root.findall(".//item")
        is_atom = False
        if not entries:
            entries = root.findall(".//{http://www.w3.org/2005/Atom}entry")
            is_atom = True

        for entry in entries[:15]:
            if is_atom:
                ns = "{http://www.w3.org/2005/Atom}"
                title = (entry.findtext(f"{ns}title") or "").strip()
                link = ""
                for lk in entry.findall(f"{ns}link"):
                    rel = lk.attrib.get("rel", "alternate")
                    href = lk.attrib.get("href", "")
                    if href and rel in ("alternate", ""):
                        link = href
                        break
                pub_raw = entry.findtext(f"{ns}published") or entry.findtext(f"{ns}updated") or ""
                summary = entry.findtext(f"{ns}summary") or entry.findtext(f"{ns}content") or ""
            else:
                title = (entry.findtext("title") or "").strip()
                link = (entry.findtext("link") or "").strip()
                pub_raw = entry.findtext("pubDate") or entry.findtext("{http://purl.org/dc/elements/1.1/}date") or ""
                summary = entry.findtext("description") or ""

            if not title or not link:
                continue
            dt = _parse_pub_time(pub_raw)
            if dt and dt < cutoff:
                continue
            out.append(
                {
                    "source": name,
                    "title": _strip_html(title),
                    "url": link.strip(),
                    "summary": _strip_html(summary)[:260],
                    "published_utc": dt.strftime("%Y-%m-%d %H:%M UTC") if dt else "recent",
                    "_ts": dt.timestamp() if dt else time.time() - 3600 * 24,
                }
            )
            if len(out) >= 3:
                break
        return out

    all_items: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        for batch in pool.map(_fetch_one, RSS_SOURCES):
            all_items.extend(batch)

    all_items.sort(key=lambda x: x.get("_ts", 0), reverse=True)
    for it in all_items:
        it.pop("_ts", None)
    return all_items[:limit]


def _github_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
    }
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_github_trending_slugs(session: requests.Session) -> list[str]:
    """从 GitHub Trending (daily) 页面抓取今日热门仓库 owner/repo 列表。"""
    slugs: list[str] = []
    try:
        resp = session.get("https://github.com/trending?since=daily", timeout=15)
        if resp.status_code != 200:
            return []
        # 匹配每个仓库卡片里的主链接 <h2 class="h3 lh-condensed"><a href="/owner/repo">
        blocks = re.findall(r'<article[^>]*class="[^"]*Box-row[^"]*"[^>]*>(.*?)</article>', resp.text, flags=re.S)
        for blk in blocks:
            m = re.search(r'<h2[^>]*>.*?<a[^>]*href="/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"', blk, flags=re.S)
            if m:
                slug = m.group(1).strip()
                if slug not in slugs:
                    slugs.append(slug)
    except Exception:
        return []
    return slugs[:18]


def _fetch_repo_detail(session: requests.Session, slug: str, gh_headers: dict[str, str]) -> dict[str, Any] | None:
    try:
        r = session.get(f"https://api.github.com/repos/{slug}", headers=gh_headers, timeout=12)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def _clean_readme(text: str, max_chars: int = 4200) -> str:
    """清理 README 中的徽章图、纯 HTML 布局，保留最核心的原理、用法和限制说明。"""
    # 移除 HTML 注释和纯图片/徽章行
    cleaned = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    cleaned = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", cleaned)
    cleaned = re.sub(r"\[!\[[^\]]*\]\([^)]+\)\]\([^)]+\)", "", cleaned)
    cleaned = re.sub(r"<img[^>]*>", "", cleaned, flags=re.I)
    cleaned = re.sub(r"<(picture|source|div|p|span|a|br|hr|table|tr|td|th|thead|tbody|align)[^>]*>", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"</(picture|source|div|p|span|a|table|tr|td|th|thead|tbody)>", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned.strip()[:max_chars]


def _fetch_repo_readme(session: requests.Session, slug: str, default_branch: str, gh_headers: dict[str, str]) -> str:
    """拉取仓库 README.md 原文用于核实真实技术细节。"""
    try:
        r = session.get(f"https://api.github.com/repos/{slug}/readme", headers=gh_headers, timeout=15)
        if r.status_code == 200:
            obj = r.json()
            content = obj.get("content", "")
            encoding = obj.get("encoding", "")
            if content and encoding == "base64":
                decoded = base64.b64decode(content).decode("utf-8", errors="replace")
                return _clean_readme(decoded)
    except Exception:
        pass

    for fname in ("README.md", "readme.md", "README.MD"):
        try:
            raw_url = f"https://raw.githubusercontent.com/{slug}/{default_branch}/{fname}"
            r = session.get(raw_url, timeout=12)
            if r.status_code == 200 and len(r.text) > 100:
                return _clean_readme(r.text)
        except Exception:
            continue
    return ""


def fetch_github_projects(
    session: requests.Session,
    today_bj: datetime.date,
    recent_repos: set[str],
    top_n: int = 6,
) -> list[dict[str, Any]]:
    """抓取 GitHub 近期飙升与热门开源项目，并附带 README.md 原文。"""
    gh_headers = _github_headers()
    candidates: dict[str, dict[str, Any]] = {}

    def _register(repo: dict[str, Any], origin_tag: str, bonus: float = 0.0) -> None:
        if not isinstance(repo, dict):
            return
        full_name = str(repo.get("full_name") or "").strip()
        if not full_name or "/" not in full_name:
            return
        if repo.get("fork") or repo.get("archived") or repo.get("disabled"):
            return
        slug_l = full_name.lower()
        desc = str(repo.get("description") or "").strip()
        topics = repo.get("topics") if isinstance(repo.get("topics"), list) else []
        text_for_match = f"{full_name} {desc} {' '.join(topics)}"
        if REPO_SKIP_PATTERNS.search(text_for_match):
            return

        stars = int(repo.get("stargazers_count") or 0)
        if stars < 60:
            return

        # 跳过近 14 天已播项目（除非非常新且高热，稍后依然由模型看 history 判断是否有大版本）
        already_covered = slug_l in recent_repos
        if already_covered:
            return

        boost = 2.0 if REPO_BOOST_PATTERNS.search(text_for_match) else 1.0
        # 优先近期新星与高势头项目
        created_str = str(repo.get("created_at") or "")
        age_days = 365
        if created_str:
            dt = _parse_pub_time(created_str)
            if dt:
                age_days = max(1, (datetime.datetime.now(datetime.timezone.utc) - dt).days)
        recency_factor = 2.5 if age_days <= 14 else (1.5 if age_days <= 60 else 1.0)
        score = (min(stars, 20000) ** 0.5) * boost * recency_factor + bonus

        existing = candidates.get(slug_l)
        if existing:
            existing["origins"].add(origin_tag)
            existing["rank_score"] = max(existing["rank_score"], score) + 15.0
            return

        lic = repo.get("license")
        license_name = lic.get("spdx_id") if isinstance(lic, dict) else ""
        candidates[slug_l] = {
            "full_name": full_name,
            "html_url": str(repo.get("html_url") or f"https://github.com/{full_name}"),
            "description": desc,
            "stars": stars,
            "forks": int(repo.get("forks_count") or 0),
            "language": str(repo.get("language") or "Unknown"),
            "license": str(license_name or "Unknown"),
            "topics": topics[:8],
            "created_at": created_str[:10],
            "pushed_at": str(repo.get("pushed_at") or "")[:10],
            "default_branch": str(repo.get("default_branch") or "main"),
            "origins": {origin_tag},
            "rank_score": score,
        }

    # 1) GitHub Daily Trending
    trending_slugs = fetch_github_trending_slugs(session)
    if trending_slugs:
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            futs = {pool.submit(_fetch_repo_detail, session, slug, gh_headers): slug for slug in trending_slugs}
            for fut in concurrent.futures.as_completed(futs):
                detail = fut.result()
                if detail:
                    _register(detail, "trending-daily", bonus=45.0)

    # 2) GitHub Search API：近 7 天新建飙升项目
    week_ago = (today_bj - datetime.timedelta(days=7)).isoformat()
    two_days_ago = (today_bj - datetime.timedelta(days=2)).isoformat()
    month_ago = (today_bj - datetime.timedelta(days=30)).isoformat()

    search_queries = [
        (f"created:>={week_ago} stars:>80", "stars", "new-rising-7d", 35.0),
        (f"created:>={month_ago} stars:>300 (agent OR llm OR mcp OR cli OR local OR self-hosted)", "stars", "ai-dev-30d", 30.0),
        (f"pushed:>={two_days_ago} stars:>500 topic:ai-agents", "updated", "active-agents", 20.0),
    ]
    for q, sort_by, tag, bonus in search_queries:
        try:
            r = session.get(
                "https://api.github.com/search/repositories",
                headers=gh_headers,
                params={"q": q, "sort": sort_by, "order": "desc", "per_page": 15},
                timeout=15,
            )
            if r.status_code == 200:
                for repo in r.json().get("items") or []:
                    _register(repo, tag, bonus=bonus)
        except Exception:
            continue

    ranked = sorted(candidates.values(), key=lambda x: x["rank_score"], reverse=True)[:top_n]

    # 并发拉取候选项目的 README.md 原文
    def _attach_readme(item: dict[str, Any]) -> dict[str, Any]:
        item["origins"] = sorted(item["origins"])
        item["readme_excerpt"] = _fetch_repo_readme(session, item["full_name"], item["default_branch"], gh_headers)
        item.pop("rank_score", None)
        item.pop("default_branch", None)
        return item

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        enriched = list(pool.map(_attach_readme, ranked))

    # 优先把有 README 原文的项目排前面
    enriched.sort(key=lambda x: (1 if len(x.get("readme_excerpt", "")) > 200 else 0, x.get("stars", 0)), reverse=True)
    return enriched


def build_prompt(
    target_date: str,
    preferences_text: str,
    recent_history_lines: list[str],
    hn_stories: list[dict[str, Any]],
    rss_items: list[dict[str, Any]],
    github_projects: list[dict[str, Any]],
) -> tuple[str, str]:
    """构造系统指令与用户素材输入，确保严格遵循结构、去 AI 味口语风格与事实核验要求。"""
    system_prompt = """你是《每日开源播客》的主播兼资深技术编辑。你每天做一期 10–15 分钟的个人技术播客，听众是一位关注 AI Agent、本地大模型部署、开发效率工具和真正实用开源项目的技术实践者。

【说话风格与去 AI 味铁律】
1. 像懂技术的老朋友面对面聊天：自然、利落、有判断、有取舍，说人话，不念新闻通稿，也不装腔作势。
2. 严禁大厂黑话、公文套话和营销号口吻：绝对不要出现“赋能、抓手、闭环、赛道、破局、托举、基本盘、梯队、重塑、颠覆、引爆、狂飙、降维打击、硬核、天花板、王炸、值得期待、让我们拭目以待、总而言之、综上所述、毫不夸张地说”等词句。
3. 严禁矫枉过正编造尬聊俚语（不要说“家人们”“咱老铁”“大家伙儿”）。
4. 口播稿是拿给语音合成模型直接朗读的纯中文口语文本：
   - 严禁出现任何 Markdown 符号（#、*、**、`、-、>、[]()）、编号列表（如“1.”、“第一点：”用自然口语过渡代替）、网址 URL 或括号夹注；
   - 遇到英文项目名或专业词（如 MCP、SQLite、Docker、CLI、Agent），按开发者日常中英混读习惯自然念出，避免长串难念的 URL 或代码符号；
   - 不要念“欢迎回到今天的节目notes”“请看屏幕”之类不符合纯音频收听的话，只需在收尾自然提一句“今天聊到的所有项目链接和资料都放在节目笔记里了”。
5. 事实核验红线：
   - 深度拆解的 GitHub 开源项目必须严格依据我提供的 README 原文与元数据来写，绝不脑补不存在的功能、性能跑分或版本号；
   - README 里没写清楚的局限、依赖、部署门槛，要坦诚讲出真实取舍。

【严格节目结构与字数要求】
1. 开场（约 30 秒，120–180 字）：自然打招呼，报出北京时间日期，用一两句话点出今天最值得听的亮点。
2. 热点快讯（约 2–3 分钟，500–750 字）：精选 3–5 条过去 24 小时最相关的科技 / AI / 开发者热点，每条说清“发生了什么”以及“对写代码或用工具的人意味着什么”。
3. 深度拆解（约 8–10 分钟，1800–2400 字）：挑出 2–3 个最符合口味档案的 GitHub 近期飙升/热门开源项目逐个深挖，每个项目必须讲透四件事：
   - 它到底解决什么具体痛点；
   - 底层架构或实现思路是什么（基于 README 真实内容）；
   - 适合什么人、什么场景直接上手；
   - 目前有什么局限、代价或踩坑点（讲真实取舍）。
4. 收尾（约 20 秒，80–120 字）：收束一句核心观察，提醒节目笔记附有原项目链接，自然道别。

总字数严格控制在 2600–3400 字（纯中文口语正文，不含标题与分隔符）。

【严格输出格式】
必须只输出以下三个区块，用精确分隔行包裹，不要在区块外写任何多余解释：

===SCRIPT_START===
（这里是纯口语中文播客正文，2600–3400 字，分段自然，无任何 Markdown 符号、网址或列表符）
===SCRIPT_END===

===NOTES_START===
# 本期吸引人且平实准确的中文标题（不搞四字对仗口号，不带日期前缀也可以）

（一句话导语）

## 今日热点快讯
- 要点标题：简明说明与真实链接 [来源名](https://...)

## 开源项目深度拆解
### 1. owner/repo — 一句话定位
- 解决的问题：...
- 实现要点：...
- 适用人群与局限：...
- 项目地址：https://github.com/owner/repo

（列出本期拆解的全部 2–3 个项目，全部使用素材中的真实链接）
===NOTES_END===

===HISTORY_JSON_START===
{
  "title": "本期标题（与 NOTES 第一行去掉 # 后一致）",
  "projects": [
    {"repo": "owner/repo", "summary": "一句话说明聊了什么"}
  ],
  "news": [
    "热点主题简述1",
    "热点主题简述2"
  ]
}
===HISTORY_JSON_END==="""

    history_block = "\n".join(recent_history_lines) if recent_history_lines else "（暂无近期已播记录）"
    payload = {
        "target_date_beijing": target_date,
        "hn_top_stories": hn_stories,
        "tech_rss_recent": rss_items,
        "github_candidates_with_readme": github_projects,
    }
    user_prompt = (
        f"今天是北京时间 {target_date}。请根据以下口味档案、近 14 天已播记录和刚刚采集的真实素材，写出今天的播客口播稿和节目笔记。\n\n"
        f"【听众口味档案 preferences.md】\n{preferences_text.strip()}\n\n"
        f"【近 14 天已播项目与主题（除非过去 24 小时有重大版本更新，否则务必跳过）】\n{history_block}\n\n"
        f"【过去 24 小时采集素材（JSON）】\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
        f"请再次核对：口播稿字数必须在 2600–3400 字之间，纯口语无 Markdown/URL；notes.md 第一行必须是 `# 标题` 且仅使用素材里的真实 URL。"
    )
    return system_prompt, user_prompt


def call_gemini_generate(
    session: requests.Session,
    api_key: str,
    system_prompt: str,
    user_prompt: str,
) -> tuple[str, str]:
    """主通道：调用 Google Gemini API 写稿（优先带 Google Search Grounding，失败自动降级无 Grounding / 备用模型）。"""
    preferred = os.environ.get("GEMINI_TEXT_MODEL", "").strip()
    models = [m for m in [preferred, "gemini-3.8-flash", "gemini-2.5-flash"] if m]
    # 去重保持顺序
    models = list(dict.fromkeys(models))

    for model in models:
        for use_grounding in (True, False):
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            body: dict[str, Any] = {
                "systemInstruction": {"parts": [{"text": system_prompt}]},
                "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
                "generationConfig": {
                    "temperature": 0.7,
                    "maxOutputTokens": 8192,
                },
            }
            if use_grounding:
                body["tools"] = [{"google_search": {}}]

            for attempt in range(1, 3):
                try:
                    resp = session.post(
                        url,
                        headers={
                            "x-goog-api-key": api_key,
                            "Content-Type": "application/json",
                        },
                        json=body,
                        timeout=180,
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        cands = data.get("candidates") or []
                        if cands:
                            parts = ((cands[0].get("content") or {}).get("parts")) or []
                            text = "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
                            if len(text) > 1200:
                                tag = f"gemini-free/{model}" + ("+search" if use_grounding else "")
                                return text, tag
                        log(f"Gemini 写稿返回空内容 ({model}, grounding={use_grounding}, attempt={attempt})")
                        time.sleep(3)
                        continue

                    if resp.status_code == 429:
                        log(f"Gemini 写稿触发 429 限流 ({model}, grounding={use_grounding}, attempt={attempt})")
                        time.sleep(15)
                        continue
                    if resp.status_code in (500, 502, 503, 504):
                        log(f"Gemini 写稿服务暂不可用 HTTP {resp.status_code} ({model}, attempt={attempt})")
                        time.sleep(6)
                        continue

                    # 400/403/404 等直接切下一个配置（例如 grounding 不受支持）
                    log(f"Gemini 写稿 HTTP {resp.status_code} ({model}, grounding={use_grounding})，切换备选策略")
                    break
                except requests.RequestException as e:
                    log(f"Gemini 写稿网络异常 ({model}, attempt={attempt}): {type(e).__name__}")
                    time.sleep(5)

    return "", ""


def call_wtl_generate(
    session: requests.Session,
    base_url: str,
    api_key: str,
    system_prompt: str,
    user_prompt: str,
) -> tuple[str, str]:
    """备用通道：调用梧桐林网关 /v1/chat/completions（cost_first + 流式读取防超时）。"""
    preferred = os.environ.get("WTL_CHAT_MODEL", "").strip()
    models = [m for m in [preferred, "gemini-3.8-flash", "deepseek-v4-flash"] if m]
    models = list(dict.fromkeys(models))

    url = f"{base_url}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "text/event-stream",
        "X-Schedule-Strategy": "cost_first",
    }

    for model in models:
        body = {
            "model": model,
            "strategy": "cost_first",
            "stream": True,
            "temperature": 0.7,
            "max_tokens": 8192,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        for attempt in range(1, 4):
            try:
                with session.post(url, headers=headers, json=body, stream=True, timeout=(30, 180)) as resp:
                    if resp.status_code != 200:
                        log(f"梧桐林写稿 HTTP {resp.status_code} ({model}, attempt={attempt})")
                        if resp.status_code in (429, 500, 502, 503, 504):
                            time.sleep(4 * attempt)
                            continue
                        break

                    chunks: list[str] = []
                    ctype = resp.headers.get("Content-Type", "").lower()
                    if "text/event-stream" in ctype or "stream" in ctype:
                        for raw_line in resp.iter_lines(decode_unicode=True):
                            if not raw_line:
                                continue
                            line = raw_line.strip()
                            if not line.startswith("data:"):
                                continue
                            payload = line[5:].strip()
                            if payload == "[DONE]":
                                break
                            try:
                                evt = json.loads(payload)
                            except json.JSONDecodeError:
                                continue
                            choices = evt.get("choices") or []
                            if choices and isinstance(choices[0], dict):
                                delta = choices[0].get("delta") or {}
                                content = delta.get("content")
                                if isinstance(content, str):
                                    chunks.append(content)
                    else:
                        data = resp.json()
                        choices = data.get("choices") or []
                        if choices and isinstance(choices[0], dict):
                            msg = choices[0].get("message") or {}
                            if isinstance(msg.get("content"), str):
                                chunks.append(msg["content"])

                    text = "".join(chunks).strip()
                    if len(text) > 1200:
                        return text, f"wtl-chat/{model}"
                    log(f"梧桐林写稿返回内容过短 ({model}, attempt={attempt})")
                    time.sleep(3)
            except requests.RequestException as e:
                log(f"梧桐林写稿网络异常 ({model}, attempt={attempt}): {type(e).__name__}")
                time.sleep(4 * attempt)

    return "", ""


def extract_block(raw: str, start_tag: str, end_tag: str) -> str:
    m = re.search(re.escape(start_tag) + r"(.*?)" + re.escape(end_tag), raw, flags=re.S)
    return m.group(1).strip() if m else ""


def sanitize_spoken_script(text: str) -> str:
    """彻底清理偶发 Markdown、URL 与列表符号，确保纯口语中文文本。"""
    s = text.replace("\r\n", "\n").replace("\r", "\n")
    # Markdown 链接 [文本](url) -> 文本
    s = re.sub(r"\[([^\]]+)\]\(https?://[^)]+\)", r"\1", s)
    # 裸 URL
    s = re.sub(r"https?://\S+", "", s)
    # 标题井号
    s = re.sub(r"^\s*#{1,6}\s+", "", s, flags=re.M)
    # 列表符号与引用符号
    s = re.sub(r"^\s*[-*•>]\s+", "", s, flags=re.M)
    s = re.sub(r"^\s*\d+\.\s+", "", s, flags=re.M)
    # 行内 Markdown 符号
    s = re.sub(r"[`*_~]+", "", s)
    # 清理多余空白
    lines = [ln.strip() for ln in s.splitlines()]
    out_lines: list[str] = []
    prev_empty = False
    for ln in lines:
        if not ln:
            if not prev_empty and out_lines:
                out_lines.append("")
            prev_empty = True
        else:
            out_lines.append(ln)
            prev_empty = False
    return "\n".join(out_lines).strip()


def normalize_notes(notes_raw: str, target_date: str, fallback_title: str) -> str:
    """确保 notes.md 第一行为 '# 本期标题'。"""
    s = notes_raw.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not s:
        return f"# {fallback_title}\n\n{target_date} 期每日开源播客节目笔记。\n"
    lines = s.splitlines()
    first = lines[0].strip()
    if not first.startswith("# "):
        # 如果第一行是 ## 或纯文本标题，转为 # 标题
        cleaned_first = re.sub(r"^#+\s*", "", first).strip()
        if cleaned_first and len(cleaned_first) <= 80:
            lines[0] = f"# {cleaned_first}"
        else:
            lines.insert(0, f"# {fallback_title}")
            lines.insert(1, "")
    return "\n".join(lines).strip() + "\n"


def update_history_md(
    history_path: Path,
    target_date: str,
    title: str,
    projects: list[dict[str, Any]],
    news_items: list[str],
) -> None:
    """将当期覆盖的项目和主题写入 history.md；若当天已存在分区（--force），原地替换避免重复。"""
    existing = history_path.read_text(encoding="utf-8") if history_path.exists() else "# 已播项目与主题记录\n"
    existing = existing.replace("\r\n", "\n").rstrip()

    section_lines = [f"## {target_date} {title}".strip()]
    for proj in projects:
        if isinstance(proj, dict):
            repo = str(proj.get("repo") or "").strip()
            summary = str(proj.get("summary") or "").strip()
            if repo:
                url = f"https://github.com/{repo}" if "/" in repo and not repo.startswith("http") else repo
                section_lines.append(f"- 项目：{repo}（{url}）— {summary}" if summary else f"- 项目：{repo}（{url}）")
        elif isinstance(proj, str) and proj.strip():
            section_lines.append(f"- 项目：{proj.strip()}")
    for n in news_items[:6]:
        if isinstance(n, str) and n.strip():
            section_lines.append(f"- 热点：{n.strip()}")
    if len(section_lines) == 1:
        section_lines.append(f"- 主题：{title}")
    new_section = "\n".join(section_lines)

    # 替换已有同日期区块，否则追加到末尾
    pattern = re.compile(rf"(?ms)^## {re.escape(target_date)}\b.*?(?=^## |\Z)")
    if pattern.search(existing):
        updated = pattern.sub(new_section + "\n\n", existing).rstrip() + "\n"
    else:
        updated = existing + "\n\n" + new_section + "\n"

    history_path.write_text(updated, encoding="utf-8")


def parse_llm_output(
    raw: str,
    target_date: str,
) -> tuple[str, str, dict[str, Any]]:
    script_raw = extract_block(raw, "===SCRIPT_START===", "===SCRIPT_END===")
    notes_raw = extract_block(raw, "===NOTES_START===", "===NOTES_END===")
    hist_raw = extract_block(raw, "===HISTORY_JSON_START===", "===HISTORY_JSON_END===")

    script = sanitize_spoken_script(script_raw)
    hist_obj: dict[str, Any] = {}
    if hist_raw:
        # 容错去掉可能的 ```json 包裹
        cleaned_json = re.sub(r"^```(?:json)?\s*|\s*```$", "", hist_raw.strip(), flags=re.S)
        try:
            parsed = json.loads(cleaned_json)
            if isinstance(parsed, dict):
                hist_obj = parsed
        except Exception:
            hist_obj = {}

    fallback_title = str(hist_obj.get("title") or f"每日开源播客 {target_date}").strip()
    notes = normalize_notes(notes_raw, target_date, fallback_title)
    # 同步标题
    first_line = notes.splitlines()[0].lstrip("#").strip()
    if first_line:
        hist_obj["title"] = first_line
    return script, notes, hist_obj


def run_generate(
    channel: ChannelConfig,
    target_date: str,
    force: bool = False,
    dry_run_collect: bool = False,
) -> int:
    channel.episodes_path.mkdir(parents=True, exist_ok=True)
    script_path = channel.episodes_path / f"{target_date}-script.txt"
    notes_path = channel.episodes_path / f"{target_date}-notes.md"

    if not dry_run_collect and not force and script_path.exists() and script_path.stat().st_size > 200:
        log(f"当天口播稿已存在，幂等跳过：{script_path.name}（如需重跑请传 --force）")
        return 0

    today_date = datetime.date.fromisoformat(target_date)
    preferences_text = (
        channel.preferences_path.read_text(encoding="utf-8")
        if channel.preferences_path.exists()
        else "关注 AI Agent、本地大模型部署、开发效率/命令行工具和实用开源项目，口语自然说人话。"
    )
    recent_repos, recent_history_lines = parse_recent_history(channel.history_path, today_date, days=14)
    log(f"开始采集 {target_date} 素材（已读取近 14 天去重项目 {len(recent_repos)} 个）")

    session = make_session()
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        f_hn = pool.submit(fetch_hn_stories, session, 12)
        f_rss = pool.submit(fetch_rss_items, session, 10)
        f_gh = pool.submit(fetch_github_projects, session, today_date, recent_repos, 6)
        hn_stories = f_hn.result()
        rss_items = f_rss.result()
        github_projects = f_gh.result()

    readme_count = sum(1 for p in github_projects if len(p.get("readme_excerpt", "")) > 200)
    log(
        f"素材采集完成：HN 热点 {len(hn_stories)} 条，技术 RSS {len(rss_items)} 条，"
        f"GitHub 候选项目 {len(github_projects)} 个（含 README 原文 {readme_count} 个）"
    )

    if dry_run_collect:
        preview = {
            "date": target_date,
            "hn_titles": [x["title"] for x in hn_stories[:5]],
            "rss_titles": [f"[{x['source']}] {x['title']}" for x in rss_items[:5]],
            "github_projects": [
                {
                    "repo": p["full_name"],
                    "stars": p["stars"],
                    "lang": p["language"],
                    "readme_chars": len(p.get("readme_excerpt", "")),
                    "origins": p.get("origins"),
                }
                for p in github_projects
            ],
        }
        print(json.dumps(preview, ensure_ascii=False, indent=2))
        return 0

    if not hn_stories and not rss_items and not github_projects:
        log("错误：所有信息源均未采集到素材，中止写稿以免产生空洞内容")
        return 1

    system_prompt, user_prompt = build_prompt(
        target_date=target_date,
        preferences_text=preferences_text,
        recent_history_lines=recent_history_lines,
        hn_stories=hn_stories,
        rss_items=rss_items,
        github_projects=github_projects,
    )

    gemini_key = load_gemini_api_key()
    wtl_base, wtl_key = load_wtl_config()
    wtl_allowed = is_wtl_fallback_allowed()

    t0 = time.time()
    raw_output = ""
    used_channel = ""

    # 最多尝试 2 轮（若第 1 轮解析失败或字数明显不足则重试一次）
    for gen_round in range(1, 3):
        round_user_prompt = user_prompt
        if gen_round == 2:
            round_user_prompt += (
                "\n\n【重要提醒】上一轮输出缺少必需分隔符或口播稿字数过短。请务必严格输出 "
                "===SCRIPT_START=== / ===NOTES_START=== / ===HISTORY_JSON_START=== 三个完整区块，"
                "并把纯口语正文充实到 2600–3400 字，将 2–3 个开源项目的原理、场景和取舍讲透。"
            )

        raw_output = ""
        used_channel = ""
        if gemini_key:
            raw_output, used_channel = call_gemini_generate(session, gemini_key, system_prompt, round_user_prompt)

        if not raw_output and wtl_key and wtl_allowed:
            log("Google Gemini 写稿不可用，回退梧桐林网关（cost_first 流式模式）")
            raw_output, used_channel = call_wtl_generate(session, wtl_base, wtl_key, system_prompt, round_user_prompt)

        if not raw_output:
            log(f"第 {gen_round} 轮大模型写稿失败，未获取到有效内容")
            continue

        script, notes, hist_obj = parse_llm_output(raw_output, target_date)
        char_len = len(re.sub(r"\s+", "", script))
        if char_len >= 1800 and notes.startswith("# "):
            script_path.write_text(script + "\n", encoding="utf-8")
            notes_path.write_text(notes, encoding="utf-8")
            projects = hist_obj.get("projects") if isinstance(hist_obj.get("projects"), list) else []
            news_list = hist_obj.get("news") if isinstance(hist_obj.get("news"), list) else []
            update_history_md(
                channel.history_path,
                target_date=target_date,
                title=str(hist_obj.get("title") or f"每日开源播客 {target_date}"),
                projects=projects,
                news_items=news_list,
            )
            elapsed = time.time() - t0
            log(
                f"写稿完成：通道={used_channel}，耗时={elapsed:.1f}s，"
                f"口播稿={char_len}字 -> {script_path.name} / {notes_path.name}"
            )
            return 0

        log(f"第 {gen_round} 轮产物校验未通过（口播稿 {char_len} 字），准备重试")

    log("错误：写稿与格式校验连续失败，未写入本期文件")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="每日开源播客云端采集与写稿脚本")
    parser.add_argument("--repo-dir", default="", help="仓库根目录（默认读 REPO_DIR 或当前目录）")
    parser.add_argument("--channel", default="", help="频道配置文件路径（默认 channels/daily-open-source.json）")
    parser.add_argument("--date", default="", help="目标日期 YYYY-MM-DD（默认北京时间今天）")
    parser.add_argument("--force", action="store_true", help="即使当天稿件已存在也强制重新生成")
    parser.add_argument("--dry-run-collect", action="store_true", help="仅测试多源素材采集，不调用大模型")
    args = parser.parse_args()

    repo_dir = resolve_repo_dir(args.repo_dir)
    channel = load_channel_config(repo_dir, args.channel or None)
    target_date = args.date.strip() or datetime.datetime.now(TZ_BJ).strftime("%Y-%m-%d")

    # 校验日期格式
    datetime.date.fromisoformat(target_date)
    return run_generate(
        channel=channel,
        target_date=target_date,
        force=args.force,
        dry_run_collect=args.dry_run_collect,
    )


if __name__ == "__main__":
    sys.exit(main())
