"""Client library (master files and brand assets per client) and the shared reference library."""
import asyncio
import logging
import os
import re
from datetime import datetime
from html import unescape
from typing import Literal
from urllib.parse import unquote, urljoin, urlparse

import httpx

import cloudinary.uploader
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import auth, brandkit, imagegen, models
from config import settings
from database import get_db

router = APIRouter(prefix="/api", tags=["clients"])

Category = Literal["master", "brand"]
CATEGORIES = ("master", "brand")

IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif", "image/svg+xml"}
# Brand assets are logos and images, plus brand-guideline PDFs. Master files can be anything
# (PSD, AI, PDF, ZIP, fonts…). References are images only, since they're fed to the image model.
ALLOWED_TYPES = {
    "brand": IMAGE_TYPES | {"application/pdf"},
}
REFERENCE_TYPES = IMAGE_TYPES - {"image/svg+xml"}
MAX_BYTES = {
    "master": settings.MAX_MASTER_FILE_BYTES,
    "brand": settings.MAX_IMAGE_BYTES,
}


class ClientIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)

    @field_validator("name")
    @classmethod
    def strip(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("Name is required")
        return v


class StoredFileOut(BaseModel):
    id: int
    name: str
    url: str
    resource_type: str
    content_type: str | None
    bytes: int | None
    width: int | None
    height: int | None
    created_at: datetime

    class Config:
        from_attributes = True


class FileOut(StoredFileOut):
    client_id: int
    category: Category
    kind: str | None = None


class FileKindIn(BaseModel):
    kind: Literal["logo", "asset"]


class ReferenceOut(StoredFileOut):
    collection: str | None = None
    source_url: str | None = None


class UrlIn(BaseModel):
    url: str = Field(min_length=4, max_length=2048)


class ClientOut(BaseModel):
    id: int
    name: str
    created_at: datetime
    counts: dict[str, int]
    cover_url: str | None = None  # latest brand image, shown on the client card


class BrandKitOut(BaseModel):
    data: brandkit.KitData
    website: str | None
    sources: list[str]
    edited: bool
    extracted_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ExtractIn(BaseModel):
    website: str | None = Field(default=None, max_length=2048)


class BrandKitIn(BaseModel):
    data: dict
    website: str | None = Field(default=None, max_length=2048)


class ClientDetail(ClientOut):
    files: list[FileOut]
    brand_kit: BrandKitOut | None = None


def get_client(db: Session, user: models.User, client_id: int) -> models.Client:
    client = db.query(models.Client).filter_by(id=client_id, user_id=user.id).first()
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")
    return client


def counts_by_client(db: Session, user_id: int) -> dict[int, dict[str, int]]:
    rows = (
        db.query(models.ClientFile.client_id, models.ClientFile.category, func.count())
        .filter(models.ClientFile.user_id == user_id)
        .group_by(models.ClientFile.client_id, models.ClientFile.category)
        .all()
    )
    out: dict[int, dict[str, int]] = {}
    for client_id, category, n in rows:
        out.setdefault(client_id, dict.fromkeys(CATEGORIES, 0))[category] = n
    return out


def is_image(f: models.ClientFile) -> bool:
    return f.resource_type == "image" and (f.content_type or "").startswith("image/")


def client_out(client: models.Client, counts: dict[str, int], cover_url: str | None) -> dict:
    return {
        "id": client.id,
        "name": client.name,
        "created_at": client.created_at,
        "counts": counts or dict.fromkeys(CATEGORIES, 0),
        "cover_url": cover_url,
    }


@router.get("/clients", response_model=list[ClientOut])
def list_clients(user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    clients = db.query(models.Client).filter_by(user_id=user.id).order_by(models.Client.name).all()
    counts = counts_by_client(db, user.id)
    covers: dict[int, str] = {}
    brand_images = (
        db.query(models.ClientFile)
        .filter_by(user_id=user.id, category="brand", resource_type="image")
        .order_by(models.ClientFile.created_at.desc())
        .all()
    )
    for f in brand_images:
        if is_image(f):
            covers.setdefault(f.client_id, f.url)
    return [client_out(c, counts.get(c.id), covers.get(c.id)) for c in clients]


@router.post("/clients", response_model=ClientOut, status_code=201)
def create_client(body: ClientIn, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    client = models.Client(user_id=user.id, name=body.name)
    db.add(client)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail=f"You already have a client named “{body.name}”")
    db.refresh(client)
    return client_out(client, None, None)


@router.get("/clients/{client_id}", response_model=ClientDetail)
def read_client(client_id: int, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    client = get_client(db, user, client_id)
    files = sorted(client.files, key=lambda f: f.created_at, reverse=True)
    cover = next((f.url for f in files if f.category == "brand" and is_image(f)), None)
    kit = db.query(models.BrandKit).filter_by(client_id=client.id, user_id=user.id).first()
    return {**client_out(client, counts_by_client(db, user.id).get(client.id), cover), "files": files, "brand_kit": kit}


def logo_handles(client: models.Client) -> set[str]:
    return {f"brand:{f.id}" for f in client.files if f.category == "brand" and brandkit.is_image(f)}


def clean_website(raw: str | None) -> str | None:
    url = (raw or "").strip()
    if not url:
        return None
    if not re.match(r"^https?://", url, re.I):
        url = f"https://{url}"
    if re.search(r"\s", url) or "." not in url[8:]:
        raise HTTPException(status_code=400, detail="Enter a web address, like https://example.com")
    return url


@router.post("/clients/{client_id}/brand-kit/extract", response_model=BrandKitOut)
async def extract_brand_kit(client_id: int, body: ExtractIn, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """Reads the client's brand files (and website, if given) and extracts their brand kit:
    logo, colors, fonts, footer/contact details, style. Replaces any earlier kit."""
    client = get_client(db, user, client_id)
    website = clean_website(body.website)
    files = list(client.files)
    try:
        kit, sources = await brandkit.extract(files, website)
    except imagegen.ImageGenError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        logging.exception("brand kit extraction failed")
        raise HTTPException(status_code=502, detail="Could not read the brand files right now. Try again in a moment.")
    return await asyncio.to_thread(brandkit.save, client.id, user.id, kit, sources, website, False)


@router.put("/clients/{client_id}/brand-kit", response_model=BrandKitOut)
def save_brand_kit(client_id: int, body: BrandKitIn, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """Saves the user's corrections to a brand kit."""
    client = get_client(db, user, client_id)
    existing = db.query(models.BrandKit).filter_by(client_id=client.id, user_id=user.id).first()
    try:
        kit = brandkit.clean(body.data, logo_handles(client))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid brand kit: {e}")
    return brandkit.save(client.id, user.id, kit, existing.sources if existing else [], clean_website(body.website), True)


@router.delete("/clients/{client_id}/brand-kit", status_code=204)
def delete_brand_kit(client_id: int, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    client = get_client(db, user, client_id)
    db.query(models.BrandKit).filter_by(client_id=client.id, user_id=user.id).delete()
    db.commit()


@router.patch("/clients/{client_id}", response_model=ClientOut)
def rename_client(client_id: int, body: ClientIn, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    client = get_client(db, user, client_id)
    client.name = body.name
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail=f"You already have a client named “{body.name}”")
    return client_out(client, counts_by_client(db, user.id).get(client.id), None)


@router.delete("/clients/{client_id}", status_code=204)
async def delete_client(client_id: int, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    client = get_client(db, user, client_id)
    stored = [(f.public_id, f.resource_type) for f in client.files]
    db.delete(client)
    db.commit()
    await asyncio.gather(*(destroy(pid, rtype) for pid, rtype in stored))


async def store_upload(file: UploadFile, allowed: set[str] | None, kind: str, limit: int, folder: str) -> dict:
    """Validates an upload and puts it on Cloudinary; returns the columns a file row needs."""
    if not settings.CLOUDINARY_CLOUD_NAME:
        raise HTTPException(status_code=503, detail="File uploads are not configured")
    content_type = file.content_type or "application/octet-stream"
    if allowed is not None and content_type not in allowed:
        raise HTTPException(status_code=400, detail=f"{kind} must be {'an image' if 'application/pdf' not in allowed else 'an image or PDF'}")
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status_code=413, detail=f"File is larger than {limit // (1024 * 1024)} MB")
    if not data:
        raise HTTPException(status_code=400, detail="File is empty")
    return await put_on_cloudinary(data, (file.filename or "file").strip()[:200], content_type, folder)


async def put_on_cloudinary(data: bytes, name: str, content_type: str, folder: str) -> dict:
    result = await asyncio.to_thread(
        cloudinary.uploader.upload,
        data,
        folder=folder,
        resource_type="auto",  # images stay images; PSD/AI/ZIP etc. are stored as raw files
        filename=name,
        use_filename=True,  # keeps the extension on raw files, so downloads open correctly
        unique_filename=True,
    )
    return {
        "name": name,
        "url": result["secure_url"],
        "public_id": result["public_id"],
        "resource_type": result.get("resource_type", "raw"),
        "content_type": content_type,
        "bytes": result.get("bytes", len(data)),
        "width": result.get("width"),
        "height": result.get("height"),
    }


@router.post("/clients/{client_id}/files", response_model=FileOut, status_code=201)
async def upload_client_file(
    client_id: int,
    category: Category = Query(...),
    file: UploadFile = File(...),
    user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    client = get_client(db, user, client_id)
    stored = await store_upload(
        file,
        ALLOWED_TYPES.get(category),
        "Brand files" if category == "brand" else "Master files",
        MAX_BYTES[category],
        f"nnt/clients/user-{user.id}/client-{client.id}/{category}",
    )
    row = models.ClientFile(client_id=client.id, user_id=user.id, category=category, kind=await file_kind(category, stored), **stored)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


async def file_kind(category: str, stored: dict) -> str:
    """What a client file is, so the agent knows which brand file is the logo."""
    image = stored["resource_type"] == "image" and (stored["content_type"] or "").startswith("image/")
    if category == "master":
        return "image" if image else ("document" if stored["content_type"] == "application/pdf" else "other")
    if not image:
        return "document"  # brand-guideline PDF
    return "logo" if await imagegen.classify_upload(stored["url"]) == "logo" else "asset"


@router.patch("/clients/{client_id}/files/{file_id}", response_model=FileOut)
def set_file_kind(client_id: int, file_id: int, body: FileKindIn, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """Lets the user correct the logo/asset tag of a brand image."""
    row = db.query(models.ClientFile).filter_by(id=file_id, client_id=client_id, user_id=user.id, category="brand").first()
    if row is None:
        raise HTTPException(status_code=404, detail="Brand file not found")
    if row.kind == "document":
        raise HTTPException(status_code=400, detail="Only brand images can be tagged as a logo")
    row.kind = body.kind
    db.commit()
    db.refresh(row)
    return row


@router.delete("/clients/{client_id}/files/{file_id}", status_code=204)
async def delete_client_file(client_id: int, file_id: int, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    row = db.query(models.ClientFile).filter_by(id=file_id, client_id=client_id, user_id=user.id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="File not found")
    public_id, resource_type = row.public_id, row.resource_type
    # A brand kit must not point at a logo that no longer exists
    kit = db.query(models.BrandKit).filter_by(client_id=client_id, user_id=user.id).first()
    if kit is not None:
        data = dict(kit.data)
        for key in ("primary_logo", "alt_logo"):
            if data.get(key) == f"brand:{row.id}":
                data[key] = None
        kit.data = data
    db.delete(row)
    db.commit()
    await destroy(public_id, resource_type)


@router.get("/references", response_model=list[ReferenceOut])
def list_references(user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """The shared reference library: one pool of reference designs, usable for every client."""
    return (
        db.query(models.Reference)
        .filter_by(user_id=user.id)
        .order_by(models.Reference.created_at.desc())
        .all()
    )


@router.post("/references", response_model=ReferenceOut, status_code=201)
async def upload_reference(
    file: UploadFile = File(...),
    user: models.User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    stored = await store_upload(file, REFERENCE_TYPES, "References", settings.MAX_IMAGE_BYTES, reference_folder(user))
    stored["description"] = await imagegen.caption_reference(stored["url"])
    return save_reference(db, user, stored)


@router.post("/references/from-url", response_model=ReferenceOut, status_code=201)
async def add_reference_from_url(body: UrlIn, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """Adds a reference from a link: a direct image link, or a web page (its preview image is used)."""
    if not settings.CLOUDINARY_CLOUD_NAME:
        raise HTTPException(status_code=503, detail="File uploads are not configured")
    try:
        data, content_type, name = await fetch_image(body.url.strip())
    except imagegen.ImageGenError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except httpx.HTTPError:
        raise HTTPException(status_code=400, detail="Could not download that link")
    stored = await put_on_cloudinary(data, name, content_type, reference_folder(user))
    stored["description"] = await imagegen.caption_reference(stored["url"])
    return save_reference(db, user, stored)


def reference_folder(user: models.User) -> str:
    return f"nnt/references/user-{user.id}/library"


def save_reference(db: Session, user: models.User, stored: dict) -> models.Reference:
    row = models.Reference(user_id=user.id, **stored)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# The page's own preview image, as social sites (Pinterest, Behance, Dribbble, Instagram…) declare it
PREVIEW_IMAGE = re.compile(
    r"""<meta[^>]+(?:property|name)=["'](?:og:image(?::secure_url)?|twitter:image(?::src)?)["'][^>]*>""", re.I
)
CONTENT_ATTR = re.compile(r"""content=["']([^"']+)["']""", re.I)
MAX_PAGE_BYTES = 2 * 1024 * 1024


async def get_public(http: httpx.AsyncClient, url: str, limit: int) -> tuple[str, str, bytes]:
    """GET that re-checks every redirect hop is a public site, and stops reading past `limit` bytes.
    Returns (final url, content type, body)."""
    for _ in range(5):
        url = imagegen._check_public_url(url)
        async with http.stream("GET", url) as response:
            if response.is_redirect:
                url = urljoin(url, response.headers.get("location", ""))
                continue
            response.raise_for_status()
            body = b""
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > limit:
                    raise HTTPException(status_code=413, detail=f"Image is larger than {settings.MAX_IMAGE_BYTES // (1024 * 1024)} MB")
            content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
            return url, content_type, body
    raise HTTPException(status_code=400, detail="That link redirects too many times")


async def fetch_image(url: str) -> tuple[bytes, str, str]:
    """Downloads an image from a link. A web page link resolves to its preview (og:image) image."""
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh) NNT-Studio", "Accept": "image/*,text/html;q=0.9,*/*;q=0.5"}
    async with httpx.AsyncClient(timeout=15, follow_redirects=False, headers=headers) as http:
        final_url, content_type, body = await get_public(http, url, max(settings.MAX_IMAGE_BYTES, MAX_PAGE_BYTES))

        if content_type in ("text/html", "application/xhtml+xml"):
            html = body[:MAX_PAGE_BYTES].decode("utf-8", errors="replace")
            tag = PREVIEW_IMAGE.search(html)
            src = tag and CONTENT_ATTR.search(tag.group(0))
            if not src:
                raise HTTPException(status_code=400, detail="That page has no preview image. Open the image itself and copy its address.")
            image_url = urljoin(final_url, unescape(src.group(1)))
            final_url, content_type, body = await get_public(http, image_url, settings.MAX_IMAGE_BYTES)

    if content_type not in REFERENCE_TYPES:
        raise HTTPException(status_code=400, detail="That link isn't a PNG, JPEG, WebP or GIF image")
    if len(body) > settings.MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail=f"Image is larger than {settings.MAX_IMAGE_BYTES // (1024 * 1024)} MB")
    name = os.path.basename(unquote(urlparse(final_url).path)) or "reference"
    if "." not in name:
        name += "." + content_type.split("/")[1].replace("jpeg", "jpg")
    return body, content_type, name[:200]


@router.delete("/references/{reference_id}", status_code=204)
async def delete_reference(reference_id: int, user: models.User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    row = db.query(models.Reference).filter_by(id=reference_id, user_id=user.id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Reference not found")
    public_id, resource_type = row.public_id, row.resource_type
    db.delete(row)
    db.commit()
    await destroy(public_id, resource_type)


async def destroy(public_id: str, resource_type: str) -> None:
    """Best-effort removal from Cloudinary; the DB row is already gone either way."""
    try:
        await asyncio.to_thread(cloudinary.uploader.destroy, public_id, resource_type=resource_type, invalidate=True)
    except Exception:
        logging.warning("could not delete %s from Cloudinary", public_id, exc_info=True)
