"""Image generation pipeline.

    reference URL    ──► brand analyst    ──► URL_BRIEF  ─┐
    reference images ──► visual forensics ──► IMAGE_SPEC ─┼─► prompt builder ──► image model ──► check
    user's words ─────────────────────────────────────────┘   (user's words stay verbatim)

The analyses run in parallel. With no references at all, the user's words go to the
image model untouched. Uploaded images are either logos (placed as-is, never restyled)
or references (style to follow); both are passed to the image model as pixels.

`generate_for_logos` runs that pipeline once per logo, optionally researching related
posts on the web for each brand. Web images are other people's work, so they only ever
become a text style description; their pixels never reach the image model.
"""
import asyncio
import base64
import ipaddress
import json
import logging
import re
import socket
import time
from collections import deque
from typing import Callable, Optional
from urllib.parse import urlparse

import cloudinary.uploader
import httpx
import trafilatura
from openai import AsyncOpenAI, BadRequestError, NotFoundError, RateLimitError
from tavily import AsyncTavilyClient

import prompts
from config import settings

log = logging.getLogger(__name__)

client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
tavily = AsyncTavilyClient(api_key=settings.TAVILY_API_KEY)

SIZES = {"square": "1024x1024", "landscape": "1536x1024", "portrait": "1024x1536", "auto": "auto"}
PAGE_TEXT_CHARS = 8000
MAX_PAGE_BYTES = 3 * 1024 * 1024


class ImageGenError(Exception):
    """A failure worth telling the user about in plain words."""


class InputImageLimiter:
    """Process-wide sliding window over input images sent to the image model.
    OpenAI rate-limits image *inputs* per minute per organization (5/min on this account),
    so batches queue here instead of failing with 429s."""

    def __init__(self, per_minute: int):
        self.capacity = max(per_minute, 1)
        self.sent: deque[float] = deque()
        self.lock = asyncio.Lock()

    async def acquire(self, n: int, on_wait: Callable[[float], None]) -> None:
        n = min(n, self.capacity)
        if n <= 0:
            return
        async with self.lock:  # waiters are served in order
            while True:
                now = time.monotonic()
                while self.sent and now - self.sent[0] >= 60:
                    self.sent.popleft()
                if len(self.sent) + n <= self.capacity:
                    self.sent.extend([now] * n)
                    return
                # wait until enough of the oldest sends leave the window
                wait = 60 - (now - self.sent[len(self.sent) + n - self.capacity - 1]) + 0.5
                on_wait(wait)
                await asyncio.sleep(wait)


input_limiter = InputImageLimiter(settings.IMAGE_INPUTS_PER_MINUTE)




def _check_public_url(url: str) -> str:
    parsed = urlparse(url if "://" in url else f"https://{url}")
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ImageGenError(f"{url} is not a valid web address")
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(parsed.hostname, None)}
    except socket.gaierror:
        raise ImageGenError(f"Could not find the website {parsed.hostname}")
    # Don't let a chat message make the server fetch its own network
    if any(not ipaddress.ip_address(a.split("%")[0]).is_global for a in addresses):
        raise ImageGenError(f"{parsed.hostname} is not a public website")
    return parsed.geturl()


async def _page_text(url: str) -> str:
    async with httpx.AsyncClient(follow_redirects=True, timeout=15, headers={"User-Agent": "Mozilla/5.0 NNT-Studio"}) as http:
        response = await http.get(url)
        response.raise_for_status()
        html = response.text[:MAX_PAGE_BYTES]
    text = trafilatura.extract(html, include_comments=False, include_tables=False) or ""
    meta = trafilatura.extract_metadata(html)
    header = "\n".join(
        f"{label}: {value}"
        for label, value in (("Title", meta and meta.title), ("Description", meta and meta.description), ("Site", meta and meta.sitename))
        if value
    )
    return f"{header}\n\n{text}".strip()[:PAGE_TEXT_CHARS]


async def _screenshot(url: str) -> bytes:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=True)
        try:
            page = await browser.new_page(viewport={"width": 1280, "height": 900})
            await page.goto(url, wait_until="networkidle", timeout=25000)
            return await page.screenshot(type="jpeg", quality=75)
        finally:
            await browser.close()


async def analyze_url(url: str) -> dict:
    url = _check_public_url(url)
    text, shot = await asyncio.gather(_page_text(url), _screenshot(url), return_exceptions=True)
    if isinstance(text, Exception):
        log.warning("page text failed for %s: %s", url, text)
        text = ""
    if isinstance(shot, Exception):
        log.warning("screenshot failed for %s: %s", url, shot)
        shot = None
    if not text and shot is None:
        raise ImageGenError(f"Could not load {url}")

    content = [{"type": "text", "text": f"URL: {url}\n\n<page_content>\n{text or '(no readable text)'}\n</page_content>"}]
    if shot is not None:
        content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(shot).decode()}})
    return await _json_completion(prompts.BRAND_ANALYST, content)


# ---------- Reference images ----------

async def analyze_image(url: str) -> dict:
    return await _json_completion(
        prompts.VISUAL_FORENSICS,
        [
            {"type": "text", "text": "Analyze this reference image. Return JSON."},
            {"type": "image_url", "image_url": {"url": url, "detail": "high"}},
        ],
    )


async def _json_completion(system: str, content: list, model: Optional[str] = None) -> dict:
    response = await client.chat.completions.create(
        model=model or settings.ANALYSIS_MODEL,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": system}, {"role": "user", "content": content}],
    )
    return json.loads(response.choices[0].message.content)


# ---------- Prompt ----------

async def build_prompt(user_prompt: str, url_brief: dict | None, image_specs: list[dict]) -> str:
    if url_brief is None and not image_specs:
        return user_prompt

    image_spec = image_specs[0] if len(image_specs) == 1 else (image_specs or None)
    response = await client.chat.completions.create(
        model=settings.PROMPT_MODEL,
        temperature=0,
        messages=[
            {"role": "system", "content": prompts.PROMPT_BUILDER},
            {
                "role": "user",
                "content": (
                    f"USER_PROMPT:\n{user_prompt}\n\n"
                    f"URL_BRIEF:\n{json.dumps(url_brief, indent=2) if url_brief else 'null'}\n\n"
                    f"IMAGE_SPEC:\n{json.dumps(image_spec, indent=2) if image_spec else 'null'}"
                ),
            },
        ],
    )
    built = response.choices[0].message.content.strip()
    # Rule 1 is enforced here, not trusted to the model: the user's words lead, verbatim.
    if not built.startswith(user_prompt):
        lines = [l for l in built.splitlines() if l.startswith(("Style:", "Brand:", "Avoid:"))]
        built = user_prompt + ("\n\n" + "\n".join(lines) if lines else "")
    return built


# ---------- Image model ----------

def input_note(editing: bool, n_logos: int, n_refs: int) -> str:
    """Tells the image model what each attached input image is, by position."""
    parts, i = [], 1

    def span(n):
        return f"Image {i}" if n == 1 else f"Images {i}–{i + n - 1}"

    if editing:
        parts.append("Image 1 is the image to modify.")
        i += 1
    if n_logos:
        parts.append(
            f"{span(n_logos)} {'is the brand logo' if n_logos == 1 else 'are brand logos'}: place "
            f"{'it' if n_logos == 1 else 'them'} exactly as provided — same shapes, colors and spelling; "
            "never redraw, restyle, translate or crop the logo. Any 'avoid logos/brand names' above refers only to "
            "other companies' marks, never to this logo."
        )
        i += n_logos
    if n_refs:
        parts.append(
            f"{span(n_refs)} {'is the user' + chr(39) + 's style reference' if n_refs == 1 else 'are the user' + chr(39) + 's style references'}: "
            "follow their composition, layout, style, colors and typography wherever the request above doesn't say otherwise."
        )
    return "\n\nInputs: " + " ".join(parts) if parts else ""


async def _download_png(http: httpx.AsyncClient, url: str) -> bytes:
    # Cloudinary converts on the fly, so GIF/WebP uploads reach the image model as PNG
    if "/image/upload/" in url:
        url = url.replace("/image/upload/", "/image/upload/f_png/", 1)
    response = await http.get(url)
    response.raise_for_status()
    return response.content


async def _render(prompt: str, size: str, inputs: list[bytes], progress: Callable[[str], None] = lambda _: None) -> bytes:
    """Generate from text alone, or from text plus input images (the image to edit, logos, references).
    Waits for the input-image rate limit, and retries 429s after the delay OpenAI asks for."""
    last_error = None
    for model in dict.fromkeys([settings.IMAGE_MODEL, settings.IMAGE_FALLBACK_MODEL]):
        for attempt in range(settings.IMAGE_RATE_LIMIT_RETRIES + 1):
            try:
                return await _render_once(model, prompt, size, inputs, progress)
            except RateLimitError as e:
                if attempt == settings.IMAGE_RATE_LIMIT_RETRIES:
                    raise ImageGenError("The image model is busy (rate limit). Please try again in a minute.")
                match = re.search(r"try again in ([\d.]+)s", str(e))
                wait = float(match.group(1)) + 1 if match else 15.0
                progress(f"Image model rate limit reached, retrying in {wait:.0f}s…")
                await asyncio.sleep(wait)
            except NotFoundError as e:
                log.warning("image model %s unavailable, trying fallback: %s", model, e)
                last_error = e
                break
    if last_error is not None:
        raise last_error
    raise RuntimeError("Image models unavailable or exhausted")


async def _render_once(model: str, prompt: str, size: str, inputs: list[bytes], progress: Callable[[str], None]) -> bytes:
    await input_limiter.acquire(len(inputs), lambda wait: progress(f"Queued for the image model (rate limit), about {wait:.0f}s…"))
    try:
        if inputs:
            result = await client.images.edit(
                model=model, prompt=prompt,
                image=[(f"input-{i + 1}.png", data, "image/png") for i, data in enumerate(inputs)],
                size=size, quality=settings.IMAGE_QUALITY,
            )
        else:
            result = await client.images.generate(model=model, prompt=prompt, size=size, quality=settings.IMAGE_QUALITY)
        return base64.b64decode(result.data[0].b64_json)
    except BadRequestError as e:
        if "safety" in str(e).lower() or "moderation" in str(e).lower():
            raise ImageGenError("The image model declined this request for safety reasons.")
        raise


async def generate(
    *,
    user_id: int,
    user_prompt: str,
    reference_images: list[str],
    reference_url: str | None,
    aspect: str,
    edit_image_url: str | None,
    logos: list[str] = (),
    extra_specs: list[dict] = (),
    url_brief: Optional[dict] = None,
    reference_specs: Optional[list[dict]] = None,
    preferences: str = "",
    brand_note: str = "",
    brand_facts: str = "",
    progress: Callable[[str], None] = lambda _: None,
) -> dict:
    """Reference images are used twice: analyzed into IMAGE_SPEC for the prompt, and
    passed to the image model as actual pixels so it can match what a text description can't
    (layout, exact typography). Logos are passed as pixels only, to be placed unchanged.
    `extra_specs` (web inspiration) and `url_brief` (an already-analyzed site) come from research;
    `reference_specs` skips re-analyzing references a batch has already analyzed.
    `brand_note` is the client's brand kit (colors, fonts, footer) and `brand_facts` its real details,
    which the QA must not flag as invented."""
    logos = list(logos)
    tasks = {}
    if reference_url:
        tasks["url"] = analyze_url(reference_url)
    if reference_specs is None:
        for i, url in enumerate(reference_images):
            tasks[f"img{i}"] = analyze_image(url)
    if reference_url and reference_images:
        progress("Analyzing the reference website and images…")
    elif reference_url:
        progress("Analyzing the reference website…")
    elif reference_images and reference_specs is None:
        n = len(reference_images)
        progress(f"Studying {n} reference image{'s' if n > 1 else ''}…")

    input_urls = ([edit_image_url] if edit_image_url else []) + logos + reference_images
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as http:
        downloads = asyncio.gather(*(_download_png(http, url) for url in input_urls))
        analyses = asyncio.gather(*tasks.values(), return_exceptions=True)
        inputs, analysis_results = await asyncio.gather(downloads, analyses)

    results = dict(zip(tasks, analysis_results))
    if "url" in results:
        url_brief = results["url"]
        if isinstance(url_brief, Exception):
            raise url_brief if isinstance(url_brief, ImageGenError) else ImageGenError(f"Could not analyze {reference_url}")
    # One entry per reference image, None where its analysis failed
    specs_by_image = []
    for key, value in results.items():
        if key.startswith("img"):
            if isinstance(value, Exception):
                log.warning("image analysis failed: %s", value)
                value = None
            specs_by_image.append(value)
    image_specs = [spec for spec in specs_by_image if spec is not None] + list(reference_specs or []) + list(extra_specs)

    if tasks or extra_specs or url_brief:
        progress("Writing the image prompt…")
    final_prompt = await build_prompt(user_prompt, url_brief, image_specs)
    final_prompt += input_note(edit_image_url is not None, len(logos), len(reference_images))
    final_prompt += brand_note
    final_prompt += preferences

    progress("Editing the image…" if edit_image_url else "Generating the image…")
    png = await _render(final_prompt, SIZES.get(aspect, "auto"), inputs, progress)

    uploaded, check = await asyncio.gather(
        asyncio.to_thread(cloudinary.uploader.upload, png, folder=f"nnt/generated/user-{user_id}", resource_type="image"),
        check_image(png, user_prompt, inputs[1:1 + len(logos)] if edit_image_url else inputs[:len(logos)], brand_facts),
    )
    return {
        "url": uploaded["secure_url"],
        "width": uploaded.get("width"),
        "height": uploaded.get("height"),
        "prompt": final_prompt,
        "url_brief": url_brief,
        "image_specs": image_specs,
        "specs_by_image": specs_by_image,
        "check": check,
    }


# ---------- Upload classification ----------

async def classify_upload(url: str) -> str:
    """'logo' or 'reference', so the composer can pre-tag each upload (the user can flip it)."""
    try:
        result = await _json_completion(
            prompts.UPLOAD_CLASSIFIER,
            [{"type": "text", "text": "Classify this image. Return JSON."},
             {"type": "image_url", "image_url": {"url": url, "detail": "low"}}],
            model=settings.CLASSIFY_MODEL,
        )
        return "logo" if result.get("kind") == "logo" else "reference"
    except Exception:
        log.exception("upload classification failed")
        return "reference"


async def caption_reference(url: str) -> Optional[str]:
    """A one-line description of a reference design, so the library can be searched by what it shows."""
    try:
        result = await _json_completion(
            prompts.REFERENCE_CAPTION,
            [{"type": "text", "text": "Describe this design. Return JSON."},
             {"type": "image_url", "image_url": {"url": url, "detail": "low"}}],
            model=settings.CLASSIFY_MODEL,
        )
        return (result.get("description") or "").strip()[:300] or None
    except Exception:
        log.exception("reference caption failed")
        return None


# ---------- Quality check ----------

async def check_image(png: bytes, user_prompt: str, logo_pngs: list[bytes], brand_facts: str = "") -> dict:
    """Vision QA of a generated image: is each logo intact, is the text spelled right,
    did other brands sneak in? Only flags; never blocks or regenerates."""
    def data_url(b: bytes) -> str:
        return "data:image/png;base64," + base64.b64encode(b).decode()

    facts = f"BRAND_FACTS:\n{brand_facts}\n\n" if brand_facts else ""
    content = [{"type": "text", "text": f"USER_REQUEST:\n{user_prompt[:3000]}\n\n{facts}The first {len(logo_pngs)} image(s) are the logos that must appear. The last image is the generated result."}]
    content += [{"type": "image_url", "image_url": {"url": data_url(b), "detail": "high"}} for b in logo_pngs]
    content.append({"type": "image_url", "image_url": {"url": data_url(png), "detail": "high"}})
    try:
        result = await _json_completion(prompts.IMAGE_QA, content)
        issues = [str(i) for i in result.get("issues") or []][:5]
        return {"passed": bool(result.get("passed")) and not issues, "issues": issues}
    except Exception:
        log.exception("image check failed")
        return {"passed": None, "issues": []}


# ---------- Per-logo batches with web research ----------

async def identify_brand(logo_url: str) -> dict:
    return await _json_completion(
        prompts.LOGO_READER,
        [{"type": "text", "text": "Read this logo. Return JSON."},
         {"type": "image_url", "image_url": {"url": logo_url, "detail": "high"}}],
    )


async def _fetch_web_image(http: httpx.AsyncClient, url: str) -> Optional[str]:
    """A found image as a data URL, or None if it isn't a reasonable public image."""
    try:
        _check_public_url(url)
        response = await http.get(url)
        response.raise_for_status()
        ctype = response.headers.get("content-type", "").split(";")[0]
        if ctype not in ("image/png", "image/jpeg", "image/webp") or not 5_000 < len(response.content) < 8_000_000:
            return None
        return f"data:{ctype};base64," + base64.b64encode(response.content).decode()
    except Exception:
        return None


async def research(user_prompt: str, brand: dict, taken: set[str]) -> dict:
    """Search the web for posts related to this request and brand. Returns style specs of up
    to WEB_INSPIRATION_IMAGES found images not used by another logo in the batch (`taken`),
    and the brand's own site brief if its official site shows up."""
    plan = await _json_completion(
        prompts.SEARCH_PLANNER,
        [{"type": "text", "text": f"USER_REQUEST:\n{user_prompt}\n\nBRAND:\n{json.dumps(brand, ensure_ascii=False)}"}],
        model=settings.CLASSIFY_MODEL,
    )
    query = (plan.get("query") or "").strip()[:200]
    if not query:
        return {"query": None, "specs": [], "sources": [], "site": None, "url_brief": None}

    found = await tavily.search(query, max_results=6, include_images=True)
    specs, sources = [], []
    async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 NNT-Studio"}) as http:
        for image in found.get("images") or []:
            if len(specs) >= settings.WEB_INSPIRATION_IMAGES:
                break
            url = image["url"] if isinstance(image, dict) else image
            if url in taken:
                continue
            taken.add(url)  # claimed now, so a parallel logo can't pick the same post
            data = await _fetch_web_image(http, url)
            if data is None:
                continue
            try:
                specs.append(await _json_completion(prompts.VISUAL_FORENSICS + prompts.INSPIRATION_SUFFIX, [
                    {"type": "text", "text": "Analyze this inspiration image. Return JSON."},
                    {"type": "image_url", "image_url": {"url": data, "detail": "low"}},
                ]))
                sources.append(url)
            except Exception:
                log.warning("inspiration analysis failed for %s", url)

    site, url_brief = _official_site(brand, found.get("results") or []), None
    if site:
        try:
            url_brief = await asyncio.wait_for(analyze_url(site), timeout=45)
        except Exception:
            log.warning("official site analysis failed for %s", site)
            site = None
    return {"query": query, "specs": specs, "sources": sources, "site": site, "url_brief": url_brief}


def _official_site(brand: dict, results: list[dict]) -> Optional[str]:
    """A result whose domain contains the brand's name, e.g. aceedu.com.au for 'ace Education'.
    Strict on purpose: a wrong 'official' site would put another company's look on the brand."""
    name = (brand.get("brand_name") or "").lower()
    tokens = [t for t in "".join(c if c.isalnum() else " " for c in name).split() if len(t) >= 3]
    if not tokens or (brand.get("confidence") or 0) < 0.7:
        return None
    for r in results:
        host = (urlparse(r.get("url", "")).hostname or "").removeprefix("www.")
        if host and tokens[0] in host.split(".")[0] and not any(s in host for s in ("facebook", "instagram", "linkedin", "youtube", "tiktok", "x.com", "twitter")):
            return f"https://{host}"
    return None


async def generate_for_logos(
    *,
    user_id: int,
    user_prompt: str,
    logos: list[str],
    reference_images: list[str],
    reference_url: Optional[str],
    search_web: bool,
    aspect: str,
    preferences: str,
    progress: Callable[[str], None],
    on_image: Callable[[dict], None],
    brands: Optional[dict[str, dict]] = None,
) -> list[dict]:
    """One image per logo. Each logo is handled independently (a failure only affects that
    logo), at most BATCH_CONCURRENCY at a time, and each result is reported via `on_image`
    as soon as it's ready. `brands` maps a logo URL to its client's brand kit
    ({"name", "note", "facts"}), applied to that logo's image only."""
    logos = logos[: settings.MAX_LOGOS_PER_BATCH]
    brands = brands or {}
    n = len(logos)
    gate = asyncio.Semaphore(settings.BATCH_CONCURRENCY)
    taken: set[str] = set()

    # Shared inputs are analyzed once for the whole batch
    shared_tasks = [analyze_image(u) for u in reference_images]
    if reference_url:
        shared_tasks.append(analyze_url(reference_url))
    shared = await asyncio.gather(*shared_tasks, return_exceptions=True)
    ref_specs = [s for s in shared[: len(reference_images)] if not isinstance(s, Exception)]
    shared_brief = shared[-1] if reference_url else None
    if isinstance(shared_brief, Exception):
        shared_brief = None

    async def one(index: int, logo: str) -> dict:
        tag = f"Logo {index + 1}/{n}"
        async with gate:
            brand = {}
            kit = brands.get(logo) or {}
            try:
                progress(f"{tag}: reading the logo…")
                brand = await identify_brand(logo)
                label = kit.get("name") or (brand.get("brand_name") if (brand.get("confidence") or 0) >= 0.7 else None)
                web_specs, url_brief, sources, site = [], shared_brief, [], None
                if search_web:
                    progress(f"{tag}: searching the web for related posts…")
                    found = await research(user_prompt, brand, taken)
                    web_specs, sources, site = found["specs"], found["sources"], found["site"]
                    url_brief = url_brief or found["url_brief"]
                progress(f"{tag}: generating…")
                result = await generate(
                    user_id=user_id, user_prompt=user_prompt, reference_images=reference_images,
                    reference_url=None, aspect=aspect, edit_image_url=None, logos=[logo],
                    reference_specs=ref_specs, extra_specs=web_specs,
                    url_brief=url_brief, preferences=preferences,
                    brand_note=kit.get("note", ""), brand_facts=kit.get("facts", ""),
                    progress=lambda text: progress(f"{tag}: {text[0].lower()}{text[1:]}"),
                )
                item = {
                    "index": index, "logo": logo, "label": label or f"Logo {index + 1}",
                    "url": result["url"], "width": result["width"], "height": result["height"],
                    "prompt": result["prompt"], "check": result["check"],
                    "inspiration": sources, "official_site": site, "brand_kit": bool(kit.get("note")),
                }
            except Exception as e:
                log.exception("batch item %s failed", index)
                item = {"index": index, "logo": logo, "label": brand.get("brand_name") or f"Logo {index + 1}",
                        "error": str(e) if isinstance(e, ImageGenError) else "Generation failed for this logo."}
            on_image(item)
            return item

    progress(f"Working on {n} logos…")
    return await asyncio.gather(*(one(i, logo) for i, logo in enumerate(logos)))
