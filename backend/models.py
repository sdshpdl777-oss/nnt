from datetime import datetime, timezone
from sqlalchemy import Boolean, Column, Integer, String, Text, DateTime, ForeignKey, JSON, REAL, UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.dialects.postgresql import ARRAY
from database import Base

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    email = Column(String, unique=True, index=True)
    hashed_password = Column(String)

class MemoryChunk(Base):
    """One embedded chunk of a past conversation turn, used for long-term recall."""
    __tablename__ = "memory_chunks"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    conversation_id = Column(String, index=True, nullable=False)
    content = Column(Text, nullable=False)
    embedding = Column(JSON().with_variant(ARRAY(REAL), "postgresql"), nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class DesignSignal(Base):
    """One piece of evidence about a user's design taste: their reaction to a generated
    image, or a reference image they brought."""
    __tablename__ = "design_signals"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    conversation_id = Column(String, index=True, nullable=False)
    kind = Column(String, nullable=False)  # "feedback" | "reference"
    image_url = Column(String, nullable=False)
    verdict = Column(String, nullable=False)  # liked | disliked | mixed | interest
    details = Column(JSON, nullable=False)  # liked/disliked aspects, design tags, user's words
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class TasteProfile(Base):
    """A user's learned design preferences, rebuilt from their DesignSignals."""
    __tablename__ = "taste_profiles"

    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    profile = Column(JSON, nullable=False)
    signal_count = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class Client(Base):
    """A customer of the user's agency. Holds master files, brand assets and references."""
    __tablename__ = "clients"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_clients_user_name"),)

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    name = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    files = relationship("ClientFile", back_populates="client", cascade="all, delete-orphan", passive_deletes=True)

class ClientFile(Base):
    """One master file or brand asset of a client, stored on Cloudinary."""
    __tablename__ = "client_files"

    id = Column(Integer, primary_key=True)
    client_id = Column(Integer, ForeignKey("clients.id", ondelete="CASCADE"), index=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    category = Column(String, nullable=False)  # master | brand
    kind = Column(String)  # brand: logo | asset | document; master: image | document | other
    name = Column(String, nullable=False)  # original filename
    url = Column(String, nullable=False)
    public_id = Column(String, nullable=False)
    resource_type = Column(String, nullable=False)  # cloudinary: image | video | raw
    content_type = Column(String)
    bytes = Column(Integer)
    width = Column(Integer)
    height = Column(Integer)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    client = relationship("Client", back_populates="files")

class Reference(Base):
    """A reference design in the user's shared library, usable for every client."""
    __tablename__ = "shared_references"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    name = Column(String, nullable=False)  # original filename, or a title for collected designs
    collection = Column(String)  # search topic it was collected for; None for the user's own uploads
    source_url = Column(String)  # where it was found on the web; None for uploads
    phash = Column(String)  # 64-bit dHash (hex), to skip near-duplicates when collecting
    description = Column(Text)  # one-line caption of what the design shows, so it can be searched
    embedding = Column(JSON().with_variant(ARRAY(REAL), "postgresql"))  # of name + description, for meaning search
    url = Column(String, nullable=False)
    public_id = Column(String, nullable=False)
    resource_type = Column(String, nullable=False)
    content_type = Column(String)
    bytes = Column(Integer)
    width = Column(Integer)
    height = Column(Integer)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class BrandKit(Base):
    """A client's brand details (logo, colors, fonts, footer/contact details, style) extracted from
    their brand files and website, then reviewed by the user. Applied when designing for the client."""
    __tablename__ = "brand_kits"

    id = Column(Integer, primary_key=True)
    client_id = Column(Integer, ForeignKey("clients.id", ondelete="CASCADE"), unique=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    data = Column(JSON, nullable=False)  # brandkit.KitData
    website = Column(String)
    sources = Column(JSON, nullable=False, default=list)  # handles of the brand files it was extracted from
    edited = Column(Boolean, nullable=False, default=False)  # the user changed it after extraction
    extracted_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
