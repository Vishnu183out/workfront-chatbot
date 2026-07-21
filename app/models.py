from pydantic import BaseModel


class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    reply: str
    tools_used: list[str]
    awaiting_confirmation: bool


class MeResponse(BaseModel):
    authenticated: bool
    name: str | None = None
