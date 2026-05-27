"""Shared Pydantic configuration for inter-agent messages."""

from pydantic import BaseModel, ConfigDict


class MessageModel(BaseModel):
    """Base class for every JSON-serializable protocol message."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
