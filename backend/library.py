"""Lets the agent browse the user's library: clients (brand files, master files) and references.

Every library item has a short handle the agent can pass around:
    brand:<id>   a client's brand file (logo, brand asset, guideline PDF)
    master:<id>  a client's master file (PSD, AI, PDF, final export…)
    ref:<id>     a design in the shared reference library
Handles are resolved per user, so the agent can never reach another account's files.
"""
import asyncio
import difflib
import logging
from typing import Optional

import numpy as np

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from sqlalchemy import func

import brandkit
import imagegen
import memory
import models
import prompts
from config import settings
from database import SessionLocal

log = logging.getLogger(__name__)

MAX_LOOK = 6
MIN_MEANING_SCORE = 0.25  # cosine similarity of query vs. a reference's title + description


_query_vectors: dict[str, list[float]] = {}


async def query_vector(query: str) -> list[float]:
    """Embedding of a search query, cached: @-mention suggestions search on every keystroke."""
    key = " ".join(query.lower().split())
    if key not in _query_vectors:
        if len(_query_vectors) >= 500:
            _query_vectors.pop(next(iter(_query_vectors)))
        _query_vectors[key] = await memory.embeddings.aembed_query(key)
    return _query_vectors[key]


def reference_text(r: models.Reference) -> str:
    return ". ".join(t for t in (r.name, r.description, r.collection) if t)


async def rank_references(rows: list[models.Reference], query: str) -> list[models.Reference]:
    """Ranks by meaning (so "Dashain" finds "Vijaya Dashami") plus exact word hits.
    Embeddings are computed the first time a reference is searched, then stored."""
    words = [w for w in query.lower().split() if len(w) > 1]
    missing = [r for r in rows if r.embedding is None]
    if missing:
        vectors = await memory.embeddings.aembed_documents([reference_text(r) for r in missing])

        def store():
            with SessionLocal() as db:
                for r, v in zip(missing, vectors):
                    db.query(models.Reference).filter_by(id=r.id).update({"embedding": v})
                db.commit()
        await asyncio.to_thread(store)
        for r, v in zip(missing, vectors):
            r.embedding = v
    query_vec = np.asarray(await query_vector(query), dtype=np.float32)
    matrix = np.asarray([r.embedding for r in rows], dtype=np.float32)
    meaning = matrix @ query_vec  # unit-length vectors: dot product = cosine similarity
    scored = []
    for r, m in zip(rows, meaning):
        text = f"{reference_text(r)} {r.source_url or ''}".lower()
        hits = sum(w in text for w in words)
        if m >= MIN_MEANING_SCORE or hits:
            scored.append((float(m) + 0.05 * hits, r))
    return [r for _, r in sorted(scored, key=lambda x: x[0], reverse=True)]


def handle(item) -> str:
    if isinstance(item, models.Reference):
        return f"ref:{item.id}"
    return f"{item.category}:{item.id}"


def is_image(item) -> bool:
    return item.resource_type == "image" and (item.content_type or "").startswith("image/") and not item.url.lower().endswith(".pdf")


def size_text(item) -> str:
    parts = []
    if item.width and item.height:
        parts.append(f"{item.width}×{item.height}")
    if item.bytes:
        parts.append(f"{item.bytes / 1024:.0f} KB" if item.bytes < 1024 * 1024 else f"{item.bytes / 1024 / 1024:.1f} MB")
    return ", ".join(parts)


def overview(user_id: int) -> str:
    """A compact index of the library for the system prompt, so the agent knows what exists."""
    with SessionLocal() as db:
        clients = db.query(models.Client).filter_by(user_id=user_id).order_by(models.Client.name).all()
        counts = dict(
            ((cid, cat), n) for cid, cat, n in
            db.query(models.ClientFile.client_id, models.ClientFile.category, func.count())
            .filter(models.ClientFile.user_id == user_id)
            .group_by(models.ClientFile.client_id, models.ClientFile.category)
        )
        logos = {cid for (cid,) in db.query(models.ClientFile.client_id).filter_by(user_id=user_id, category="brand", kind="logo")}
        kits = {cid for (cid,) in db.query(models.BrandKit.client_id).filter_by(user_id=user_id)}
        collections = (
            db.query(models.Reference.collection, func.count())
            .filter(models.Reference.user_id == user_id)
            .group_by(models.Reference.collection)
            .order_by(func.max(models.Reference.created_at).desc())
            .all()
        )
    if not clients and not collections:
        return "The library is empty: no clients and no references yet."
    lines = []
    if clients:
        lines.append(f"Clients ({len(clients)}):")
        for c in clients[:40]:
            brand, master = counts.get((c.id, "brand"), 0), counts.get((c.id, "master"), 0)
            lines.append(
                f"- {c.name} (client {c.id}): {brand} brand file(s){', has a logo' if c.id in logos else ', NO logo tagged'}, "
                f"{master} master file(s), {'brand kit ready' if c.id in kits else 'no brand kit'}"
            )
        if len(clients) > 40:
            lines.append(f"- …and {len(clients) - 40} more (use find_clients)")
    else:
        lines.append("Clients: none yet.")
    if collections:
        total = sum(n for _, n in collections)
        lines.append(f"References ({total}, shared by all clients):")
        for name, n in collections[:15]:
            lines.append(f"- {name or 'Uploaded by the user'}: {n}")
    else:
        lines.append("References: none yet.")
    return "\n".join(lines)


def _match_clients(db, user_id: int, query: str) -> list[models.Client]:
    clients = db.query(models.Client).filter_by(user_id=user_id).order_by(models.Client.name).all()
    q = query.strip().lower()
    if not q:
        return clients
    if q.isdigit():
        return [c for c in clients if c.id == int(q)]
    exact = [c for c in clients if q in c.name.lower() or c.name.lower() in q]
    if exact:
        return exact
    names = {c.name.lower(): c for c in clients}
    return [names[n] for n in difflib.get_close_matches(q, names, n=3, cutoff=0.5)]


def describe_file(f: models.ClientFile) -> str:
    bits = [f"[{handle(f)}]", f.kind or ("image" if is_image(f) else "file"), f'"{f.name}"']
    if size_text(f):
        bits.append(size_text(f))
    return " ".join(bits)


def resolve(user_id: int, handles: list[str]) -> tuple[list, list[str]]:
    """Library rows for the given handles (only the user's own), plus the handles not found."""
    found, missing = [], []
    with SessionLocal() as db:
        for h in handles:
            kind, _, raw = h.strip().partition(":")
            row = None
            if raw.isdigit():
                if kind == "ref":
                    row = db.query(models.Reference).filter_by(id=int(raw), user_id=user_id).first()
                elif kind in ("brand", "master"):
                    row = db.query(models.ClientFile).filter_by(id=int(raw), user_id=user_id, category=kind).first()
            if row is None:
                missing.append(h)
            else:
                db.expunge(row)
                found.append(row)
    return found, missing


def client_kit(user_id: int, query: str) -> tuple[Optional[str], Optional["brandkit.KitData"], Optional[str]]:
    """(client name, its brand kit or None, error) for a client named in a generate_image call."""
    with SessionLocal() as db:
        matches = _match_clients(db, user_id, query)
        if len(matches) != 1:
            names = ", ".join(c.name for c in matches)
            return None, None, (f"\"{query}\" matches several clients: {names}." if matches else f"No client matches \"{query}\".")
        c = matches[0]
        kit = db.query(models.BrandKit).filter_by(client_id=c.id, user_id=user_id).first()
        return c.name, (brandkit.KitData.model_validate(kit.data) if kit else None), None


def image_urls(user_id: int, handles: list[str]) -> tuple[list[str], list[str]]:
    """Image URLs for handles; non-images and unknown handles are returned as problems."""
    rows, missing = resolve(user_id, handles)
    urls, problems = [], [f"{h}: not found" for h in missing]
    for r in rows:
        if is_image(r):
            urls.append(r.url)
        else:
            problems.append(f"{handle(r)}: not an image ({r.name})")
    return urls, problems


# ---------- @-mentions ----------

MAX_MENTION_REFS = 30  # references one @topic mention can bring


def _load_references(user_id: int) -> list[models.Reference]:
    with SessionLocal() as db:
        rows = db.query(models.Reference).filter_by(user_id=user_id).order_by(models.Reference.created_at.desc()).all()
        for r in rows:
            db.expunge(r)
        return rows


_topic_terms: dict[str, list[str]] = {}


async def topic_terms(topic: str) -> list[str]:
    """Names and spellings that identify a topic ("dashain" → "vijaya dashami", "dasain", "दशैं"…), cached.
    Keywords instead of embedding similarity: embeddings rank Tihar designs as close to "dashain" as Dashain ones."""
    key = " ".join(topic.lower().split())
    if key not in _topic_terms:
        terms = [key]
        try:
            result = await imagegen._json_completion(
                prompts.TOPIC_TERMS, [{"type": "text", "text": f"TOPIC: {key}"}], model=settings.CLASSIFY_MODEL)
            terms += [str(t).lower().strip() for t in result.get("terms") or [] if len(str(t).strip()) >= 3]
        except Exception:
            log.exception("topic terms failed for %r", key)
            return terms  # not cached, so a later call retries
        if len(_topic_terms) >= 500:
            _topic_terms.pop(next(iter(_topic_terms)))
        _topic_terms[key] = list(dict.fromkeys(terms))[:16]
    return _topic_terms[key]


async def references_for_topic(user_id: int, query: str) -> list[models.Reference]:
    """All references about a topic: in a collection with that name, or whose title/caption names the topic."""
    q = query.strip().lower()
    if not q:
        return []
    rows = await asyncio.to_thread(_load_references, user_id)
    if not rows:
        return []
    terms = await topic_terms(q)
    out = []
    for r in rows:
        text = reference_text(r).lower()
        if (r.collection and q in r.collection.lower()) or any(t in text for t in terms):
            out.append(r)
    return out[:MAX_MENTION_REFS]


def _thumb(url: str) -> str:
    return url.replace("/image/upload/", "/image/upload/c_fill,w_96,h_96,f_auto,q_auto/", 1)


async def suggest_mentions(user_id: int, query: str, kind: str = "all") -> dict:
    """What an @ in the composer can point at: clients, and reference topics (with how many designs each brings).
    `kind` ("clients" | "references" | "all") lets the composer show clients at once while topics are looked up."""
    q = query.strip()

    def load_clients():
        with SessionLocal() as db:
            clients = _match_clients(db, user_id, q)[:6] if q else (
                db.query(models.Client).filter_by(user_id=user_id).order_by(models.Client.name).limit(6).all())
            ids = [c.id for c in clients]
            logos = {}
            for f in db.query(models.ClientFile).filter(models.ClientFile.client_id.in_(ids), models.ClientFile.kind == "logo"):
                logos.setdefault(f.client_id, f.url)
            kits = {cid for (cid,) in db.query(models.BrandKit.client_id).filter(models.BrandKit.client_id.in_(ids))}
            return [{"type": "client", "id": c.id, "label": c.name, "logo": _thumb(logos[c.id]) if c.id in logos else None,
                     "has_kit": c.id in kits} for c in clients]

    clients = await asyncio.to_thread(load_clients) if kind in ("clients", "all") else []
    if kind == "clients":
        return {"clients": clients, "references": []}
    rows = await asyncio.to_thread(_load_references, user_id)
    collections: dict[str, list[models.Reference]] = {}
    for r in rows:
        if r.collection and (not q or q.lower() in r.collection.lower()):
            collections.setdefault(r.collection, []).append(r)
    topics = []
    if len(q) >= 3:
        matched = await references_for_topic(user_id, q)
        if matched:
            topics.append({"type": "references", "label": q, "query": q, "count": len(matched),
                           "thumbs": [_thumb(r.url) for r in matched[:4]]})
    for name, refs in list(collections.items())[:5]:
        if name.lower() != q.lower():
            topics.append({"type": "references", "label": name, "query": name, "count": len(refs),
                           "thumbs": [_thumb(r.url) for r in refs[:4]]})
    return {"clients": clients, "references": topics}


MENTIONS_MARK = "[mentions]"


async def describe_mentions(user_id: int, mentions: list[dict]) -> tuple[str, list[str]]:
    """The note the agent gets about what the user @-mentioned, resolved per user, and the mention labels."""
    lines, labels = [], []
    for m in mentions[:6]:
        label = m.get("label", "").strip()
        if m.get("type") == "client":
            def load(cid=m.get("id")):
                with SessionLocal() as db:
                    c = db.query(models.Client).filter_by(id=cid, user_id=user_id).first()
                    if c is None:
                        return None
                    logos = [f"brand:{f.id}" for f in c.files if f.kind == "logo"]
                    kit = db.query(models.BrandKit).filter_by(client_id=c.id, user_id=user_id).first()
                    return c.name, logos, kit is not None
            found = await asyncio.to_thread(load)
            if found is None:
                continue
            name, logos, has_kit = found
            lines.append(
                f'- @{label} = client "{name}". Design for it with generate_image(client="{name}")'
                + ("; its brand kit (logo, colors, fonts, footer) is applied automatically." if has_kit
                   else f"; no brand kit, logo handle(s): {', '.join(logos) or 'none tagged'} (open_client for details).")
            )
        elif m.get("type") == "references":
            refs = await references_for_topic(user_id, m.get("query") or label)
            if not refs:
                lines.append(f'- @{label} = reference topic "{label}": no matching references in the library.')
                continue
            listed = "; ".join(f'ref:{r.id} "{r.description or r.name}"' for r in refs)
            lines.append(f'- @{label} = {len(refs)} reference(s) about "{label}": {listed}')
        else:
            continue
        labels.append(label)
    if not lines:
        return "", []
    note = (
        f"{MENTIONS_MARK} The user tagged these with @ (already resolved; no need to search for them):\n"
        + "\n".join(lines)
    )
    return note, labels


# ---------- Tools ----------

@tool
async def find_clients(config: RunnableConfig, query: str = "") -> str:
    """List the user's clients (all, or those matching a name), with their logos and file counts.

    Args:
        query: Part of a client name; empty for all clients.
    """
    user_id = config["configurable"]["user_id"]

    def run():
        with SessionLocal() as db:
            matches = _match_clients(db, user_id, query)
            out = []
            for c in matches[:30]:
                files = db.query(models.ClientFile).filter_by(client_id=c.id, user_id=user_id).all()
                logos = [f for f in files if f.kind == "logo"]
                brand = sum(f.category == "brand" for f in files)
                master = sum(f.category == "master" for f in files)
                logo_text = ", ".join(f"[{handle(f)}] {f.name}" for f in logos) or "none tagged"
                out.append(f"- {c.name} (client {c.id}): logo {logo_text}; {brand} brand file(s), {master} master file(s)")
            return out, len(matches)

    out, total = await asyncio.to_thread(run)
    if not out:
        return f"No client matches \"{query}\"." if query else "The user has no clients yet."
    more = f"\n…{total - 30} more; narrow the query." if total > 30 else ""
    return "\n".join(out) + more


@tool
async def open_client(client: str, config: RunnableConfig) -> str:
    """Show one client's brand files (logos, brand assets, guidelines) and master files, each with a handle.

    Args:
        client: The client's name (or part of it) or id.
    """
    user_id = config["configurable"]["user_id"]

    def run():
        with SessionLocal() as db:
            matches = _match_clients(db, user_id, client)
            if len(matches) != 1:
                return None, [c.name for c in matches]
            c = matches[0]
            files = (
                db.query(models.ClientFile).filter_by(client_id=c.id, user_id=user_id)
                .order_by(models.ClientFile.category, models.ClientFile.created_at.desc()).all()
            )
            brand = [describe_file(f) for f in files if f.category == "brand"]
            master = [describe_file(f) for f in files if f.category == "master"]
            kit = db.query(models.BrandKit).filter_by(client_id=c.id, user_id=user_id).first()
            kit_text = brandkit.summary(brandkit.KitData.model_validate(kit.data), kit) if kit else None
            return (c, brand, master, kit_text), []

    result, candidates = await asyncio.to_thread(run)
    if result is None:
        if candidates:
            return f"\"{client}\" matches several clients: {', '.join(candidates)}. Ask which one, or retry with the exact name."
        return f"No client matches \"{client}\". Use find_clients to list them."
    c, brand, master, kit_text = result
    lines = [f"Client: {c.name} (client {c.id})", ""]
    if kit_text:
        lines += [kit_text, "Pass client=\"" + c.name + "\" to generate_image to apply this kit (its primary logo is used if you give no logo).", ""]
    else:
        lines += ["No brand kit yet (the user can extract one on the client's Brands tab).", ""]
    lines += [f"Brand files ({len(brand)}):"]
    lines += brand or ["- none"]
    lines += ["", f"Master files ({len(master)}):"]
    lines += master or ["- none"]
    if not kit_text and not any("logo" in b for b in brand):
        lines += ["", "No brand file is tagged as the logo. Ask the user which one is, or look_at the brand images."]
    return "\n".join(lines)


@tool
async def find_references(
    config: RunnableConfig, query: str = "", collection: str = "", only_uploads: bool = False, limit: int = 20
) -> str:
    """Search the shared reference library (web-collected designs and the user's own uploads) by words in a
    design's title, collection name or source site.

    Args:
        query: Words to match against titles and descriptions, e.g. "dashain kites". Empty to list the newest.
        collection: Leave empty unless the user names a specific collection.
        only_uploads: True only if the user asks for references they uploaded themselves.
        limit: Max results (1-50). Use 10+ when you'll compare them.
    """
    user_id = config["configurable"]["user_id"]
    limit = max(1, min(limit, 50))

    async def search(col: str, uploads: bool) -> list[models.Reference]:
        def load():
            with SessionLocal() as db:
                q = db.query(models.Reference).filter_by(user_id=user_id)
                if uploads:
                    q = q.filter(models.Reference.collection.is_(None))
                elif col:
                    q = q.filter(models.Reference.collection.ilike(f"%{col}%"))
                rows = q.order_by(models.Reference.created_at.desc()).all()
                for r in rows:
                    db.expunge(r)
                return rows
        rows = await asyncio.to_thread(load)
        return await rank_references(rows, query) if rows and query.strip() else rows

    rows = await search(collection.strip(), only_uploads)
    note = ""
    if not rows and (collection.strip() or only_uploads):
        # A too-narrow filter shouldn't hide matches elsewhere in the library
        rows = await search("", False)
        if rows:
            note = "(Nothing matched with that filter; these are matches from the whole library.)\n"
    if not rows:
        return "No references match." + (" Try fewer words, or collect_references to find new ones." if query else "")
    lines = [f"{note}{len(rows)} reference(s){' (showing ' + str(limit) + ')' if len(rows) > limit else ''}:"]
    for r in rows[:limit]:
        origin = f"collection \"{r.collection}\"" if r.collection else "uploaded by the user"
        about = f" — {r.description}" if r.description and r.description not in r.name else ""
        lines.append(f"- [ref:{r.id}] \"{r.name}\"{about} — {origin}{', ' + size_text(r) if size_text(r) else ''}")
    return "\n".join(lines)


@tool
async def look_at(handles: list[str], config: RunnableConfig, question: str = "") -> str:
    """Look at library images (by handle) and describe them: style, colors, layout, text, what they show.
    Use it to choose between references, or to check what a brand file is, before generating.

    Args:
        handles: Up to 6 handles like "ref:12" or "brand:4".
        question: Optional thing to focus on, e.g. "which is the most minimal?".
    """
    user_id = config["configurable"]["user_id"]
    rows, missing = await asyncio.to_thread(resolve, user_id, handles[:MAX_LOOK])
    notes = [f"{h}: not found" for h in missing]
    images = [r for r in rows if is_image(r)]
    notes += [f"{handle(r)}: not an image ({r.name}, {r.content_type or 'unknown type'})" for r in rows if not is_image(r)]
    if images:
        content = [{"type": "text", "text": (
            f"Describe each of the {len(images)} images, in order, labeled with its handle: "
            + ", ".join(handle(r) for r in images)
            + (f"\nFocus on: {question}" if question else "")
        )}]
        content += [{"type": "image_url", "image_url": {
            "url": r.url.replace("/image/upload/", "/image/upload/c_limit,w_768,f_jpg/"), "detail": "low"}} for r in images]
        try:
            result = await imagegen._json_completion(prompts.LIBRARY_LOOK, content, model=settings.CLASSIFY_MODEL)
            for item in result.get("images", []):
                notes.append(f"{item.get('handle')}: {item.get('description')}")
            if result.get("answer"):
                notes.append(f"Answer: {result['answer']}")
        except Exception:
            log.exception("look_at failed")
            notes.append("Could not analyze the images right now.")
    return "\n".join(notes) or "Nothing to look at."


TOOLS = [find_clients, open_client, find_references, look_at]
