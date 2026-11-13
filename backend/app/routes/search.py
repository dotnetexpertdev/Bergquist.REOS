"""Assistant question and voice transcription routes."""

from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import require_auth, require_csrf
from ..database import get_session
from ..providers.registry import ai
from ..schemas import SearchRequest, SearchResponse, TranscriptionResponse
from ..services.assistant import ask

router = APIRouter(prefix="/api", tags=["search"])

MAX_AUDIO_BYTES = 25 * 1024 * 1024
MIN_AUDIO_BYTES = 1000  # anything smaller is an empty or cancelled recording
AUDIO_EXTENSIONS = {
    "audio/webm": ".webm", "video/webm": ".webm", "audio/ogg": ".ogg", "audio/mp4": ".mp4",
    "audio/x-m4a": ".m4a", "audio/m4a": ".m4a", "audio/mpeg": ".mp3", "audio/wav": ".wav",
    "audio/x-wav": ".wav", "audio/flac": ".flac",
}


@router.post("/search", response_model=SearchResponse)
async def search_documents(request: SearchRequest, username: str = Depends(require_auth),
                           db: AsyncSession = Depends(get_session), _csrf: None = Depends(require_csrf)):
    """Answer a typed or transcribed question from stored records. Read-only."""
    if not request.query.strip() and not request.select_document_id:
        raise HTTPException(status_code=400, detail="Ask a question first")
    return await ask(db, request.query.strip(), request.context_document_id, request.select_document_id)


@router.post("/transcribe", response_model=TranscriptionResponse)
async def transcribe_audio_route(audio: UploadFile = File(...), username: str = Depends(require_auth),
                                 _csrf: None = Depends(require_csrf)):
    """Transcribe one push-to-talk turn. Audio is processed in memory and not stored."""
    content_type = (audio.content_type or "").split(";")[0].strip().lower()
    if content_type not in AUDIO_EXTENSIONS:
        return TranscriptionResponse(success=False, error=f"Unsupported audio format ({content_type or 'unknown'}).")
    content = await audio.read(MAX_AUDIO_BYTES + 1)
    if len(content) > MAX_AUDIO_BYTES:
        return TranscriptionResponse(success=False, error="Recording is too long. Keep it under a minute or two.")
    if len(content) < MIN_AUDIO_BYTES:
        return TranscriptionResponse(success=False, error="The recording was empty. Hold the button and speak, "
                                                          "or type your question.")
    filename = f"turn{AUDIO_EXTENSIONS[content_type]}"
    if audio.filename and Path(audio.filename).suffix:
        filename = f"turn{Path(audio.filename).suffix}"
    transcript, error = await ai().transcribe_audio(content, filename, content_type)
    del content
    if error:
        return TranscriptionResponse(success=False, error=error)
    return TranscriptionResponse(success=True, transcript=transcript)
