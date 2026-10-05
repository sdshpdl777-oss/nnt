# TechnoFort AI Studio

An AI design studio for agencies. Chat with an agent that generates on-brand marketing images,
remembers your preferences, and works from each client's brand kit and reference library.

- **Backend:** FastAPI + LangGraph (OpenAI models), PostgreSQL with LangGraph checkpoints
- **Frontend:** React 18 + Vite

## Features

- **Chat agent** with streaming responses and persistent conversations (LangGraph + Postgres checkpointer)
- **Image generation** via OpenAI image models, with multi-logo batches and web-sourced style inspiration
- **Clients & brand kits:** per-client files, auto-extracted brand kits (colors, fonts, tone), editable
- **Reference library:** upload images or import from a URL
- **Memory & taste:** long-term user memory and learned style preferences
- **@mentions** to pull clients and references into a chat
- JWT authentication

## Project structure

```
backend/
  main.py        FastAPI app: auth, uploads, chat (streaming), preferences
  agent.py       LangGraph agent graph
  imagegen.py    image generation pipeline
  clients.py     clients, brand kits, files, references API
  brandkit.py    brand kit extraction
  memory.py      long-term memory (embeddings)
  taste.py       learned style preferences
  config.py      settings (env vars)
frontend/
  src/App.jsx    app shell and routing
  src/components Chat, Sidebar, Clients, BrandKit, Mentions
```

## Getting started

### Prerequisites

- Python 3.12+
- Node.js 18+
- PostgreSQL (falls back to SQLite if Postgres is unavailable, but chat history needs Postgres)
- API keys: OpenAI, Tavily (web search), Cloudinary (image hosting)

### Backend

```bash
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in the values
uvicorn main:app --reload --port 8080
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

The frontend expects the API at `http://localhost:8080` (set in `frontend/src/conversations.js`).

## Configuration

Set these in `backend/.env`:

| Variable | Purpose |
| --- | --- |
| `OPENAI_API_KEY` | Chat, embeddings, and image generation |
| `TAVILY_API_KEY` | Web search for the agent |
| `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`, `CLOUDINARY_API_SECRET` | Image storage |
| `DB_NAME`, `DB_USER`, `DB_PASS` | PostgreSQL connection (`DB_HOST`/`DB_PORT` default to `localhost:5432`) |
| `JWT_SECRET` | Signs auth tokens |

Model choices (chat, image, analysis) can be overridden with the env vars defined in `backend/config.py`.

---

© TechnoFort
