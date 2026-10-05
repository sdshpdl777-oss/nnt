"""Mood detection for user messages, used to adapt the assistant's tone."""
from typing import Literal, Optional

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from config import settings

Emotion = Literal[
    "excited", "happy", "grateful", "curious", "neutral",
    "confused", "impatient", "frustrated", "disappointed", "angry", "anxious",
]


class Sentiment(BaseModel):
    label: Literal["positive", "neutral", "negative"]
    emotion: Emotion
    confidence: float = Field(ge=0, le=1, description="How clearly the message shows this emotion")


CLASSIFIER_PROMPT = """Classify the mood of the user's LATEST message in a chat with a creative design assistant.
The assistant's previous reply is given only as context (e.g. "no, that's wrong" after a reply signals frustration).
Judge the user's tone, not the topic: "a poster for a funeral home" is neutral; "this is the third time I've asked" is frustrated.
Plain requests and instructions are neutral. Only pick a strong emotion when the wording clearly shows it."""

# How the assistant should respond to each emotion. Neutral and mild cases add nothing.
TONE_GUIDANCE = {
    "frustrated": "The user seems frustrated. Open with one short line that restates what they actually want, in your own words, so they know you heard them. No long apologies, then fix the problem directly. Keep the whole reply noticeably shorter than usual.",
    "angry": "The user seems upset. Stay calm and respectful, take responsibility briefly if something went wrong, and focus on fixing it.",
    "disappointed": "The user seems disappointed with the result. Briefly acknowledge what missed the mark and offer a concrete improvement.",
    "impatient": "The user seems short on time. Be as brief as possible and get straight to the result.",
    "confused": "The user seems confused. Explain more simply, step by step, and avoid jargon.",
    "anxious": "The user seems anxious or under pressure. Be reassuring and give clear, concrete next steps.",
    "excited": "The user is excited. Match their energy briefly, without overdoing it.",
    "happy": "The user is happy with how things are going. Keep the warm tone.",
    "grateful": "The user is thankful. Acknowledge it briefly and naturally.",
}

classifier = ChatOpenAI(
    model=settings.SENTIMENT_MODEL, temperature=0, api_key=settings.OPENAI_API_KEY
).with_structured_output(Sentiment)


def _text(msg: BaseMessage) -> str:
    if isinstance(msg.content, str):
        return msg.content
    return "\n".join(p["text"] for p in msg.content if isinstance(p, dict) and p.get("type") == "text")


async def classify(messages: list[BaseMessage]) -> Optional[Sentiment]:
    """Mood of the latest user message, or None if it has no text (images only)."""
    latest = messages[-1]
    text = _text(latest).strip()
    if not isinstance(latest, HumanMessage) or not text:
        return None
    previous = next((m for m in reversed(messages[:-1]) if isinstance(m, AIMessage) and _text(m).strip()), None)
    context = f"Assistant's previous reply:\n{_text(previous)[:1500]}\n\n" if previous else ""
    return await classifier.ainvoke([
        ("system", CLASSIFIER_PROMPT),
        ("human", f"{context}User's latest message:\n{text[:4000]}"),
    ])


def tone_guidance(sentiment: Optional[dict]) -> Optional[str]:
    if not sentiment or sentiment.get("confidence", 0) < 0.5:
        return None
    return TONE_GUIDANCE.get(sentiment["emotion"])
