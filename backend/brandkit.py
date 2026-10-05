"""Brand kits: a client's brand details (logo, colors, fonts, footer/contact details, style), extracted
once from the client's brand files (and optionally their website), editable by the user, and applied
automatically whenever the agent designs for that client.

    brand images ─┐   (logos, brand sheets; measured logo colors via Pillow)
    guideline PDFs ┼─► BRAND_KIT_EXTRACTOR (vision, JSON) ─► brand_kits.data
    website ──────┘   (screenshot, page text, footer / tel: / mailto: / social links)

Footer details are real business facts that end up printed on designs, so the extractor
only reports what is visibly written in the sources; the user reviews and edits them.
"""
import asyncio
import base64
import io
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import httpx
import trafilatura
from lxml import html as lxml_html
from PIL import Image
from pydantic import BaseModel, Field

import imagegen
import models
import prompts
from config import settings
from database import SessionLocal

log = logging.getLogger(__name__)

MAX_IMAGES = 8  # brand images sent to the extractor (logos first)
MAX_PDF_PAGES = 3  # pages of each guideline PDF, rendered as images by Cloudinary
COLOR_ROLES = ("primary", "secondary", "accent", "background", "text")
SOCIAL_HOSTS = ("facebook.com", "instagram.com", "linkedin.com", "x.com", "twitter.com", "tiktok.com", "youtube.com", "pinterest.com")


# ---------- Kit shape ----------

class KitColor(BaseModel):
    hex: str
    name: Optional[str] = None
    role: Optional[str] = None  # primary | secondary | accent | background | text


class KitSocial(BaseModel):
    platform: str
    handle: str  # @handle or URL, exactly as written


class KitFooter(BaseModel):
    phone: Optional[str] = None
    email: Optional[str] = None
    website: Optional[str] = None
    address: Optional[str] = None
    socials: list[KitSocial] = Field(default_factory=list)
    extra: Optional[str] = None  # e.g. registration number, opening hours


class KitData(BaseModel):
    brand_name: Optional[str] = None
    tagline: Optional[str] = None
    industry: Optional[str] = None
    primary_logo: Optional[str] = None  # handle, e.g. "brand:12"
    alt_logo: Optional[str] = None  # a version for dark backgrounds, if there is one
    colors: list[KitColor] = Field(default_factory=list)
    heading_font: Optional[str] = None
    body_font: Optional[str] = None
    visual_style: Optional[str] = None
    voice: Optional[str] = None
    footer: KitFooter = Field(default_factory=KitFooter)
    dos: list[str] = Field(default_factory=list)
    donts: list[str] = Field(default_factory=list)


HEX = re.compile(r"^#?([0-9a-fA-F]{6}|[0-9a-fA-F]{3})$")


def clean(data: dict, logo_handles: set[str]) -> KitData:
    """Validates extractor/user output: valid hex colors only, logo handles from this client only,
    empty strings as None."""
    def blank_to_none(v):
        if isinstance(v, str):
            v = v.strip()
            return v or None
        if isinstance(v, dict):
            return {k: blank_to_none(x) for k, x in v.items()}
        if isinstance(v, list):
            return [x for x in (blank_to_none(x) for x in v) if x is not None]
        return v

    data = blank_to_none(data or {}) or {}
    colors = []
    for c in data.get("colors") or []:
        m = HEX.match(str((c or {}).get("hex") or ""))
        if not m:
            continue
        h = m.group(1)
        h = "".join(ch * 2 for ch in h) if len(h) == 3 else h
        role = c.get("role") if c.get("role") in COLOR_ROLES else None
        colors.append({"hex": f"#{h.upper()}", "name": c.get("name"), "role": role})
    data["colors"] = colors[:10]
    footer = data.get("footer") or {}
    footer["socials"] = [s for s in footer.get("socials") or [] if isinstance(s, dict) and s.get("platform") and s.get("handle")]
    data["footer"] = footer
    for key in ("primary_logo", "alt_logo"):
        if data.get(key) not in logo_handles:
            data[key] = None
    for key in ("dos", "donts"):
        data[key] = [str(x)[:200] for x in data.get(key) or []][:8]
    return KitData.model_validate(data)


# ---------- Sources ----------

def _thumb(url: str, transform: str) -> str:
    return url.replace("/image/upload/", f"/image/upload/{transform}/", 1) if "/image/upload/" in url else url


def is_pdf(f: models.ClientFile) -> bool:
    return f.content_type == "application/pdf" or f.url.lower().endswith(".pdf")


def is_image(f: models.ClientFile) -> bool:
    return f.resource_type == "image" and (f.content_type or "").startswith("image/") and not is_pdf(f)


def measured_colors(png: bytes, n: int = 6) -> list[str]:
    """Dominant colors of a logo, measured from its pixels (vision models only estimate hex values).
    Transparent pixels are ignored."""
    img = Image.open(io.BytesIO(png)).convert("RGBA")
    img.thumbnail((200, 200))
    pixels = [p[:3] for p in img.getdata() if p[3] > 200]
    if not pixels:
        return []
    solid = Image.new("RGB", (len(pixels), 1))
    solid.putdata(pixels)
    quant = solid.quantize(colors=n, method=Image.Quantize.MEDIANCUT)
    palette = quant.getpalette()
    counts = sorted(quant.getcolors(), reverse=True)
    total = len(pixels)
    out = []
    for count, index in counts:
        if count / total < 0.02:
            continue
        r, g, b = palette[index * 3: index * 3 + 3]
        out.append(f"#{r:02X}{g:02X}{b:02X} ({count * 100 // total}%)")
    return out


def page_contacts(page_html: str, base_url: str) -> str:
    """Contact details a site states in its footer and links: footer text, tel:/mailto: links,
    social profiles. trafilatura drops footers, which is exactly where these live."""
    try:
        doc = lxml_html.fromstring(page_html)
    except Exception:
        return ""
    doc.make_links_absolute(base_url, resolve_base_href=True)
    for bad in doc.xpath("//script|//style|//noscript|//svg"):
        bad.getparent().remove(bad)
    lines = []
    footers = doc.xpath("//footer | //*[contains(@class,'footer') or contains(@id,'footer')]")
    if footers:
        text = " ".join(" ".join(footers[-1].itertext()).split())
        lines.append(f"Footer text: {text[:1500]}")
    links = {a.get("href", "").strip() for a in doc.xpath("//a[@href]")}
    tel = sorted({h[4:] for h in links if h.lower().startswith("tel:")})
    mail = sorted({h[7:].split("?")[0] for h in links if h.lower().startswith("mailto:")})
    social = sorted({h for h in links if any(s in h.lower() for s in SOCIAL_HOSTS) and "/share" not in h.lower()})
    if tel:
        lines.append("Phone links: " + ", ".join(tel[:5]))
    if mail:
        lines.append("Email links: " + ", ".join(mail[:5]))
    if social:
        lines.append("Social links: " + ", ".join(social[:10]))
    return "\n".join(lines)


async def website_sources(url: str) -> tuple[str, Optional[bytes]]:
    """Text (main content + contact details) and a screenshot of the client's website."""
    url = imagegen._check_public_url(url)

    async def fetch_text() -> str:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15, headers={"User-Agent": "Mozilla/5.0 NNT-Studio"}) as http:
            response = await http.get(url)
            response.raise_for_status()
            imagegen._check_public_url(str(response.url))  # redirects must stay on public sites
            raw = response.text[:imagegen.MAX_PAGE_BYTES]
        main = (trafilatura.extract(raw, include_comments=False, include_tables=False) or "")[:4000]
        meta = trafilatura.extract_metadata(raw)
        title = f"Title: {meta.title}\n" if meta and meta.title else ""
        return f"{title}{page_contacts(raw, str(response.url))}\n\nMain content:\n{main}".strip()

    text, shot = await asyncio.gather(fetch_text(), imagegen._screenshot(url), return_exceptions=True)
    if isinstance(text, Exception):
        log.warning("brand kit: page text failed for %s: %s", url, text)
        text = ""
    if isinstance(shot, Exception):
        log.warning("brand kit: screenshot failed for %s: %s", url, shot)
        shot = None
    if not text and shot is None:
        raise imagegen.ImageGenError(f"Could not load {url}")
    return text, shot


# ---------- Extraction ----------

async def extract(files: list[models.ClientFile], website: Optional[str]) -> tuple[KitData, list[str]]:
    """Builds a brand kit from a client's brand files (and website). Returns the kit and the
    handles of the files it read."""
    images = [f for f in files if f.category == "brand" and is_image(f)]
    images.sort(key=lambda f: (f.kind != "logo", -f.created_at.timestamp()))
    images = images[:MAX_IMAGES]
    pdfs = [f for f in files if f.category == "brand" and is_pdf(f) and f.resource_type == "image"][:2]
    if not images and not pdfs and not website:
        raise imagegen.ImageGenError("Upload a logo or brand guide first, or enter the client's website.")

    logos = [f for f in images if f.kind == "logo"] or images[:1]
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as http:
        async def logo_colors(f):
            try:
                response = await http.get(_thumb(f.url, "f_png,w_400"))
                response.raise_for_status()
                return f"brand:{f.id}: " + ", ".join(await asyncio.to_thread(measured_colors, response.content))
            except Exception:
                log.warning("brand kit: could not measure colors of %s", f.url)
                return None

        async def site():
            if not website:
                return None, None
            try:
                return await website_sources(website)
            except Exception as e:
                if not images and not pdfs:
                    raise e if isinstance(e, imagegen.ImageGenError) else imagegen.ImageGenError(f"Could not load {website}")
                log.warning("brand kit: website %s skipped: %s", website, e)
                return None, None

        measured, (site_text, site_shot) = await asyncio.gather(
            asyncio.gather(*(logo_colors(f) for f in logos[:3])), site()
        )

    content: list[dict] = []
    intro = [f"Brand images ({len(images)}), in order: " + ", ".join(
        f"brand:{f.id} ({f.kind or 'image'}, \"{f.name}\")" for f in images)] if images else ["No brand images."]
    if pdfs:
        intro.append("Then pages of brand-guideline PDF(s): " + ", ".join(f"brand:{f.id} \"{f.name}\"" for f in pdfs))
    if site_shot is not None:
        intro.append("The last image is a screenshot of the brand's website.")
    if any(measured):
        intro.append("MEASURED_LOGO_COLORS (exact, from pixels; share of the logo's area):\n" + "\n".join(m for m in measured if m))
    if site_text:
        intro.append(f"WEBSITE ({website}):\n<website_content>\n{site_text}\n</website_content>")
    content.append({"type": "text", "text": "\n\n".join(intro)})
    for f in images:
        content.append({"type": "image_url", "image_url": {"url": _thumb(f.url, "c_limit,w_1024,f_png"), "detail": "high"}})
    for f in pdfs:
        for page in range(1, MAX_PDF_PAGES + 1):
            content.append({"type": "image_url", "image_url": {"url": _thumb(f.url, f"pg_{page},c_limit,w_1400,f_jpg"), "detail": "high"}})
    if site_shot is not None:
        content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(site_shot).decode()}})

    try:
        result = await imagegen._json_completion(prompts.BRAND_KIT_EXTRACTOR, content)
    except Exception as e:
        # A PDF shorter than MAX_PDF_PAGES makes its later page URLs fail; retry with page 1 only
        if not pdfs:
            raise
        log.warning("brand kit extraction failed with all PDF pages, retrying with first pages: %s", e)
        content = [p for p in content if not re.search(r"/pg_[2-9],", p.get("image_url", {}).get("url", ""))]
        result = await imagegen._json_completion(prompts.BRAND_KIT_EXTRACTOR, content)

    handles = {f"brand:{f.id}" for f in images}
    kit = clean(result, handles)
    if kit.primary_logo is None and logos:
        kit.primary_logo = f"brand:{logos[0].id}"
    return kit, [f"brand:{f.id}" for f in images + pdfs]


# ---------- Using a kit ----------

def get(client_id: int, user_id: int) -> Optional[models.BrandKit]:
    with SessionLocal() as db:
        row = db.query(models.BrandKit).filter_by(client_id=client_id, user_id=user_id).first()
        if row is not None:
            db.expunge(row)
        return row


def footer_line(kit: KitData) -> str:
    f = kit.footer
    parts = [f.phone, f.email, f.website, f.address]
    parts += [s.handle for s in f.socials[:3]]
    if f.extra:
        parts.append(f.extra)
    return "  |  ".join(p for p in parts if p)


def approved_text(kit: KitData) -> str:
    """The brand's real details, so the image QA doesn't flag them as invented copy."""
    bits = [f"Brand name: {kit.brand_name}" if kit.brand_name else "", f"Tagline: {kit.tagline}" if kit.tagline else ""]
    line = footer_line(kit)
    if line:
        bits.append(f"Contact/footer details: {line}")
    return "\n".join(b for b in bits if b)


def prompt_note(kit: KitData, include_footer: bool) -> str:
    """The brand-kit section appended to the image prompt (after the user's verbatim words)."""
    lines = []
    if kit.colors:
        lines.append("Colors: " + ", ".join(f"{c.hex}{f' {c.role}' if c.role else ''}{f' ({c.name})' if c.name else ''}" for c in kit.colors))
    fonts = "; ".join(p for p in (f"headings {kit.heading_font}" if kit.heading_font else "", f"body {kit.body_font}" if kit.body_font else "") if p)
    if fonts:
        lines.append(f"Typography: {fonts}")
    if kit.visual_style:
        lines.append(f"Visual style: {kit.visual_style}")
    if kit.donts:
        lines.append("Brand don'ts: " + "; ".join(kit.donts))
    if kit.tagline:
        lines.append(f'Slogan: if the design has a slogan, use only the brand tagline, spelled exactly: "{kit.tagline}". Never write any other slogan.')
    else:
        lines.append("Slogan: don't invent a slogan or tagline.")
    footer = footer_line(kit) if include_footer else ""
    if footer:
        lines.append(
            f'Footer: a clean, slim footer band along the bottom edge in the brand colors with exactly this text, spelled exactly: "{footer}". '
            "Add no other contact details anywhere."
        )
    if not lines:
        return ""
    name = f" for {kit.brand_name}" if kit.brand_name else ""
    return f"\n\nBrand kit{name} (follow it wherever the request above doesn't say otherwise):\n" + "\n".join(f"- {l}" for l in lines)


def summary(kit: KitData, row: models.BrandKit) -> str:
    """The kit as the agent sees it in open_client."""
    lines = [f"Brand kit (extracted {row.extracted_at:%Y-%m-%d}{', edited by the user' if row.edited else ''}):"]
    for label, value in (
        ("Brand name", kit.brand_name), ("Tagline", kit.tagline), ("Industry", kit.industry),
        ("Primary logo", kit.primary_logo), ("Logo for dark backgrounds", kit.alt_logo),
        ("Colors", ", ".join(f"{c.hex} {c.role or ''} {c.name or ''}".strip() for c in kit.colors)),
        ("Heading font", kit.heading_font), ("Body font", kit.body_font),
        ("Visual style", kit.visual_style), ("Voice", kit.voice),
        ("Footer", footer_line(kit)),
        ("Do", "; ".join(kit.dos)), ("Don't", "; ".join(kit.donts)),
    ):
        if value:
            lines.append(f"- {label}: {value}")
    return "\n".join(lines)


def kits_for_logo_urls(user_id: int, urls: list[str]) -> dict[str, KitData]:
    """Brand kit of the client each library logo URL belongs to."""
    if not urls:
        return {}
    with SessionLocal() as db:
        rows = (
            db.query(models.ClientFile.url, models.BrandKit.data)
            .join(models.BrandKit, models.BrandKit.client_id == models.ClientFile.client_id)
            .filter(models.ClientFile.user_id == user_id, models.ClientFile.url.in_(urls))
            .all()
        )
    return {url: KitData.model_validate(data) for url, data in rows}


def save(client_id: int, user_id: int, kit: KitData, sources: list[str], website: Optional[str], edited: bool) -> models.BrandKit:
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        row = db.query(models.BrandKit).filter_by(client_id=client_id, user_id=user_id).first()
        if row is None:
            row = models.BrandKit(client_id=client_id, user_id=user_id, extracted_at=now)
            db.add(row)
        row.data = kit.model_dump()
        row.website = website
        row.edited = edited
        row.updated_at = now
        if not edited:
            row.sources = sources
            row.extracted_at = now
        db.commit()
        db.refresh(row)
        db.expunge(row)
        return row
