"""Per-user taste learning.

Signals:
- feedback: the user's next message after NNT generated an image. A vision model looks
  at that image and the message and decides whether it is a reaction (liked / disliked /
  mixed) and to which design aspects.
- reference: a reference image the user brought, which shows what they're interested in.

Signals are condensed into a TasteProfile, which new generations get as a
"learned preferences" line that only fills gaps the prompt and references leave open.
Nothing is fine-tuned: the model learns by remembering, per user.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Literal, Optional

from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

import models
from config import settings
from database import SessionLocal

log = logging.getLogger(__name__)


class Feedback(BaseModel):
    is_feedback: bool = Field(description="False if the message is about something else (a new, unrelated request, a question, small talk)")
    verdict: Literal["liked", "disliked", "mixed", "none"]
    liked: list[str] = Field(description="Specific design aspects the user approved of, e.g. 'warm golden-hour lighting'")
    disliked: list[str] = Field(description="Specific design aspects the user rejected or asked to change, e.g. 'background too dark'")
    design_tags: list[str] = Field(description="5-10 short tags describing the image's design: style, palette, layout, typography, mood")


class Profile(BaseModel):
    summary: str = Field(description="One or two sentences on what this user tends to want")
    styles: list[str]
    palettes: list[str]
    typography: list[str]
    composition: list[str]
    avoid: list[str]


FEEDBACK_PROMPT = """You learn a user's design taste for an image-generation assistant.
You get an image the assistant just generated, the prompt it used, and the user's NEXT message.
Decide whether that message reacts to the image, and how.

- Praise, approval, "perfect", "use this", downloading/asking for variations of it → liked.
- Complaints, "no", "not like this", "too X" → disliked.
- A change request on an otherwise accepted image ("make the mug green") → mixed: the changed aspect is disliked, the rest is implicitly accepted.
- A brand-new unrelated request, a question, or small talk → is_feedback=false, verdict=none.

Name concrete visual aspects (palette, lighting, typography, layout, style, mood), not subjects the user asked for.
design_tags always describe the image itself, whatever the verdict."""

PROFILE_PROMPT = """Build a compact design taste profile for one user from the signals below (newest first).

Weighting: explicit likes/dislikes > change requests (mixed) > reference images (interest).
Newer signals outweigh older ones; when they conflict, the newer one wins.
Only include a preference that is supported by two or more signals, or by one explicit, strongly worded statement.
Describe style only: palette, lighting, typography, composition, finish, mood. Never subjects, objects, or text to render.
Be concrete ("muted earth tones, terracotta and cream"), never vague ("nice colors"). Leave a list empty rather than guess."""

judge = ChatOpenAI(model=settings.ANALYSIS_MODEL, temperature=0, api_key=settings.OPENAI_API_KEY).with_structured_output(Feedback)
profiler = ChatOpenAI(model=settings.ANALYSIS_MODEL, temperature=0, api_key=settings.OPENAI_API_KEY).with_structured_output(Profile)

_locks: dict[int, asyncio.Lock] = {}
_background: set[asyncio.Task] = set()


def in_background(coro) -> None:
    """Learning must never slow down or break a chat reply."""
    async def guarded():
        try:
            await coro
        except Exception:
            log.exception("taste learning failed")

    task = asyncio.create_task(guarded())
    _background.add(task)
    task.add_done_callback(_background.discard)



async def learn_from_reply(user_id: int, conversation_id: str, image_url: str, image_prompt: str, user_message: str) -> None:
    if not user_message.strip():
        return
    result: Feedback = await judge.ainvoke([
        ("system", FEEDBACK_PROMPT),
        ("human", [
            {"type": "text", "text": f"Prompt used for this image:\n{image_prompt[:3000]}\n\nUser's next message:\n{user_message[:2000]}"},
            {"type": "image_url", "image_url": {"url": image_url, "detail": "low"}},
        ]),
    ])
    if not result.is_feedback or result.verdict == "none":
        return
    await _save_signal(user_id, conversation_id, "feedback", image_url, result.verdict, {
        "liked": result.liked,
        "disliked": result.disliked,
        "design_tags": result.design_tags,
        "user_message": user_message[:500],
    })
    await rebuild_profile(user_id)


async def learn_from_references(user_id: int, conversation_id: str, image_urls: list[str], specs: list[dict]) -> None:
    """Record references once per image, with the visual-forensics spec already computed for them."""
    def new_urls():
        with SessionLocal() as db:
            seen = {
                url for (url,) in db.query(models.DesignSignal.image_url).filter(
                    models.DesignSignal.user_id == user_id,
                    models.DesignSignal.kind == "reference",
                    models.DesignSignal.image_url.in_(image_urls),
                )
            }
        return [u for u in image_urls if u not in seen]

    fresh = set(await asyncio.to_thread(new_urls))
    added = False
    for url, spec in zip(image_urls, specs):
        if url in fresh:
            await _save_signal(user_id, conversation_id, "reference", url, "interest", {"spec": spec})
            added = True
    if added:
        await rebuild_profile(user_id)


async def _save_signal(user_id: int, conversation_id: str, kind: str, image_url: str, verdict: str, details: dict) -> None:
    def save():
        with SessionLocal() as db:
            db.add(models.DesignSignal(
                user_id=user_id, conversation_id=conversation_id, kind=kind,
                image_url=image_url, verdict=verdict, details=details,
            ))
            db.commit()

    await asyncio.to_thread(save)


# ---------- Profile ----------

async def rebuild_profile(user_id: int) -> None:
    async with _locks.setdefault(user_id, asyncio.Lock()):
        def load():
            with SessionLocal() as db:
                q = db.query(models.DesignSignal).filter(models.DesignSignal.user_id == user_id)
                total = q.count()
                rows = q.order_by(models.DesignSignal.created_at.desc()).limit(settings.TASTE_MAX_SIGNALS).all()
                return total, [
                    {"kind": r.kind, "verdict": r.verdict, "when": r.created_at.date().isoformat(), **r.details}
                    for r in rows
                ]

        total, signals = await asyncio.to_thread(load)
        if not signals:
            return
        profile: Profile = await profiler.ainvoke([
            ("system", PROFILE_PROMPT),
            ("human", json.dumps(signals, ensure_ascii=False, indent=1)),
        ])

        def save():
            with SessionLocal() as db:
                row = db.get(models.TasteProfile, user_id) or models.TasteProfile(user_id=user_id)
                row.profile = profile.model_dump()
                row.signal_count = total
                row.updated_at = datetime.now(timezone.utc)
                db.add(row)
                db.commit()

        await asyncio.to_thread(save)
        log.info("taste profile for user %s rebuilt from %s signals", user_id, total)


def get_profile(user_id: int) -> Optional[models.TasteProfile]:
    with SessionLocal() as db:
        return db.get(models.TasteProfile, user_id)


def reset(user_id: int) -> None:
    with SessionLocal() as db:
        db.query(models.DesignSignal).filter(models.DesignSignal.user_id == user_id).delete()
        db.query(models.TasteProfile).filter(models.TasteProfile.user_id == user_id).delete()
        db.commit()


def preference_note(row: Optional[models.TasteProfile]) -> str:
    """The line appended to image prompts. Empty until there's enough evidence."""
    if row is None or row.signal_count < settings.TASTE_MIN_SIGNALS:
        return ""
    p = row.profile
    parts = [
        f"{label}: {', '.join(p[key])}"
        for label, key in (("Style", "styles"), ("Palette", "palettes"), ("Typography", "typography"),
                           ("Composition", "composition"), ("Avoid", "avoid"))
        if p.get(key)
    ]
    if not parts:
        return ""
    return (
        "\n\nLearned preferences (from this user's past feedback; apply only where the request and references above are silent): "
        + "; ".join(parts) + "."
    )
