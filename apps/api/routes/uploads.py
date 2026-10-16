from fastapi import APIRouter
from pydantic import BaseModel
from typing import List, Optional
import uuid

router = APIRouter()


class FileRegistrationRequest(BaseModel):
    filename: str
    file_size_bytes: int
    mime_type: str
    po_reference: Optional[str] = None


class PresignedUploadResponse(BaseModel):
    upload_id: str
    filename: str
    s3_key: str
    upload_url: str
    expires_in_seconds: int


@router.post("/presign", response_model=PresignedUploadResponse)
def generate_presigned_upload(req: FileRegistrationRequest):
    upload_id = str(uuid.uuid4())
    s3_key = f"uploads/{upload_id}/{req.filename}"
    return PresignedUploadResponse(
        upload_id=upload_id,
        filename=req.filename,
        s3_key=s3_key,
        upload_url=f"http://localhost:9000/recorda-documents/{s3_key}",
        expires_in_seconds=900,
    )
