from typing import Literal, Union

from pydantic import BaseModel, EmailStr, Field

class UserBase(BaseModel):
    username: str
    email: EmailStr

class UserCreate(UserBase):
    password: str

class UserLogin(BaseModel):
    username: str
    password: str

class User(UserBase):
    id: int

    class Config:
        from_attributes = True

class Token(BaseModel):
    access_token: str
    token_type: str

class ImageRef(BaseModel):
    url: str
    kind: Literal["logo", "reference"] = "reference"

class Mention(BaseModel):
    """Something the user @-mentioned in the composer: a client, or a reference topic."""
    type: Literal["client", "references"]
    label: str = Field(min_length=1, max_length=120)
    id: int | None = None  # client id
    query: str | None = Field(default=None, max_length=120)  # reference topic

class ChatRequest(BaseModel):
    message: str = ""
    conversation_id: str = Field(min_length=1, max_length=100)
    images: list[Union[ImageRef, str]] = []  # plain URLs are treated as references
    mentions: list[Mention] = Field(default=[], max_length=6)

    def image_refs(self) -> list[ImageRef]:
        return [ImageRef(url=i) if isinstance(i, str) else i for i in self.images]

class UploadedImage(BaseModel):
    url: str
    width: int | None = None
    height: int | None = None
    kind: Literal["logo", "reference"] = "reference"
