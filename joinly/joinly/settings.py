"""The one setting the platform controllers read: the bot's own name in the meeting."""
import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Settings:
    """name: how the bot appears in the meeting, so it can ignore itself as a speaker."""

    name: str


def get_settings() -> Settings:
    return Settings(name=os.environ.get("JOINLY_NAME") or "Colleague AI")
