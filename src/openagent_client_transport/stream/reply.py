"""Client-side accumulated batched reply, independent of StreamSession."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class BatchedReply:
    text: str = ""
    audio_chunks: list[bytes] = field(default_factory=list)
    audio_format: str | None = None
    audio_mime: str | None = None
    voice_id: str | None = None
    attachments: list[dict] = field(default_factory=list)
    model: str | None = None
    errored: bool = False
    error_text: str | None = None

    @property
    def audio_bytes(self) -> bytes | None:
        return b"".join(self.audio_chunks) if self.audio_chunks else None
