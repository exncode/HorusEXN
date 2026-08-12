import os
import re
import json
import hashlib
import asyncio
from typing import Any
from urllib.parse import urlparse, parse_qs, urlencode

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()

APP_ID = "com.scrapper.animezey"
APP_NAME = "Animezey Search"
APP_VERSION = "1.0.0"

ANIMEZEY_SEARCH_URL = os.getenv("ANIMEZEY_SEARCH_URL", "https://1.animezeydl.workers.dev/1:search")
ANIMEZEY_BASE_URL = os.getenv("ANIMEZEY_BASE_URL", "https://1.animezeydl.workers.dev")
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "")
TMDB_LANGUAGE = os.getenv("TMDB_LANGUAGE", "pt-BR")
MAX_SEARCH_RESULTS = int(os.getenv("MAX_SEARCH_RESULTS", "80"))
MAX_PAGES = int(os.getenv("MAX_PAGES", "20"))
PAGE_SIZE = 1000
VIDEO_EXTS = (".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".ts", ".m4v", ".webm")

app = FastAPI(title=APP_NAME)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "HEAD", "OPTIONS"],
    allow_headers=["*"],
)

# Small in-process caches. They avoid hammering either upstream API while Stremio
# asks for catalog -> meta -> stream in quick succession.
CACHE: dict[str, tuple[float, Any]] = {}
CACHE_TTL = int(os.getenv("CACHE_TTL", "300"))


def cache_get(key: str):
    import time
    item = CACHE.get(key)
    if not item:
        return None
    ts, value = item
    if time.time() - ts > CACHE_TTL:
        CACHE.pop(key, None)
        return None
    return value


def cache_set(key: str, value: Any):
    import time
    CACHE[key] = (time.time(), value)
    return value


def api_key_required() -> bool:
    return bool(TMDB_API_KEY)


async def api_json(method: str, url: str, *, json_body: dict | None = None, params: dict | None = None, timeout: float = 30):
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers={"User-Agent": APP_NAME}) as client:
        response = await client.request(method, url, json=json_body, params=params)
        response.raise_for_status()
        return response.json()


async def animezey_post(payload: dict) -> dict:
    return await api_json(
        "POST",
        ANIMEZEY_SEARCH_URL,
        json_body=payload,
        timeout=30,
    )


async def fetch_all_pages(query: str) -> list[dict]:
    key = "animezey:" + hashlib.sha256(query.lower().strip().encode()).hexdigest()
    cached = cache_get(key)
    if cached is not None:
        return cached

    all_files: list[dict] = []
    token = None
    idx = 0

    for _ in range(MAX_PAGES):
        payload = {
            "q": query,
            "is_search_page": True,
            "root_type": 1,
            "page_token": token,
            "page_index": idx,
        }
        try:
            data = await animezey_post(payload)
        except Exception:
            break

        files = data.get("data", {}).get("files", [])
        if not files:
            break

        all_files.extend(files)
        token = data.get("nextPageToken")
        if not token or len(all_files) >= PAGE_SIZE * MAX_PAGES:
            break

        cur = data.get("curPageIndex")
        idx = (cur if isinstance(cur, int) else idx) + 1

    return cache_set(key, all_files[: PAGE_SIZE * MAX_PAGES])


def build_download_link(link_part: str | None) -> str | None:
    if not link_part:
        return None
    if link_part.startswith("http://") or link_part.startswith("https://"):
        return link_part
    if not link_part.startswith("/"):
        return None

    try:
        parts = link_part.split("?", 1)
        path = parts[0]
        qs = parts[1] if len(parts) > 1 else ""
        params = parse_qs(qs)
        file_id = params.get("file", [None])[0]
        if not file_id:
            return None
        out = {"file": file_id}
        for name in ("expiry", "mac"):
            value = params.get(name, [None])[0]
            if value:
                out[name] = value
        return f"{ANIMEZEY_BASE_URL}{path}?{urlencode(out)}"
    except Exception:
        return None


def clean_title(raw: str) -> str:
    title = os.path.splitext(raw)[0]
    title = re.sub(r"[\[\(].*?[\]\)]", " ", title)
    title = re.sub(
        r"\b(2160p|1080p|720p|480p|360p|4K|HD|SD|WEB[- .]?DL|WEBRip|BluRay|BDRip|HDTV|AMZN|NF|HMAX|DSNP|TrueHD|DDP\d*\.?\d*|EAC3|AC3|DTS|Dual\s*[ÁA]udio|x264|x265|HEVC|AV1|AAC|H\.?264|H\.?265)\b",
        " ",
        title,
        flags=re.I,
    )
    title = re.sub(r"[._\-]+", " ", title)
    title = re.sub(r"\s+", " ", title).strip()
    return title


def extract_episode(filename: str):
    base = os.path.splitext(filename)[0]
    patterns = [
        re.compile(r"\bS(?P<season>\d{1,2})E(?P<episode>\d{1,3})\b", re.I),
        re.compile(r"\b(?P<season>\d{1,2})x(?P<episode>\d{1,3})\b", re.I),
    ]
    for pattern in patterns:
        m = pattern.search(base)
        if m:
            title = base[: m.start()]
            title = clean_title(title)
            return title, int(m.group("season")), int(m.group("episode"))
    return None


def filename_is_video(name: str) -> bool:
    return name.lower().endswith(VIDEO_EXTS)


def dedupe_files(files: list[dict]) -> list[dict]:
    out = []
    seen = set()
    for f in files:
        name = (f.get("name") or "").strip()
        if not name or f.get("mimeType") == "application/vnd.google-apps.folder":
            continue
        if not filename_is_video(name):
            continue
        link = build_download_link(f.get("link")) or f.get("link")
        if not link:
            continue
        key = (name.lower(), link)
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": name, "url": link})
    return out


async def tmdb_search(query: str) -> list[dict]:
    if not TMDB_API_KEY:
        return []
    key = "tmdb:" + TMDB_LANGUAGE + ":" + hashlib.sha256(query.lower().strip().encode()).hexdigest()
    cached = cache_get(key)
    if cached is not None:
        return cached

    params = {"api_key": TMDB_API_KEY, "language": TMDB_LANGUAGE, "query": query, "include_adult": "false"}
    try:
        data = await api_json("GET", "https://api.themoviedb.org/3/search/multi", params=params, timeout=15)
    except Exception:
        return []

    results = []
    for item in data.get("results", []):
        if item.get("media_type") not in ("movie", "tv"):
            continue
        title = item.get("title") or item.get("name")
        if not title:
            continue
        results.append(item)
    return cache_set(key, results)


async def tmdb_details(media_type: str, tmdb_id: int) -> dict | None:
    if not TMDB_API_KEY:
        return None
    key = f"tmdbdetail:{media_type}:{tmdb_id}:{TMDB_LANGUAGE}"
    cached = cache_get(key)
    if cached is not None:
        return cached
    endpoint = "movie" if media_type == "movie" else "tv"
    try:
        data = await api_json(
            "GET",
            f"https://api.themoviedb.org/3/{endpoint}/{tmdb_id}",
            params={"api_key": TMDB_API_KEY, "language": TMDB_LANGUAGE},
            timeout=15,
        )
        return cache_set(key, data)
    except Exception:
        return None


def tmdb_id(raw_id: str) -> tuple[str, int] | None:
    m = re.fullmatch(r"tmdb:(movie|tv):(\d+)", raw_id)
    if m:
        return m.group(1), int(m.group(2))
    m = re.fullmatch(r"tmdb:(\d+)", raw_id)
    if m:
        return "movie", int(m.group(1))
    return None


def make_meta_id(media_type: str, item_id: int) -> str:
    return f"tmdb:{media_type}:{item_id}"


def poster_url(path: str | None) -> str | None:
    return f"https://image.tmdb.org/t/p/w600_and_h900_bestv2{path}" if path else None


def background_url(path: str | None) -> str | None:
    return f"https://image.tmdb.org/t/p/w1280{path}" if path else None


def year_from_item(item: dict) -> str | None:
    value = item.get("release_date") or item.get("first_air_date")
    return value[:4] if value else None


def title_from_tmdb(item: dict) -> str:
    return item.get("title") or item.get("name") or item.get("original_title") or item.get("original_name") or "Desconhecido"


def search_result_to_meta(item: dict) -> dict:
    media_type = item.get("media_type")
    tmdb_type = "tv" if media_type == "tv" else "movie"
    meta = {
        "id": make_meta_id(tmdb_type, int(item["id"])),
        "type": "series" if tmdb_type == "tv" else "movie",
        "name": title_from_tmdb(item),
    }
    p = poster_url(item.get("poster_path"))
    if p:
        meta["poster"] = p
        meta["posterShape"] = "poster"
    b = background_url(item.get("backdrop_path"))
    if b:
        meta["background"] = b
    y = year_from_item(item)
    if y:
        meta["releaseInfo"] = y
    if item.get("overview"):
        meta["description"] = item["overview"]
    return meta


async def resolve_tmdb_for_title(title: str, preferred_type: str | None = None) -> dict | None:
    results = await tmdb_search(title)
    if not results:
        return None

    def score(item: dict):
        item_title = title_from_tmdb(item).lower()
        target = title.lower()
        score = 0
        if item_title == target:
            score += 100
        if target in item_title or item_title in target:
            score += 40
        if preferred_type == "series" and item.get("media_type") == "tv":
            score += 20
        if preferred_type == "movie" and item.get("media_type") == "movie":
            score += 20
        score += float(item.get("popularity", 0)) / 100
        return score

    return max(results, key=score)


async def build_catalog(search: str, content_type: str) -> list[dict]:
    files = dedupe_files(await fetch_all_pages(search))
    if not files:
        # Fallback: TMDB can resolve a localized query even when Animezey doesn't
        # return file names immediately. The resulting item will be tested again
        # when Stremio asks for streams.
        tmdb_results = await tmdb_search(search)
        filtered = [x for x in tmdb_results if (x.get("media_type") == "tv") == (content_type == "series")]
        return [search_result_to_meta(x) for x in filtered[:MAX_SEARCH_RESULTS]]

    candidates: dict[str, dict] = {}
    for f in files:
        ep = extract_episode(f["name"])
        if ep:
            title, _, _ = ep
            preferred = "series"
        else:
            title = clean_title(f["name"])
            preferred = "movie"

        if not title:
            continue

        # Group by the filename-level title first; only resolve the title once.
        group = candidates.setdefault(title.lower(), {"title": title, "preferred": preferred, "count": 0})
        group["count"] += 1

    groups = sorted(candidates.values(), key=lambda x: x["count"], reverse=True)
    metas: list[dict] = []
    seen_ids = set()

    # Resolve a bounded number of titles concurrently to keep search responsive.
    async def resolve(group):
        item = await resolve_tmdb_for_title(group["title"], group["preferred"])
        return group, item

    pairs = await asyncio.gather(*(resolve(g) for g in groups[:MAX_SEARCH_RESULTS]))
    for group, item in pairs:
        if not item:
            continue
        is_series = item.get("media_type") == "tv"
        if (content_type == "series") != is_series:
            continue
        meta = search_result_to_meta(item)
        if meta["id"] not in seen_ids:
            seen_ids.add(meta["id"])
            metas.append(meta)
    return metas[:MAX_SEARCH_RESULTS]


async def find_files_for_tmdb(media_type: str, item_id: int) -> list[dict]:
    details = await tmdb_details(media_type, item_id)
    if not details:
        return []

    names = []
    for field in ("title", "original_title", "name", "original_name"):
        value = details.get(field)
        if value and value not in names:
            names.append(value)

    # Include localized alternate titles when possible.
    if TMDB_API_KEY:
        endpoint = "movie" if media_type == "movie" else "tv"
        try:
            alt = await api_json(
                "GET",
                f"https://api.themoviedb.org/3/{endpoint}/{item_id}/alternative_titles",
                params={"api_key": TMDB_API_KEY},
                timeout=15,
            )
            for item in alt.get("titles", []) or alt.get("results", []):
                value = item.get("title") or item.get("name")
                if value and value not in names:
                    names.append(value)
        except Exception:
            pass

    # Search all known names concurrently and merge links.
    searches = await asyncio.gather(*(fetch_all_pages(name) for name in names[:8]))
    merged = []
    for result in searches:
        merged.extend(result)
    return dedupe_files(merged)


def movie_streams(files: list[dict]) -> list[dict]:
    streams = []
    for f in files:
        streams.append({"title": f"Animezey • {f['name']}", "url": f["url"], "behaviorHints": {"bingeGroup": "animezey"}})
    return streams


def episode_streams_for(files: list[dict], season: int, episode: int) -> list[dict]:
    streams = []
    for f in files:
        parsed = extract_episode(f["name"])
        if not parsed:
            continue
        _, s, e = parsed
        if s == season and e == episode:
            streams.append({
                "title": f"Animezey • {f['name']}",
                "url": f["url"],
                "behaviorHints": {"bingeGroup": "animezey"},
            })
    return streams


def episode_id(tmdb_tv_id: int, season: int, episode: int) -> str:
    return f"tmdb:{tmdb_tv_id}:s{season}:e{episode}"


def parse_episode_id(raw_id: str):
    m = re.fullmatch(r"tmdb:(\d+):s(\d+):e(\d+)", raw_id)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


async def build_series_meta(tv_id: int) -> dict:
    details = await tmdb_details("tv", tv_id)
    if not details:
        return {"meta": {"id": make_meta_id("tv", tv_id), "type": "series", "name": f"TMDB {tv_id}"}}

    meta = {
        "id": make_meta_id("tv", tv_id),
        "type": "series",
        "name": title_from_tmdb(details),
        "description": details.get("overview") or "",
        "genres": details.get("genres", []),
    }
    p = poster_url(details.get("poster_path"))
    b = background_url(details.get("backdrop_path"))
    if p:
        meta["poster"] = p
        meta["posterShape"] = "poster"
    if b:
        meta["background"] = b

    files = await find_files_for_tmdb("tv", tv_id)
    episodes = {}
    for f in files:
        parsed = extract_episode(f["name"])
        if not parsed:
            continue
        title, season, episode = parsed
        key = (season, episode)
        episodes[key] = {"season": season, "episode": episode, "title": title or f"Episódio {episode}"}

    meta["videos"] = [
        {
            "id": episode_id(tv_id, season, episode),
            "title": data["title"],
            "season": season,
            "episode": episode,
        }
        for (season, episode), data in sorted(episodes.items())
    ]
    return {"meta": meta}


@app.get("/manifest.json")
async def manifest():
    return {
        "id": APP_ID,
        "version": APP_VERSION,
        "name": APP_NAME,
        "description": "Busca títulos e fornece streams do Animezey ao Stremio, usando TMDB para resolução de títulos.",
        "logo": "https://www.stremio.com/website/stremio-logo-small.png",
        "resources": [
            {
                "name": "catalog",
                "types": ["movie", "series"],
                "idPrefixes": ["tmdb:"]
            },
            {
                "name": "meta",
                "types": ["movie", "series"],
                "idPrefixes": ["tmdb:"]
            },
            {
                "name": "stream",
                "types": ["movie", "series"],
                "idPrefixes": ["tmdb:"]
            }
        ],
        "types": ["movie", "series"],
        "catalogs": [
            {
                "type": "movie",
                "id": "animezey-movies",
                "name": "Animezey",
                "extra": [{"name": "search", "isRequired": True}]
            },
            {
                "type": "series",
                "id": "animezey-series",
                "name": "Animezey",
                "extra": [{"name": "search", "isRequired": True}]
            }
        ],
        "behaviorHints": {"configurable": False, "adult": False}
    }


@app.get("/catalog/{content_type}/{catalog_id}.json")
async def catalog(content_type: str, catalog_id: str, request: Request):
    query = request.query_params.get("search", "").strip()
    if not query:
        return {"metas": []}

    if catalog_id not in {"animezey-movies", "animezey-series"}:
        return {"metas": []}
    if content_type not in {"movie", "series"}:
        return {"metas": []}

    metas = await build_catalog(query, content_type)
    return {"metas": metas}


@app.get("/meta/{content_type}/{raw_id}.json")
async def meta(content_type: str, raw_id: str):
    parsed = tmdb_id(raw_id)
    if not parsed:
        return {"meta": {"id": raw_id, "type": content_type, "name": raw_id}}

    media_type, item_id = parsed
    if content_type == "series" and media_type == "tv":
        return await build_series_meta(item_id)

    details = await tmdb_details(media_type, item_id)
    if not details:
        return {"meta": {"id": raw_id, "type": content_type, "name": raw_id}}

    meta = search_result_to_meta({**details, "media_type": media_type})
    return {"meta": meta}


@app.get("/stream/{content_type}/{raw_id}.json")
async def streams(content_type: str, raw_id: str):
    parsed_episode = parse_episode_id(raw_id)
    if parsed_episode:
        tv_id, season, episode = parsed_episode
        files = await find_files_for_tmdb("tv", tv_id)
        return {"streams": episode_streams_for(files, season, episode)}

    parsed = tmdb_id(raw_id)
    if not parsed:
        return {"streams": []}

    media_type, item_id = parsed
    files = await find_files_for_tmdb(media_type, item_id)
    if media_type == "tv" or content_type == "series":
        return {"streams": []}
    return {"streams": movie_streams(files)}


@app.get("/")
async def root():
    return {"name": APP_NAME, "manifest": "/manifest.json", "tmdb_enabled": api_key_required()}


@app.get("/health")
async def health():
    return {"ok": True, "tmdb_enabled": api_key_required(), "animezey_search": ANIMEZEY_SEARCH_URL}
