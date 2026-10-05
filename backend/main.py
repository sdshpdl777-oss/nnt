import os
import certifi

# python.org builds of Python ship without a CA bundle, so the async (aiohttp) web
# search fails with CERTIFICATE_VERIFY_FAILED. aiohttp builds its SSL context when it
# is first imported, so this must run before any other import.
os.environ.setdefault("SSL_CERT_FILE", certifi.where())

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Literal

import cloudinary
import cloudinary.uploader
from fastapi import FastAPI, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from langchain_core.messages import AIMessageChunk
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from sqlalchemy.orm import Session

import models, schemas, auth, memory, taste, clients
from database import engine, get_db
from config import settings
import imagegen
import library
from agent import build_graph, build_user_message, dangling_tool_results

logging.basicConfig(level=logging.INFO)

# Create database tables
models.Base.metadata.create_all(bind=engine)
# create_all doesn't add columns to existing tables
if engine.dialect.name == "postgresql":
    with engine.begin() as conn:
        for column in ("collection", "source_url", "phash"):
            conn.exec_driver_sql(f"ALTER TABLE shared_references ADD COLUMN IF NOT EXISTS {column} VARCHAR")
        conn.exec_driver_sql("ALTER TABLE client_files ADD COLUMN IF NOT EXISTS kind VARCHAR")
        conn.exec_driver_sql("ALTER TABLE shared_references ADD COLUMN IF NOT EXISTS description TEXT")
        conn.exec_driver_sql("ALTER TABLE shared_references ADD COLUMN IF NOT EXISTS embedding REAL[]")

cloudinary.config(
    cloud_name=settings.CLOUDINARY_CLOUD_NAME,
    api_key=settings.CLOUDINARY_API_KEY,
    api_secret=settings.CLOUDINARY_API_SECRET,
    secure=True,
)

ALLOWED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The checkpointer stores each conversation's LangGraph state (messages, summary) in Postgres
    async with AsyncConnectionPool(
        settings.psycopg_dsn,
        max_size=10,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    ) as pool:
        checkpointer = AsyncPostgresSaver(pool)
        await checkpointer.setup()
        app.state.checkpointer = checkpointer
        app.state.graph = build_graph(checkpointer)
        yield


app = FastAPI(title="NNT API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(clients.router)


LIBRARY_STATUS = {
    "find_clients": "Looking through your clients…",
    "open_client": "Opening the client's files…",
    "find_references": "Searching your references…",
    "look_at": "Looking at the designs…",
}


def thread_id(user: models.User, conversation_id: str) -> str:
    # Namespaced by user so one account can never load another's conversation
    return f"user-{user.id}:{conversation_id}"


def sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"

@app.get("/")
def read_root():
    return {"message": "Welcome to NNT API"}

@app.post("/api/auth/register", response_model=schemas.Token)
def register_user(user: schemas.UserCreate, db: Session = Depends(get_db)):
    db_user = db.query(models.User).filter((models.User.email == user.email) | (models.User.username == user.username)).first()
    if db_user:
        raise HTTPException(status_code=400, detail="Username or email already registered")
    
    hashed_password = auth.get_password_hash(user.password)
    new_user = models.User(username=user.username, email=user.email, hashed_password=hashed_password)
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    
    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = auth.create_access_token(
        data={"sub": new_user.username}, expires_delta=access_token_expires
    )
    return {"access_token": access_token, "token_type": "bearer"}

@app.post("/api/auth/login", response_model=schemas.Token)
def login(user: schemas.UserLogin, db: Session = Depends(get_db)):
    db_user = db.query(models.User).filter(models.User.username == user.username).first()
    if not db_user or not auth.verify_password(user.password, db_user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = auth.create_access_token(
        data={"sub": db_user.username}, expires_delta=access_token_expires
    )
    return {"access_token": access_token, "token_type": "bearer"}

@app.post("/api/uploads/image", response_model=schemas.UploadedImage)
async def upload_reference_image(file: UploadFile = File(...), user: models.User = Depends(auth.get_current_user)):
    if not settings.CLOUDINARY_CLOUD_NAME:
        raise HTTPException(status_code=503, detail="Image uploads are not configured")
    if file.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=400, detail="Use a PNG, JPEG, WebP or GIF image")
    data = await file.read(settings.MAX_IMAGE_BYTES + 1)
    if len(data) > settings.MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Image is larger than 10 MB")

    result = await asyncio.to_thread(
        cloudinary.uploader.upload,
        data,
        folder=f"nnt/references/user-{user.id}",
        resource_type="image",
    )
    kind = await imagegen.classify_upload(result["secure_url"])
    return {"url": result["secure_url"], "width": result.get("width"), "height": result.get("height"), "kind": kind}


@app.post("/api/chat")
async def chat_endpoint(request: schemas.ChatRequest, user: models.User = Depends(auth.get_current_user)):
    text = request.message.strip()
    images = request.image_refs()
    if not text and not images:
        raise HTTPException(status_code=400, detail="Message is empty")
    if len(images) > settings.MAX_REFERENCE_IMAGES:
        raise HTTPException(status_code=400, detail=f"At most {settings.MAX_REFERENCE_IMAGES} images per message")
    # Only our own uploads are forwarded to the model
    own_prefix = f"https://res.cloudinary.com/{settings.CLOUDINARY_CLOUD_NAME}/"
    if any(not img.url.startswith(own_prefix) for img in images):
        raise HTTPException(status_code=400, detail="Reference images must be uploaded first")

    graph = app.state.graph
    config = {
        "configurable": {
            "thread_id": thread_id(user, request.conversation_id),
            "user_id": user.id,
            "conversation_id": request.conversation_id,
        },
        "recursion_limit": 25,
    }
    mention_note, _ = await library.describe_mentions(user.id, [m.model_dump() for m in request.mentions])
    previous = await graph.aget_state(config)
    new_messages = dangling_tool_results(previous.values.get("messages", []))
    new_messages.append(build_user_message(text, [img.model_dump() for img in images], mention_note))
    inputs = {"messages": new_messages, "image_kinds": {img.url: img.kind for img in images}}

    async def event_generator():
        streamed_step = None  # graph step whose tokens are being streamed
        try:
            async for mode, chunk in graph.astream(
                inputs, config, stream_mode=["messages", "updates", "custom"]
            ):
                if mode == "messages":
                    msg, meta = chunk
                    # Only the agent's own answer is streamed, not the summarizer's
                    if meta.get("langgraph_node") != "agent" or not isinstance(msg, AIMessageChunk):
                        continue
                    if isinstance(msg.content, str) and msg.content:
                        token = msg.content
                        if streamed_step is not None and meta["langgraph_step"] != streamed_step:
                            token = "\n\n" + token  # text from a later model call (after a search)
                        streamed_step = meta["langgraph_step"]
                        yield sse({"type": "token", "content": token})
                    continue

                if mode == "custom":
                    # reported from inside a tool: pipeline stages, and each image as soon as it's ready
                    if chunk.get("status"):
                        yield sse({"type": "status", "content": chunk["status"]})
                    if chunk.get("image"):
                        yield sse({"type": "image", **chunk["image"]})
                    if chunk.get("reference"):
                        yield sse({"type": "reference", **chunk["reference"]})
                    continue

                for node, output in chunk.items():
                    output = output or {}
                    if node == "sentiment" and output.get("sentiment"):
                        yield sse({"type": "sentiment", **output["sentiment"]})
                    elif node == "recall" and output.get("recalled"):
                        n = len(output["recalled"])
                        yield sse({"type": "status", "content": f"Recalled {n} related note{'s' if n > 1 else ''} from past chats…"})
                    elif node == "agent":
                        for m in output.get("messages", []):
                            for call in getattr(m, "tool_calls", None) or []:
                                if call["name"] == "generate_image":
                                    yield sse({"type": "status", "content": "Preparing your image…"})
                                elif call["name"] == "collect_references":
                                    yield sse({"type": "status", "content": "Planning searches…"})
                                elif call["name"] in LIBRARY_STATUS:
                                    yield sse({"type": "status", "content": LIBRARY_STATUS[call["name"]]})
                                else:
                                    query = call["args"].get("query", "")
                                    yield sse({"type": "status", "content": f"Searching the web for “{query}”…" if query else "Searching the web…"})
                    elif node == "tools":
                        for m in output.get("messages", []):
                            if m.name not in ("generate_image", "collect_references", *LIBRARY_STATUS):
                                yield sse({"type": "status", "content": "Reading search results…"})
            yield sse({"type": "done"})
        except Exception as e:
            logging.exception("chat stream failed")
            yield sse({"type": "error", "content": str(e)})

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/api/mentions")
async def mention_suggestions(
    q: str = "", kind: Literal["clients", "references", "all"] = "all", user: models.User = Depends(auth.get_current_user)
):
    """Suggestions for an @ in the composer: clients and reference topics."""
    return await library.suggest_mentions(user.id, q[:120], kind)


@app.delete("/api/conversations/{conversation_id}", status_code=204)
async def delete_conversation(conversation_id: str, user: models.User = Depends(auth.get_current_user)):
    await app.state.checkpointer.adelete_thread(thread_id(user, conversation_id))
    await memory.forget_conversation(user.id, conversation_id)


@app.get("/api/preferences")
async def get_preferences(user: models.User = Depends(auth.get_current_user)):
    """What NNT has learned about this user's design taste."""
    row = await asyncio.to_thread(taste.get_profile, user.id)
    if row is None:
        return {"profile": None, "signal_count": 0, "active": False}
    return {
        "profile": row.profile,
        "signal_count": row.signal_count,
        "active": row.signal_count >= settings.TASTE_MIN_SIGNALS,
        "updated_at": row.updated_at,
    }


@app.delete("/api/preferences", status_code=204)
async def reset_preferences(user: models.User = Depends(auth.get_current_user)):
    await asyncio.to_thread(taste.reset, user.id)
