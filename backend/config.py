from urllib.parse import quote_plus
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    DB_NAME: str = "nntdb"
    DB_PASS: str = ""
    DB_USER: str = "alexchaudhary" # Defaulting to mac username, adjust if needed
    DB_HOST: str = "localhost"
    DB_PORT: str = "5432"
    JWT_SECRET: str = ""
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 720  # the frontend refreshes the token before it runs out

    OPENAI_API_KEY: str = ""
    TAVILY_API_KEY: str = ""
    CLOUDINARY_CLOUD_NAME: str = ""
    CLOUDINARY_API_KEY: str = ""
    CLOUDINARY_API_SECRET: str = ""

    CHAT_MODEL: str = "gpt-4o"
    SUMMARY_MODEL: str = "gpt-4o-mini"
    SENTIMENT_MODEL: str = "gpt-4.1-mini"
    EMBEDDING_MODEL: str = "text-embedding-3-small"

    # Image pipeline. gpt-image-2.5-sunburst is OpenAI's highest-fidelity image model
    # (2026-09); gpt-image-2.5-flare is the faster option at gpt-image-2 quality.
    IMAGE_MODEL: str = "gpt-image-2.5-sunburst"
    IMAGE_FALLBACK_MODEL: str = "gpt-image-2"
    IMAGE_QUALITY: str = "medium"  # low | medium | high
    ANALYSIS_MODEL: str = "gpt-4.1"  # brand analyst + visual forensics (vision, JSON)
    PROMPT_MODEL: str = "gpt-4.1"    # prompt builder
    CLASSIFY_MODEL: str = "gpt-4.1-mini"  # logo-vs-reference tagging, search-query planning

    # Multi-logo batches: one image per logo
    MAX_LOGOS_PER_BATCH: int = 5
    BATCH_CONCURRENCY: int = 3  # logos processed at once (image API rate limits, cost spikes)
    WEB_INSPIRATION_IMAGES: int = 2  # web posts analyzed per logo (style only, never pixels)
    # OpenAI limits image *inputs* per minute per organization (this account: 5 on gpt-image-*).
    # Raise this if your OpenAI usage tier allows more.
    IMAGE_INPUTS_PER_MINUTE: int = 5
    IMAGE_RATE_LIMIT_RETRIES: int = 3

    # Short-term memory: once a thread's live messages exceed MAX_CONTEXT_TOKENS,
    # older turns are folded into a running summary, keeping ~KEEP_RECENT_TOKENS verbatim.
    MAX_CONTEXT_TOKENS: int = 6000
    KEEP_RECENT_TOKENS: int = 2000

    # Long-term memory (RAG over past turns)
    CHUNK_TOKENS: int = 350
    CHUNK_OVERLAP_TOKENS: int = 50
    RECALL_TOP_K: int = 4
    RECALL_MIN_SCORE: float = 0.3

    # Taste learning: preferences are applied to new images once a user has this many signals
    TASTE_MIN_SIGNALS: int = 2
    TASTE_MAX_SIGNALS: int = 40  # most recent signals used to rebuild a profile

    MAX_COLLECT_REFERENCES: int = 30  # designs one collect_references call may save

    MAX_REFERENCE_IMAGES: int = 6  # e.g. 5 logos + 1 style reference
    MAX_IMAGE_BYTES: int = 10 * 1024 * 1024
    MAX_MASTER_FILE_BYTES: int = 100 * 1024 * 1024  # Cloudinary free plan caps raw files at 10 MB, paid at 100+

    @property
    def psycopg_dsn(self) -> str:
        return (
            f"postgresql://{quote_plus(self.DB_USER)}:{quote_plus(self.DB_PASS)}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
        )

    class Config:
        env_file = ".env"
        extra = "ignore"

settings = Settings()
