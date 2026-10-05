"""Long-term memory: chunk finished turns, embed them, and recall the relevant ones later.

Embeddings live in Postgres (`memory_chunks`) and are ranked with numpy, scoped to one
user. pgvector isn't installed on the local server; if it is added later, only
`search` needs to change to an ORDER BY embedding <=> query.
"""
import asyncio
from datetime import datetime, timezone

import numpy as np
from langchain_openai import OpenAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

import models
from config import settings
from database import SessionLocal

embeddings = OpenAIEmbeddings(model=settings.EMBEDDING_MODEL, api_key=settings.OPENAI_API_KEY)

# Token-based (not character-based) so chunks line up with the embedding model's
# tokenizer; the separators prefer paragraph → line → sentence → word breaks.
splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
    encoding_name="cl100k_base",
    chunk_size=settings.CHUNK_TOKENS,
    chunk_overlap=settings.CHUNK_OVERLAP_TOKENS,
    separators=["\n\n", "\n", ". ", "? ", "! ", "; ", ", ", " ", ""],
)

QUESTION_HEADER_CHARS = 300


def build_chunks(user_text: str, assistant_text: str, when: datetime) -> list[str]:
    """Split one user→assistant turn into self-contained chunks.

    Every chunk carries a short header (date + the question being answered) so a
    chunk cut from the middle of a long answer still makes sense on its own, both
    to the embedding model and to the LLM reading it back.
    """
    date = when.strftime("%Y-%m-%d")
    question = " ".join(user_text.split())
    short_q = question if len(question) <= QUESTION_HEADER_CHARS else question[:QUESTION_HEADER_CHARS] + "…"

    chunks: list[str] = []
    user_parts = splitter.split_text(user_text)
    for i, part in enumerate(user_parts):
        label = "User said" if len(user_parts) == 1 else f"User said (part {i + 1}/{len(user_parts)})"
        chunks.append(f"[{date}] {label}:\n{part}")

    answer_parts = splitter.split_text(assistant_text)
    for i, part in enumerate(answer_parts):
        label = "Assistant replied" if len(answer_parts) == 1 else f"Assistant replied (part {i + 1}/{len(answer_parts)})"
        chunks.append(f"[{date}] In reply to \"{short_q}\" — {label}:\n{part}")
    return chunks


async def remember_turn(user_id: int, conversation_id: str, user_text: str, assistant_text: str) -> int:
    if not user_text.strip() or not assistant_text.strip():
        return 0
    now = datetime.now(timezone.utc)
    chunks = build_chunks(user_text, assistant_text, now)
    vectors = await embeddings.aembed_documents(chunks)

    def save():
        with SessionLocal() as db:
            db.add_all(
                models.MemoryChunk(
                    user_id=user_id,
                    conversation_id=conversation_id,
                    content=text,
                    embedding=vec,
                    created_at=now,
                )
                for text, vec in zip(chunks, vectors)
            )
            db.commit()

    await asyncio.to_thread(save)
    return len(chunks)


async def search(user_id: int, query: str, conversation_id: str, live_since: datetime | None) -> list[str]:
    """Top-k chunks across all of this user's conversations, most similar first.

    Chunks of the current conversation created since `live_since` (the last time its
    history was compacted, or ever if it never was) are still verbatim in the
    model's context, so they are skipped rather than injected twice.
    """
    if not query.strip():
        return []
    query_vec = np.asarray(await embeddings.aembed_query(query), dtype=np.float32)

    def load():
        with SessionLocal() as db:
            q = db.query(models.MemoryChunk.content, models.MemoryChunk.embedding).filter(
                models.MemoryChunk.user_id == user_id
            )
            in_window = models.MemoryChunk.conversation_id == conversation_id
            if live_since is not None:
                in_window = in_window & (models.MemoryChunk.created_at >= live_since)
            return q.filter(~in_window).all()

    rows = await asyncio.to_thread(load)
    if not rows:
        return []
    matrix = np.asarray([r.embedding for r in rows], dtype=np.float32)
    # OpenAI embeddings are unit-length, so the dot product is the cosine similarity
    scores = matrix @ query_vec
    best = np.argsort(-scores)[: settings.RECALL_TOP_K]
    return [rows[i].content for i in best if scores[i] >= settings.RECALL_MIN_SCORE]


async def forget_conversation(user_id: int, conversation_id: str) -> None:
    def delete():
        with SessionLocal() as db:
            db.query(models.MemoryChunk).filter(
                models.MemoryChunk.user_id == user_id,
                models.MemoryChunk.conversation_id == conversation_id,
            ).delete()
            db.commit()

    await asyncio.to_thread(delete)
