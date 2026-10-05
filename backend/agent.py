"""The chat agent as an explicit LangGraph graph.

    START → recall ────┐
          sentiment ──┤
          learn ──────┴→ agent ⇄ tools (web search, generate_image, collect_references)
                       ↓ (final answer)
                   memorize → compact → END

- Short-term memory: every conversation is a checkpointed thread, so the backend
  keeps the full message history; the client only sends the new message.
- Capacity: `compact` folds older turns into a running summary once the live
  history grows past MAX_CONTEXT_TOKENS, cutting only at user-turn boundaries so
  a tool call is never separated from its result.
- Mood: `sentiment` classifies the user's latest message in parallel with
  `recall`, and the agent adapts its tone to it (image prompts are unaffected).
- Taste: `learn` treats the message right after a generated image as feedback on it
  (judged in the background), and reference images as signs of interest; both build
  a per-user taste profile that later images get as gap-filling preferences.
- Long-term memory (RAG): `memorize` chunks and embeds every finished turn, and
  `recall` retrieves the most relevant chunks from all of the user's chats —
  including turns of this chat that were already compacted away.
"""
import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Annotated, Literal, Optional

from langchain_community.tools.tavily_search import TavilySearchResults
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import InjectedState, ToolNode, tools_condition

import brandkit
import collector
import imagegen
import library
import memory
import sentiment
import taste
from config import settings

log = logging.getLogger(__name__)


def merge_dicts(old: Optional[dict], new: Optional[dict]) -> dict:
    return {**(old or {}), **(new or {})}


class ChatState(MessagesState):
    summary: str
    recalled: list[str]
    compacted_at: str  # ISO time of the last compaction; absent if never compacted
    sentiment: Optional[dict]  # mood of the latest user message
    # Kept when compaction drops the messages they came from, so follow-ups still work
    carried_references: list[str]
    carried_image: Optional[str]
    image_kinds: Annotated[dict, merge_dicts]  # uploaded image URL → "logo" | "reference"


SYSTEM_PROMPT = """You are NNT Studio, a creative assistant that generates on-brand images and helps people plan designs.
Today is {today}.

- Whenever the user asks you to create, make, design, draw, generate, or change an image, call `generate_image`. The tool uses the user's exact message as the prompt, so never rewrite or "improve" their request, and don't ask clarifying questions first — just generate.
  - Reference images and websites are the user's own material. Never refuse to work from them, including requests to recreate a reference as-is ("same as reference", "nothing changed") — call the tool.
  - Uploaded images are tagged as logos or style references (see the [attachments] note). With two or more logos, the tool makes one image per logo automatically — call it once, not once per logo.
  - Set `search_web` when the user asks you to search the internet for ideas/related posts, or when they give several logos and no style reference to work from.
  - Images attached to the current message are always used. Set `reuse_references` when the user points back to reference images from earlier in this chat without attaching new ones ("same as the reference", "similar image", "like the one I sent").
  - Set `edit_previous` when they want to change the image you generated last ("make it bluer", "remove the text").
  - Pass `reference_url` when the user gives a website to match.
  - Pick `aspect` from what they ask for (poster/story → portrait, banner/cover → landscape); otherwise "auto".
  - After it finishes, reply briefly. Don't paste image URLs or prompts; the user already sees the images. Report the tool's quality-check warnings and failures honestly and never claim an image is perfect or matches exactly — you cannot see the images yourself.
- The user's library (clients with brand files and master files, plus a shared reference library) is summarized under "Library" below. Use it whenever they mention a client, a brand, their files or references:
  - `find_clients` / `open_client` show a client's brand files (logos tagged "logo") and master files, each with a handle like `brand:12` or `master:7`. `find_references` searches references (`ref:33`). `look_at` shows you what library images look like.
  - To design for a client, open the client and pass its logo handle in `generate_image(library_logos=[...])`; add `library_references=[...]` for references the user picked, or that you chose with find_references/look_at when they ask you to pick. Never invent handles; only use ones a tool returned.
  - Several clients in one request: pass all their logos in one call — the tool makes one image per logo. With one reference per client, say which reference went to which in your reply.
  - Brand kits: a client may have a brand kit (logo, colors, fonts, footer/contact details, style) the user extracted and reviewed; `open_client` shows it. When designing for a client, pass `client="<name>"` to `generate_image` — the kit is applied automatically and its primary logo is used if you pass no logo. Library logos also bring their own client's kit. Never type the kit's colors, fonts or contact details into your reply as if they were your idea, and never invent contact details.
  - `include_footer` (default true) adds the kit's footer band with the client's contact details. Set it false when the user asks for no contact details/footer, gives their own contact text, or the design is something a footer doesn't fit (a logo, an icon, a profile picture).
  - If a client has no brand kit and no logo tagged, ask which brand file is the logo (or look_at its brand images) before generating. You may suggest extracting a brand kit on the client's Brands tab.
  - Answer questions about the library ("which clients have no logo?", "what master files does X have?") from these tools; don't guess.
- @-mentions: when the user tags a client or references with @ (see the [mentions] note in their message), they are already resolved — act on them directly without find_clients/find_references.
  - @client → design with `generate_image(client="<name>")`.
  - @topic references → use them as `library_references`. The image model takes few input images, so when a topic brings more than 3, pick the 2-3 that best fit the request (look_at them if the descriptions aren't enough) and say which you used. Use more only if the user explicitly asks for all of them, and then at most 4.
  - Several @clients in one request → pass each client's logo in one call (library_logos) so each gets its own image with its own brand kit.
- When the user asks you to find, collect, gather or save designs/references/inspiration from the web (Pinterest, Behance, Dribbble, "popular sites"…), call `collect_references`. It saves the designs straight into their References library.
  - `topic`: a short English search topic with the occasion, locale, format and style words they used (e.g. "Dashain festival social media poster"). Don't add things they didn't ask for.
  - `count`: the number they asked for; for a range like "20-30" use the top of it. Default 20, at most 30.
  - `sites`: only if they name specific sites. Otherwise leave it empty (it searches Pinterest, Behance, Dribbble and the wider web).
  - Call it once per request. Don't call `generate_image` unless they also asked for an image.
  - Afterwards reply briefly: how many were saved, from which sites, and that they're in **References** ([open References](/dashboard/references)). If fewer than asked were saved, say so honestly and suggest a broader topic. Don't list the image URLs; the user already sees them.
- Use web search when the answer depends on current trends, facts, or examples you are unsure of. Cite sources as markdown links.
- When the user attaches reference images, study them closely (style, palette, typography, composition, mood) and ground your answer in what you actually see.
- Be concise and concrete. Use markdown for structure when it helps."""

SUMMARY_SECTION = """

## Earlier in this conversation (summary)
{summary}"""

TASTE_SECTION = """

## What you've learned about this user's design taste
{summary}
This is applied to their images automatically; mention it only if they ask what you've learned."""

TONE_SECTION = """

## Tone for this reply
{guidance}
Don't mention that you detected their mood."""

LIBRARY_SECTION = """

## Library
{overview}"""

RECALL_SECTION = """

## Possibly relevant notes from the user's past conversations
These were retrieved automatically and may be outdated or unrelated; use them only if they help.
{notes}"""

SUMMARY_PROMPT = """You maintain the running memory of a conversation between a user and a creative assistant.
Merge the existing summary with the new messages into one updated summary.
Keep: the user's goals, brand/project details, names, preferences, decisions made, descriptions of reference images they shared, and open questions.
Drop: pleasantries, search result dumps, and anything superseded by a later decision.
Write compact bullet points, at most ~300 words."""


def message_text(msg: BaseMessage) -> str:
    """Text of a message, with attached images reduced to a short note."""
    if isinstance(msg.content, str):
        return msg.content
    texts, images = [], 0
    for part in msg.content:
        if isinstance(part, str):
            texts.append(part)
        elif part.get("type") == "text":
            texts.append(part["text"])
        elif part.get("type") == "image_url":
            images += 1
    text = "\n".join(texts)
    if images:
        text += f"\n[attached {images} reference image{'s' if images > 1 else ''}]"
    return text.strip()


ATTACHMENTS_MARK = "[attachments]"


def build_user_message(text: str, images: list[dict], mention_note: str = "") -> HumanMessage:
    """`images` are {"url", "kind"} dicts. Separate, marked text parts tell the model which
    attachment is a logo and which a reference, and what the user @-mentioned; they are never
    part of the user's words."""
    if not images and not mention_note:
        return HumanMessage(content=text)
    roles = "; ".join(f"image {i + 1}: {img['kind']}" for i, img in enumerate(images))
    return HumanMessage(
        content=([{"type": "text", "text": text}] if text else [])
        + ([{"type": "text", "text": mention_note}] if mention_note else [])
        + ([{"type": "text", "text": f"{ATTACHMENTS_MARK} {roles}"}] if images else [])
        + [{"type": "image_url", "image_url": {"url": img["url"], "detail": "auto"}} for img in images]
    )


MENTION_LABEL = re.compile(r"^- @(.+?) = ", re.M)


def without_mention_marks(words: str, msg: HumanMessage) -> str:
    """The user's words with the @ dropped from mentions ("@Himali Brew" → "Himali Brew"),
    so the image model doesn't print handles. Nothing else is changed."""
    if isinstance(msg.content, str):
        return words
    note = next((p["text"] for p in msg.content if isinstance(p, dict) and p.get("type") == "text"
                 and p["text"].startswith(library.MENTIONS_MARK)), "")
    for label in sorted(MENTION_LABEL.findall(note), key=len, reverse=True):
        words = re.sub("@" + re.escape(label), lambda _: label, words, flags=re.I)
    return words


def user_words_and_images(msg: HumanMessage) -> tuple[str, list[str]]:
    """The user's own text (exactly as typed) and the attached image URLs of a message."""
    if isinstance(msg.content, str):
        return msg.content, []
    texts = [
        p["text"] for p in msg.content
        if isinstance(p, dict) and p.get("type") == "text" and not p["text"].startswith((ATTACHMENTS_MARK, library.MENTIONS_MARK))
    ]
    urls = [p["image_url"]["url"] for p in msg.content if isinstance(p, dict) and p.get("type") == "image_url"]
    return "\n".join(texts), urls


def image_shown_last_turn(messages: list[BaseMessage]) -> Optional[dict]:
    """The single image generated in the turn just before the latest user message, if any.
    Batches are skipped: a reply to five images can't be pinned to one of them."""
    for m in reversed(messages[:-1]):
        if isinstance(m, HumanMessage):
            return None
        if isinstance(m, ToolMessage) and m.name == "generate_image" and isinstance(m.artifact, dict):
            return m.artifact if m.artifact.get("url") else None
    return None


def latest_reference_images(messages: list[BaseMessage]) -> list[str]:
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            _, urls = user_words_and_images(m)
            if urls:
                return urls
    return []


def last_generated_image(messages: list[BaseMessage]) -> Optional[str]:
    for m in reversed(messages):
        if isinstance(m, ToolMessage) and m.name == "generate_image" and isinstance(m.artifact, dict) and m.artifact.get("url"):
            return m.artifact["url"]
    return None


def describe_check(check: Optional[dict]) -> str:
    if not check or check.get("passed") is None:
        return "quality check unavailable"
    return "quality check passed" if check["passed"] else "quality check FLAGGED: " + "; ".join(check["issues"])


@tool(response_format="content_and_artifact")
async def generate_image(
    state: Annotated[dict, InjectedState],
    config: RunnableConfig,
    aspect: Literal["auto", "square", "portrait", "landscape"] = "auto",
    reference_url: Optional[str] = None,
    edit_previous: bool = False,
    reuse_references: bool = False,
    search_web: bool = False,
    library_logos: Optional[list[str]] = None,
    library_references: Optional[list[str]] = None,
    client: Optional[str] = None,
    include_footer: bool = True,
):
    """Generate images from the user's latest message, using the logos and reference images they attached
    and/or files from their library. With two or more logos it makes one image per logo in a single call.
    Library logos automatically bring their client's brand kit (colors, fonts, footer/contact details).

    Args:
        aspect: Canvas shape. "auto" unless the user implies a format.
        reference_url: A website whose brand (colors, type, mood) the image should follow, if the user gave one.
        edit_previous: True to modify the last single image you generated instead of starting fresh.
        reuse_references: True to use the logos/references the user attached earlier in this chat, when the current message has none.
        search_web: True to search the internet for related posts as style inspiration (used as a description only, never copied).
        library_logos: Handles of logos from the library to place, e.g. ["brand:12"] (from open_client/find_clients).
        library_references: Handles of library images to use as style references, e.g. ["ref:33"] (from find_references).
        client: Name of the client the design is for, to apply its brand kit. With no logo given, the kit's primary logo is used.
        include_footer: Add the brand kit's footer band with the client's contact details. False if the user gives their own contact text or doesn't want contact details.
    """
    write = get_stream_writer()
    cfg = config["configurable"]
    question = next(m for m in reversed(state["messages"]) if isinstance(m, HumanMessage))
    words, images = user_words_and_images(question)
    words = without_mention_marks(words, question)
    if not words.strip():
        return "The user attached images but didn't say what to make. Ask them what image they want.", None
    if not images and reuse_references and not (library_logos or library_references):
        images = latest_reference_images(state["messages"]) or state.get("carried_references") or []
        if not images:
            return "There are no reference images earlier in this conversation. Ask the user to attach one.", None

    # A batch already covers every logo; never run a second one in the same turn
    turn = []
    for m in reversed(state["messages"]):
        if isinstance(m, HumanMessage):
            break
        turn.append(m)
    if any(isinstance(m, ToolMessage) and m.name == "generate_image" and isinstance(m.artifact, dict) and m.artifact.get("images") for m in turn):
        return "All logos were already generated in this turn. Don't call generate_image again; just reply to the user.", None

    kinds = state.get("image_kinds") or {}
    logos = [u for u in images if kinds.get(u) == "logo"]
    references = [u for u in images if kinds.get(u) != "logo"]
    user_id = cfg["user_id"]

    # The client the design is for: its brand kit, and its primary logo when no logo was given
    client_kit = None
    if client:
        name, client_kit, problem = await asyncio.to_thread(library.client_kit, user_id, client)
        if problem:
            return problem + " Check the client with find_clients.", None
        if client_kit and client_kit.primary_logo and not logos and not library_logos:
            library_logos = [client_kit.primary_logo]

    if library_references and len(library_references) > 4:
        return "Too many references: the image model takes at most 4. Pick the 2-3 that fit the request best and call again.", None

    # Files picked from the user's library (resolved per user, so only their own)
    lib_logos = []
    if library_logos or library_references:
        lib_logos, bad_logos = await asyncio.to_thread(library.image_urls, user_id, library_logos or [])
        lib_refs, bad_refs = await asyncio.to_thread(library.image_urls, user_id, library_references or [])
        if bad_logos or bad_refs:
            return "Some library handles can't be used: " + "; ".join(bad_logos + bad_refs) + ". Check them with open_client/find_references.", None
        logos += [u for u in lib_logos if u not in logos]
        references += [u for u in lib_refs if u not in references]
        n_logo, n_ref = len(lib_logos), len(lib_refs)
        write({"status": "Using " + " and ".join(
            p for p in (f"{n_logo} logo{'s' if n_logo != 1 else ''}" if n_logo else "",
                        f"{n_ref} reference{'s' if n_ref != 1 else ''}" if n_ref else "") if p
        ) + " from your library…"})
    preferences = taste.preference_note(await asyncio.to_thread(taste.get_profile, user_id))
    progress = lambda text: write({"status": text})

    # Brand kit per logo: a library logo brings its own client's kit; logos attached in chat use `client`'s
    kits = await asyncio.to_thread(brandkit.kits_for_logo_urls, user_id, lib_logos)
    if client_kit:
        for u in logos:
            kits.setdefault(u, client_kit)
    brands = {
        u: {"name": k.brand_name, "note": brandkit.prompt_note(k, include_footer), "facts": brandkit.approved_text(k)}
        for u, k in kits.items()
    }
    if not logos and client_kit:  # no logo at all, but the design is still for this client
        brands[None] = {"name": client_kit.brand_name, "note": brandkit.prompt_note(client_kit, include_footer),
                        "facts": brandkit.approved_text(client_kit)}
    if brands:
        progress("Applying the brand kit…")

    if len(logos) >= 2:
        if edit_previous:
            return "Editing isn't supported for multi-logo batches. Ask the user which single image to change.", None
        dropped = logos[settings.MAX_LOGOS_PER_BATCH:]
        items = await imagegen.generate_for_logos(
            user_id=user_id, user_prompt=words.strip(), logos=logos, reference_images=references,
            reference_url=reference_url, search_web=search_web, aspect=aspect, preferences=preferences,
            progress=progress, on_image=lambda item: write({"image": item}), brands=brands,
        )
        lines = [f"Generated a batch of {len(items)} images (one per logo); all are already shown to the user."]
        for item in items:
            if item.get("error"):
                lines.append(f"- {item['label']}: FAILED — {item['error']}")
            else:
                extra = f"; style inspiration from {len(item['inspiration'])} web post(s)" if item["inspiration"] else ""
                site = f"; brand site analyzed: {item['official_site']}" if item["official_site"] else ""
                kit = "; brand kit applied" if item.get("brand_kit") else ""
                lines.append(f"- {item['label']}: {describe_check(item['check'])}{kit}{extra}{site}")
        if dropped:
            lines.append(f"Only the first {settings.MAX_LOGOS_PER_BATCH} logos were used; {len(dropped)} were skipped. Tell the user.")
        return "\n".join(lines), {"images": items, "url": None}

    edit_url = (last_generated_image(state["messages"]) or state.get("carried_image")) if edit_previous else None
    if edit_previous and edit_url is None:
        return "There is no earlier generated image in this conversation to edit. Tell the user.", None

    try:
        extra_specs, url_brief, sources = [], None, []
        if search_web:
            progress("Searching the web for related posts…")
            brand = await imagegen.identify_brand(logos[0]) if logos else {}
            found = await imagegen.research(words.strip(), brand, set())
            extra_specs, url_brief, sources = found["specs"], found["url_brief"], found["sources"]
        result = await imagegen.generate(
            user_id=user_id,
            user_prompt=words.strip(),
            reference_images=references,
            reference_url=reference_url,
            aspect=aspect,
            edit_image_url=edit_url,
            logos=logos,
            extra_specs=extra_specs,
            url_brief=url_brief,
            preferences=preferences,
            brand_note=(brands.get(logos[0] if logos else None) or {}).get("note", ""),
            brand_facts=(brands.get(logos[0] if logos else None) or {}).get("facts", ""),
            progress=progress,
        )
    except imagegen.ImageGenError as e:
        return f"Image generation failed: {e}", None

    image = {"url": result["url"], "width": result["width"], "height": result["height"],
             "prompt": result["prompt"], "check": result["check"], "inspiration": sources}
    write({"image": image})

    analyzed = [(url, spec) for url, spec in zip(references, result["specs_by_image"]) if spec is not None]
    if analyzed:
        taste.in_background(taste.learn_from_references(
            user_id, cfg["conversation_id"], [u for u, _ in analyzed], [s for _, s in analyzed]
        ))

    notes = [describe_check(result["check"])]
    if brands.get(logos[0] if logos else None):
        notes.append("The client's brand kit was applied" + (" (footer included)." if include_footer else " (no footer)."))
    if result["url_brief"]:
        notes.append(f"Brand brief used: {result['url_brief']}")
    if result["image_specs"]:
        notes.append(f"Used {len(result['image_specs'])} style description(s) from references/web.")
    content = (
        "Image generated and already shown to the user.\n"
        f"Final prompt sent to the image model:\n{result['prompt']}\n" + "\n".join(notes)
    )
    return content, image


@tool(response_format="content_and_artifact")
async def collect_references(
    topic: str,
    config: RunnableConfig,
    count: int = 20,
    sites: Optional[list[str]] = None,
):
    """Search Pinterest, Behance, Dribbble and the web for unique design examples on a topic, and save the
    best ones into the user's References library (deduplicated, checked to be relevant finished designs).

    Args:
        topic: Short search topic, e.g. "Dashain festival social media poster".
        count: How many designs to save (1-30).
        sites: Only the sites the user named, e.g. ["pinterest", "behance"]. Empty for the default mix.
    """
    write = get_stream_writer()
    result = await collector.collect(
        user_id=config["configurable"]["user_id"],
        topic=topic.strip()[:200],
        count=count,
        sites=sites,
        progress=lambda text: write({"status": text}),
        on_saved=lambda ref: write({"reference": ref}),
    )
    saved = result["saved"]
    if not saved:
        return f"No designs were saved. {result.get('reason', 'None of the found images were relevant, distinct designs.')} Searched: {result['searched']}.", None
    by_site: dict[str, int] = {}
    for s in result["sites"]:
        by_site[s] = by_site.get(s, 0) + 1
    content = (
        f"Saved {len(saved)} of the {count} requested designs to the References library (collection \"{topic}\"); "
        f"the user already sees them. From: {', '.join(f'{k} ({v})' for k, v in by_site.items())}. "
        f"Searches: {result['searched']}. {result['candidates']} candidates found, {result['downloaded']} distinct usable images reviewed."
    )
    return content, {"references": saved, "collection": topic}


LIBRARY_TOOL_NAMES = {t.name for t in library.TOOLS}


def dangling_tool_results(messages: list[BaseMessage]) -> list[ToolMessage]:
    """If a run was stopped between the model asking for a tool and the tool answering,
    the saved history ends on unanswered tool calls, which the API rejects.
    Returns placeholder results that close them."""
    if not messages:
        return []
    last = messages[-1]
    if not isinstance(last, AIMessage) or not last.tool_calls:
        return []
    return [
        ToolMessage(content="Cancelled by the user.", tool_call_id=call["id"], name=call["name"])
        for call in last.tool_calls
    ]


def build_graph(checkpointer):
    search_tool = TavilySearchResults(max_results=5, tavily_api_key=settings.TAVILY_API_KEY)
    tools = [search_tool, generate_image, collect_references, *library.TOOLS]
    # One tool call at a time: parallel generate_image calls multiplied batches (and cost) in testing
    llm = ChatOpenAI(model=settings.CHAT_MODEL, temperature=0, api_key=settings.OPENAI_API_KEY).bind_tools(
        tools, parallel_tool_calls=False
    )
    summarizer = ChatOpenAI(model=settings.SUMMARY_MODEL, temperature=0, api_key=settings.OPENAI_API_KEY)

    async def recall(state: ChatState, config: RunnableConfig):
        cfg = config["configurable"]
        query = message_text(state["messages"][-1])
        compacted_at = state.get("compacted_at")
        live_since = datetime.fromisoformat(compacted_at) if compacted_at else None
        try:
            notes = await memory.search(cfg["user_id"], query, cfg["conversation_id"], live_since)
        except Exception:
            log.exception("memory recall failed")
            notes = []
        return {"recalled": notes}

    async def detect_sentiment(state: ChatState):
        try:
            result = await sentiment.classify(state["messages"])
        except Exception:
            log.exception("sentiment classification failed")
            result = None
        return {"sentiment": result.model_dump() if result else None}

    async def learn(state: ChatState, config: RunnableConfig):
        cfg = config["configurable"]
        shown = image_shown_last_turn(state["messages"])
        if shown:
            words, _ = user_words_and_images(state["messages"][-1])
            taste.in_background(taste.learn_from_reply(
                cfg["user_id"], cfg["conversation_id"], shown["url"], shown.get("prompt", ""), words
            ))
        return {}

    async def agent(state: ChatState, config: RunnableConfig):
        system = SYSTEM_PROMPT.format(today=datetime.now().strftime("%A, %B %d, %Y"))
        profile = await asyncio.to_thread(taste.get_profile, config["configurable"]["user_id"])
        if profile and profile.signal_count >= settings.TASTE_MIN_SIGNALS and profile.profile.get("summary"):
            system += TASTE_SECTION.format(summary=profile.profile["summary"])
        try:
            system += LIBRARY_SECTION.format(overview=await asyncio.to_thread(library.overview, config["configurable"]["user_id"]))
        except Exception:
            log.exception("library overview failed")
        if state.get("summary"):
            system += SUMMARY_SECTION.format(summary=state["summary"])
        guidance = sentiment.tone_guidance(state.get("sentiment"))
        if guidance:
            system += TONE_SECTION.format(guidance=guidance)
        if state.get("recalled"):
            system += RECALL_SECTION.format(notes="\n\n---\n\n".join(state["recalled"]))
        response = await llm.ainvoke([SystemMessage(system), *state["messages"]])
        return {"messages": [response]}

    async def memorize(state: ChatState, config: RunnableConfig):
        cfg = config["configurable"]
        messages = state["messages"]
        answer = messages[-1]
        question = next((m for m in reversed(messages) if isinstance(m, HumanMessage)), None)
        if question is None or not isinstance(answer, AIMessage):
            return {}
        try:
            await memory.remember_turn(cfg["user_id"], cfg["conversation_id"], message_text(question), message_text(answer))
        except Exception:
            log.exception("memorizing turn failed")
        return {}

    async def compact(state: ChatState):
        messages = state["messages"]
        if count_tokens_approximately(messages) <= settings.MAX_CONTEXT_TOKENS:
            return {}

        # Keep the most recent whole turns worth at least KEEP_RECENT_TOKENS.
        cut = 0
        for i in range(len(messages) - 1, 0, -1):
            if isinstance(messages[i], HumanMessage) and count_tokens_approximately(messages[i:]) >= settings.KEEP_RECENT_TOKENS:
                cut = i
                break
        if cut == 0:
            return {}

        removed = messages[:cut]
        lines = []
        for m in removed:
            if isinstance(m, HumanMessage):
                lines.append(f"User: {message_text(m)}")
            elif isinstance(m, AIMessage):
                for call in m.tool_calls:
                    if call["name"] == "generate_image":
                        lines.append("(Assistant generated an image)")
                    elif call["name"] == "collect_references":
                        lines.append(f"(Assistant collected design references for: {call['args'].get('topic')})")
                    elif call["name"] in LIBRARY_TOOL_NAMES:
                        lines.append(f"(Assistant checked the library: {call['name']} {call['args']})")
                    else:
                        lines.append(f"(Assistant searched the web for: {call['args'].get('query', call['args'])})")
                if message_text(m):
                    lines.append(f"Assistant: {message_text(m)}")
        transcript = "\n\n".join(lines)

        response = await summarizer.ainvoke([
            SystemMessage(SUMMARY_PROMPT),
            HumanMessage(f"Existing summary:\n{state.get('summary') or '(none)'}\n\nNew messages to fold in:\n{transcript}"),
        ])
        return {
            "summary": response.content,
            "messages": [RemoveMessage(id=m.id) for m in removed],
            "compacted_at": datetime.now(timezone.utc).isoformat(),
            "carried_references": latest_reference_images(messages) or state.get("carried_references") or [],
            "carried_image": last_generated_image(messages) or state.get("carried_image"),
        }

    graph = StateGraph(ChatState)
    graph.add_node("recall", recall)
    graph.add_node("sentiment", detect_sentiment)
    graph.add_node("learn", learn)
    graph.add_node("agent", agent)
    graph.add_node("tools", ToolNode(tools))
    graph.add_node("memorize", memorize)
    graph.add_node("compact", compact)

    graph.add_edge(START, "recall")
    graph.add_edge(START, "sentiment")
    graph.add_edge(START, "learn")
    graph.add_edge(["recall", "sentiment", "learn"], "agent")  # agent waits for all three
    graph.add_conditional_edges("agent", tools_condition, {"tools": "tools", END: "memorize"})
    graph.add_edge("tools", "agent")
    graph.add_edge("memorize", "compact")
    graph.add_edge("compact", END)

    return graph.compile(checkpointer=checkpointer)
