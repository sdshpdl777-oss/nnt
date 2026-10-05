"""Collects design references from the web into the user's reference library.

    plan queries → search Pinterest / Behance / Dribbble / the web (images + item pages)
      → download (public hosts only) → size + near-duplicate filter
      → vision curation (relevant, finished designs; no stock watermarks) → save to library

Images are kept with their source link. They are third-party work: the library holds them
as references, the same way a Pinterest board does.
"""
import asyncio
import base64
import io
import itertools
import logging
import re
from typing import Callable, Optional
from urllib.parse import urlparse

import httpx
from PIL import Image
from fastapi import HTTPException

import clients
import imagegen
import models
import prompts
from config import settings
from database import SessionLocal

log = logging.getLogger(__name__)

DEFAULT_SITES = ["pinterest.com", "behance.net", "dribbble.com", None]  # None = the whole web
SITE_ALIASES = {
    "pinterest": "pinterest.com", "behance": "behance.net", "dribbble": "dribbble.com",
    "instagram": "instagram.com", "freepik": "freepik.com", "canva": "canva.com",
    "envato": "envato.com", "awwwards": "awwwards.com", "deviantart": "deviantart.com",
}
SITE_NAMES = {
    "pinimg.com": "Pinterest", "pinterest.com": "Pinterest", "behance.net": "Behance",
    "dribbble.com": "Dribbble", "instagram.com": "Instagram", "freepik.com": "Freepik",
    "canva.com": "Canva", "envato.com": "Envato", "deviantart.com": "DeviantArt",
}
# Item pages whose preview image is one specific design (not a search or board page)
ITEM_PAGE = re.compile(r"pinterest\.[a-z.]+/pin/|dribbble\.com/shots/\d|behance\.net/gallery/\d")
MIN_SIDE = 300
DUPLICATE_DISTANCE = 6  # max differing bits of a 64-bit dHash to count as the same image
CURATE_BATCH = 8


def site_name(url: str) -> str:
    host = (urlparse(url).hostname or "").removeprefix("www.")
    for domain, name in SITE_NAMES.items():
        if host == domain or host.endswith("." + domain):
            return name
    return host or "web"


def normalize_sites(sites: Optional[list[str]]) -> list[Optional[str]]:
    if not sites:
        return DEFAULT_SITES
    out: list[Optional[str]] = []
    for s in sites[:6]:
        s = s.strip().lower().removeprefix("https://").removeprefix("http://").removeprefix("www.").split("/")[0]
        s = SITE_ALIASES.get(s, s)
        if s in ("web", "any", "internet", "google", "other", "others"):
            s = None
        if s not in out:
            out.append(s)
    return out or DEFAULT_SITES


def full_size(url: str) -> str:
    """Pinterest/Dribbble thumbnails → the larger version of the same image."""
    url = re.sub(r"(i\.pinimg\.com)/(?:\d+x|\d+x\d+(?:_RS)?)/", r"\1/736x/", url)
    if "cdn.dribbble.com" in url:
        url = re.sub(r"[?&]resize=[^&]*", "", url)
    return url


def dhash(img: Image.Image) -> int:
    small = img.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    px = list(small.getdata())
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (px[row * 9 + col] > px[row * 9 + col + 1])
    return bits


def is_duplicate(h: int, seen: list[int]) -> bool:
    return any(bin(h ^ s).count("1") <= DUPLICATE_DISTANCE for s in seen)


def inspect(data: bytes) -> Optional[tuple[int, int, int]]:
    """(width, height, dHash) of a usable image, or None if it's too small or oddly shaped."""
    try:
        img = Image.open(io.BytesIO(data))
        img.seek(0)
        w, h = img.size
        if min(w, h) < MIN_SIDE or not 0.25 <= w / h <= 4:
            return None
        return w, h, dhash(img)
    except Exception:
        return None


def thumbnail_data_url(data: bytes) -> str:
    img = Image.open(io.BytesIO(data))
    img.seek(0)
    img = img.convert("RGB")
    img.thumbnail((512, 512))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


async def plan_queries(topic: str) -> list[str]:
    try:
        plan = await imagegen._json_completion(
            prompts.REFERENCE_QUERY_PLANNER,
            [{"type": "text", "text": f"TOPIC:\n{topic}"}],
            model=settings.CLASSIFY_MODEL,
        )
        queries = [q.strip()[:120] for q in plan.get("queries", []) if isinstance(q, str) and q.strip()]
    except Exception:
        log.exception("reference query planning failed")
        queries = []
    return ([topic] + [q for q in queries if q.lower() != topic.lower()])[:4]


async def search(queries: list[str], sites: list[Optional[str]]) -> list[dict]:
    """Candidate images, interleaved across sites so no single site dominates."""
    sem = asyncio.Semaphore(6)

    async def one(query: str, site: Optional[str]) -> list[dict]:
        async with sem:
            try:
                found = await imagegen.tavily.search(
                    query, max_results=10, include_images=True, include_image_descriptions=True,
                    include_domains=[site] if site else None,
                )
            except Exception:
                log.warning("reference search failed: %s @ %s", query, site)
                return []
        out = []
        for img in found.get("images") or []:
            url = img["url"] if isinstance(img, dict) else img
            out.append({"image_url": full_size(url), "source_url": None, "site": site})
        for r in found.get("results") or []:
            if ITEM_PAGE.search(r.get("url", "")):
                out.append({"image_url": None, "source_url": r["url"], "site": site})
        return out

    pairs = list(itertools.product(queries, sites))[:16]
    per_search = await asyncio.gather(*(one(q, s) for q, s in pairs))
    seen, merged = set(), []
    for group in itertools.zip_longest(*per_search):
        for c in group:
            key = c and (c["image_url"] or c["source_url"])
            if c and key not in seen:
                seen.add(key)
                merged.append(c)
    return merged


async def download(candidates: list[dict], want: int, known_hashes: list[int], progress: Callable[[str], None]) -> list[dict]:
    """Downloads candidates until `want` usable, distinct images are in hand."""
    sem = asyncio.Semaphore(8)
    hashes = list(known_hashes)
    usable: list[dict] = []
    done = 0
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh) NNT-Studio", "Accept": "image/*,text/html;q=0.9,*/*;q=0.5"}

    async with httpx.AsyncClient(timeout=15, follow_redirects=False, headers=headers) as http:
        async def one(c: dict):
            nonlocal done
            if len(usable) >= want:
                return
            async with sem:
                if len(usable) >= want:
                    return
                try:
                    if c["image_url"]:
                        url, ctype, data = await clients.get_public(http, c["image_url"], settings.MAX_IMAGE_BYTES)
                        name = None
                    else:  # an item page: use its preview image
                        data, ctype, name = await clients.fetch_image(c["source_url"])
                        url = c["source_url"]
                except (HTTPException, imagegen.ImageGenError, httpx.HTTPError, UnicodeError):
                    return
                except Exception:
                    log.warning("reference download failed: %s", c, exc_info=True)
                    return
            if ctype not in clients.REFERENCE_TYPES:
                return
            info = await asyncio.to_thread(inspect, data)
            if info is None or len(usable) >= want:
                return
            w, h, hsh = info
            if is_duplicate(hsh, hashes):
                return
            hashes.append(hsh)
            usable.append({**c, "data": data, "content_type": ctype, "width": w, "height": h, "phash": f"{hsh:016x}",
                           "source_url": c["source_url"] or url, "file_name": name})
            done += 1
            if done % 5 == 0:
                progress(f"Downloaded {done} candidate designs…")

        await asyncio.gather(*(one(c) for c in candidates))
    return usable


async def curate(topic: str, items: list[dict]) -> list[dict]:
    """Keeps finished, relevant designs; gives each a short title."""
    async def batch(chunk: list[dict]) -> list[dict]:
        content = [{"type": "text", "text": f"TOPIC:\n{topic}\n\n{len(chunk)} images follow, numbered from 0. Return JSON."}]
        for item in chunk:
            content.append({"type": "image_url", "image_url": {"url": await asyncio.to_thread(thumbnail_data_url, item["data"]), "detail": "low"}})
        try:
            verdict = await imagegen._json_completion(prompts.REFERENCE_CURATOR, content, model=settings.CLASSIFY_MODEL)
        except Exception:
            log.exception("reference curation failed")
            return []
        kept = []
        for v in verdict.get("items", []):
            i = v.get("i")
            if isinstance(i, int) and 0 <= i < len(chunk) and v.get("keep"):
                kept.append({**chunk[i], "title": (v.get("title") or "").strip()[:80]})
        return kept

    chunks = [items[i:i + CURATE_BATCH] for i in range(0, len(items), CURATE_BATCH)]
    results = await asyncio.gather(*(batch(c) for c in chunks))
    return [item for group in results for item in group]


def existing_hashes(user_id: int) -> list[int]:
    with SessionLocal() as db:
        rows = db.query(models.Reference.phash).filter(models.Reference.user_id == user_id, models.Reference.phash.isnot(None)).all()
    return [int(h, 16) for (h,) in rows]


def save(user_id: int, stored: dict) -> dict:
    with SessionLocal() as db:
        row = models.Reference(user_id=user_id, **stored)
        db.add(row)
        db.commit()
        db.refresh(row)
        return clients.ReferenceOut.model_validate(row).model_dump(mode="json")


async def collect(
    *,
    user_id: int,
    topic: str,
    count: int,
    sites: Optional[list[str]],
    progress: Callable[[str], None],
    on_saved: Callable[[dict], None],
) -> dict:
    count = max(1, min(count, settings.MAX_COLLECT_REFERENCES))
    site_list = normalize_sites(sites)
    names = ", ".join(SITE_NAMES.get(s, s) if s else "the web" for s in site_list)

    progress(f"Searching {names}…")
    queries = await plan_queries(topic)
    candidates = await search(queries, site_list)
    if not candidates:
        return {"saved": [], "searched": queries, "candidates": 0, "reason": "The searches returned no images."}

    progress(f"Found {len(candidates)} candidates. Downloading…")
    known = await asyncio.to_thread(existing_hashes, user_id)
    # Some won't be designs or won't be relevant; fetch extra so curation still leaves enough
    usable = await download(candidates, int(count * 1.7) + 4, known, progress)

    progress(f"Picking the best {count} of {len(usable)} designs…")
    kept = (await curate(topic, usable))[:count]

    progress(f"Saving {len(kept)} designs to your references…")
    sem = asyncio.Semaphore(4)
    saved: list[dict] = []
    folder = f"nnt/references/user-{user_id}/collected"

    async def store(item: dict):
        source = site_name(item["source_url"])
        name = f"{item['title'] or topic} — {source}"[:200]
        async with sem:
            try:
                stored = await clients.put_on_cloudinary(item["data"], item["file_name"] or "reference", item["content_type"], folder)
            except Exception:
                log.warning("saving collected reference failed", exc_info=True)
                return
        stored.update(name=name, collection=topic[:120], source_url=item["source_url"][:2048], phash=item["phash"],
                      description=item["title"] or None)
        ref = await asyncio.to_thread(save, user_id, stored)
        saved.append(ref)
        on_saved(ref)

    await asyncio.gather(*(store(item) for item in kept))
    return {
        "saved": saved,
        "searched": queries,
        "candidates": len(candidates),
        "downloaded": len(usable),
        "sites": [site_name(r["source_url"]) for r in saved],
    }
