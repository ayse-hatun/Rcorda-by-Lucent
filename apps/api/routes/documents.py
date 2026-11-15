import hashlib
import io
import uuid
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from apps.api.database import get_db
from apps.api.models import Document, PurchaseOrder, Supplier
from apps.api.schemas import (
    ClassificationResponse,
    ClassifyDocumentRequest,
    DocumentResponse,
    ExtractDocumentRequest,
    ExtractionResponse,
    HumanReviewRequest,
    OcrRunResponse,
    normalize_document_type,
)
from apps.api.services.classification_service import (
    ClassificationInputMissingError,
    ClassificationNotFoundError,
    DocumentClassificationService,
    InvalidDocumentTypeError,
)
from apps.api.services.extraction_service import (
    DocumentExtractionService,
    ExtractionNotFoundError,
    OcrRunMissingError,
    UnknownDocumentTypeError,
)
from apps.api.services.ocr_service import (
    DocumentNotFoundError,
    DocumentOcrService,
)
from apps.api.storage import (
    FileNotFoundInStorageError,
    StorageBackend,
    StorageError,
    get_storage_service,
)
from packages.ocr.base import (
    CorruptedDocumentError,
    OcrError,
    UnsupportedDocumentFormatError,
)

router = APIRouter()

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg"}
MIME_MAP = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


def _validate_file_content(ext: str, content: bytes) -> None:
    """Validate file content using magic numbers and format verification."""
    if ext == ".pdf":
        if not content.startswith(b"%PDF-"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid PDF content: missing %PDF- header",
            )
        try:
            import pypdf

            reader = pypdf.PdfReader(io.BytesIO(content))
            if len(reader.pages) == 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid PDF content: document contains no pages",
                )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Malformed PDF document: {exc}",
            )

    elif ext in (".png", ".jpg", ".jpeg"):
        if ext == ".png" and not content.startswith(b"\x89PNG\r\n\x1a\n"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid PNG content: missing PNG signature",
            )
        elif ext in (".jpg", ".jpeg") and not (
            content.startswith(b"\xff\xd8\xff") or content.startswith(b"\xff\xd8")
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid JPEG content: missing JPEG signature",
            )

        try:
            from PIL import Image

            img = Image.open(io.BytesIO(content))
            img.verify()
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Malformed image file: {exc}",
            )


@router.post(
    "/upload",
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload and register a document",
)
async def upload_document(
    file: UploadFile = File(...),
    document_type: str = Form(..., description="Document type: PURCHASE_ORDER, DELIVERY_CHALLAN, GOODS_RECEIPT_NOTE, SUPPLIER_INVOICE"),
    document_number: Optional[str] = Form(None),
    po_reference: Optional[str] = Form(None),
    supplier_id: Optional[uuid.UUID] = Form(None),
    purchase_order_id: Optional[uuid.UUID] = Form(None),
    db: Session = Depends(get_db),
    storage: StorageBackend = Depends(get_storage_service),
):
    # 1. Validate document type
    try:
        validated_doc_type = normalize_document_type(document_type)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    # 2. Validate filename and extension
    if not file.filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Filename is required",
        )

    # Prevent path traversal in original filename
    clean_filename = Path(file.filename).name
    ext = Path(clean_filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported file extension '{ext}'. Allowed extensions: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
        )

    # 3. Read content and check size limit (10MB)
    content = await file.read()
    file_size = len(content)

    if file_size == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file is empty",
        )

    if file_size > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds maximum allowed size of 10 MB (received {file_size} bytes)",
        )

    # 4. Validate file content signatures and structure
    _validate_file_content(ext, content)

    # 5. Check SHA-256 for duplicate rejection
    sha256 = hashlib.sha256(content).hexdigest()
    existing_doc = db.query(Document).filter(Document.sha256 == sha256).first()
    if existing_doc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Document with identical checksum already exists (document_id: {existing_doc.id})",
        )

    # 6. Validate supplier and purchase order relationships if provided
    if supplier_id:
        supplier = db.query(Supplier).filter(Supplier.id == supplier_id).first()
        if not supplier:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Supplier '{supplier_id}' not found",
            )

    if purchase_order_id:
        po = db.query(PurchaseOrder).filter(PurchaseOrder.id == purchase_order_id).first()
        if not po:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Purchase order '{purchase_order_id}' not found",
            )
        if not po_reference and po.po_number:
            po_reference = po.po_number

        # If supplier is also given, verify consistency if PO is linked to a supplier
        if supplier_id and po.supplier_id and po.supplier_id != supplier_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Supplier ID does not match the supplier on the referenced purchase order",
            )

    # Determine MIME type
    mime_type = MIME_MAP.get(ext, file.content_type or "application/octet-stream")

    # 7. Generate a unique, safe storage key
    document_id = uuid.uuid4()
    safe_ext = ext.lstrip(".")
    storage_key = f"documents/{validated_doc_type.lower()}/{document_id}.{safe_ext}"

    # 8. Store file in storage backend
    try:
        storage.put_object(storage_key, content, content_type=mime_type)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Storage service failure: {exc}",
        )

    # 9. Create and persist document metadata in PostgreSQL
    doc = Document(
        id=document_id,
        document_type=validated_doc_type,
        document_number=document_number,
        po_reference=po_reference,
        purchase_order_id=purchase_order_id,
        supplier_id=supplier_id,
        sha256=sha256,
        s3_key=storage_key,
        file_name=clean_filename,
        mime_type=mime_type,
        file_size=file_size,
        status="INGESTED",
    )

    try:
        db.add(doc)
        db.commit()
        db.refresh(doc)
    except Exception as exc:
        db.rollback()
        # Clean up stored object to prevent orphaned storage objects
        try:
            storage.delete_object(storage_key)
        except Exception:
            pass
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database persistence failure: {exc}",
        )

    return doc


@router.get(
    "",
    response_model=List[DocumentResponse],
    summary="List documents with filtering and pagination",
)
def list_documents(
    document_type: Optional[str] = Query(None, description="Filter by document type"),
    po_reference: Optional[str] = Query(None, description="Filter by PO reference"),
    supplier_id: Optional[uuid.UUID] = Query(None, description="Filter by supplier ID"),
    purchase_order_id: Optional[uuid.UUID] = Query(None, description="Filter by purchase order ID"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    query = db.query(Document)

    if document_type:
        try:
            validated_type = normalize_document_type(document_type)
            query = query.filter(Document.document_type == validated_type)
        except ValueError:
            query = query.filter(Document.document_type == document_type.strip().upper())

    if po_reference:
        query = query.filter(Document.po_reference == po_reference.strip())

    if supplier_id:
        query = query.filter(Document.supplier_id == supplier_id)

    if purchase_order_id:
        query = query.filter(Document.purchase_order_id == purchase_order_id)

    query = query.order_by(Document.created_at.desc())
    return query.offset(offset).limit(limit).all()


@router.get(
    "/{document_id}",
    response_model=DocumentResponse,
    summary="Retrieve document metadata",
)
def get_document(
    document_id: uuid.UUID,
    db: Session = Depends(get_db),
):
    doc = db.query(Document).filter(Document.id == document_id).first()
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Document '{document_id}' not found",
        )
    return doc


@router.get(
    "/{document_id}/download",
    summary="Download original document file",
)
def download_document(
    document_id: uuid.UUID,
    db: Session = Depends(get_db),
    storage: StorageBackend = Depends(get_storage_service),
):
    doc = db.query(Document).filter(Document.id == document_id).first()
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Document '{document_id}' not found",
        )

    if not doc.s3_key:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Storage key not found for this document",
        )

    if not storage.exists(doc.s3_key):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Original file not found in storage",
        )

    try:
        stream = storage.get_object(doc.s3_key)
    except FileNotFoundInStorageError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Original file not found in storage",
        )
    except StorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to retrieve file from storage: {exc}",
        )

    original_filename = doc.file_name or f"{doc.id}.bin"
    encoded_filename = quote(original_filename)

    headers = {
        "Content-Disposition": f'attachment; filename="{original_filename}"; filename*=UTF-8\'\'{encoded_filename}',
    }
    if doc.file_size:
        headers["Content-Length"] = str(doc.file_size)

    return StreamingResponse(
        stream,
        media_type=doc.mime_type or "application/octet-stream",
        headers=headers,
    )


def get_ocr_service(
    db: Session = Depends(get_db),
    storage: StorageBackend = Depends(get_storage_service),
) -> DocumentOcrService:
    return DocumentOcrService(db=db, storage=storage)


def get_classification_service(
    db: Session = Depends(get_db),
    storage: StorageBackend = Depends(get_storage_service),
) -> DocumentClassificationService:
    return DocumentClassificationService(db=db, storage=storage)


def get_extraction_service(
    db: Session = Depends(get_db),
) -> DocumentExtractionService:
    return DocumentExtractionService(db=db)


# ==============================================================================
# OCR Pipeline Endpoints
# ==============================================================================


@router.post(
    "/{document_id}/ocr",
    response_model=OcrRunResponse,
    status_code=status.HTTP_200_OK,
    summary="Trigger OCR extraction on a document",
)
def process_document_ocr(
    document_id: uuid.UUID,
    service: DocumentOcrService = Depends(get_ocr_service),
):
    try:
        run = service.run_ocr(document_id)
        return run
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except FileNotFoundInStorageError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except (UnsupportedDocumentFormatError, CorruptedDocumentError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        )
    except OcrError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Unexpected OCR failure: {exc}",
        )


@router.get(
    "/{document_id}/ocr",
    response_model=List[OcrRunResponse],
    summary="List all OCR runs for a document",
)
def list_document_ocr_runs(
    document_id: uuid.UUID,
    service: DocumentOcrService = Depends(get_ocr_service),
):
    try:
        return service.list_runs(document_id)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.get(
    "/{document_id}/ocr/{ocr_run_id}",
    response_model=OcrRunResponse,
    summary="Retrieve a specific OCR run with page breakdown",
)
def get_document_ocr_run(
    document_id: uuid.UUID,
    ocr_run_id: uuid.UUID,
    service: DocumentOcrService = Depends(get_ocr_service),
):
    try:
        run = service.get_run(document_id, ocr_run_id)
        if not run:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"OCR run '{ocr_run_id}' not found for document '{document_id}'",
            )
        return run
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


# ==============================================================================
# Document Classification Endpoints
# ==============================================================================


@router.post(
    "/{document_id}/classify",
    response_model=ClassificationResponse,
    status_code=status.HTTP_200_OK,
    summary="Trigger document classification",
)
def classify_document(
    document_id: uuid.UUID,
    payload: Optional[ClassifyDocumentRequest] = None,
    service: DocumentClassificationService = Depends(get_classification_service),
):
    try:
        raw_text = payload.raw_text if payload else None
        actor = payload.actor if payload else None
        return service.classify_document(
            document_id=document_id, raw_text=raw_text, actor=actor
        )
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ClassificationInputMissingError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Classification error: {exc}",
        )


@router.get(
    "/{document_id}/classifications",
    response_model=List[ClassificationResponse],
    summary="List classification history for a document",
)
def list_document_classifications(
    document_id: uuid.UUID,
    service: DocumentClassificationService = Depends(get_classification_service),
):
    try:
        return service.list_classifications(document_id)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.get(
    "/{document_id}/classifications/{classification_id}",
    response_model=ClassificationResponse,
    summary="Retrieve a specific classification record",
)
def get_document_classification(
    document_id: uuid.UUID,
    classification_id: uuid.UUID,
    service: DocumentClassificationService = Depends(get_classification_service),
):
    try:
        record = service.get_classification(document_id, classification_id)
        if not record:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Classification '{classification_id}' not found for document '{document_id}'.",
            )
        return record
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.post(
    "/{document_id}/classifications/{classification_id}/review",
    response_model=ClassificationResponse,
    summary="Submit human review / correction for a classification",
)
def review_document_classification(
    document_id: uuid.UUID,
    classification_id: uuid.UUID,
    payload: HumanReviewRequest,
    service: DocumentClassificationService = Depends(get_classification_service),
):
    try:
        return service.submit_human_review(
            document_id=document_id,
            classification_id=classification_id,
            actual_type=payload.actual_type,
            reviewer_id=payload.reviewer_id,
            notes=payload.notes,
        )
    except (DocumentNotFoundError, ClassificationNotFoundError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except InvalidDocumentTypeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        )


# ==============================================================================
# Structured Data Extraction Endpoints
# ==============================================================================


@router.post(
    "/{document_id}/extract",
    response_model=ExtractionResponse,
    status_code=status.HTTP_200_OK,
    summary="Trigger structured data extraction on a document",
)
def extract_document_data(
    document_id: uuid.UUID,
    payload: Optional[ExtractDocumentRequest] = None,
    service: DocumentExtractionService = Depends(get_extraction_service),
):
    try:
        ocr_run_id = payload.ocr_run_id if payload else None
        doc_type = payload.document_type if payload else None
        return service.extract_document(
            document_id=document_id,
            ocr_run_id=ocr_run_id,
            document_type_override=doc_type,
        )
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except (OcrRunMissingError, UnknownDocumentTypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Extraction failure: {exc}",
        )


@router.get(
    "/{document_id}/extractions",
    response_model=List[ExtractionResponse],
    summary="List extraction run history for a document",
)
def list_document_extractions(
    document_id: uuid.UUID,
    service: DocumentExtractionService = Depends(get_extraction_service),
):
    try:
        return service.list_extractions(document_id)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.get(
    "/{document_id}/extractions/{extraction_id}",
    response_model=ExtractionResponse,
    summary="Retrieve a specific extraction run",
)
def get_document_extraction(
    document_id: uuid.UUID,
    extraction_id: uuid.UUID,
    service: DocumentExtractionService = Depends(get_extraction_service),
):
    try:
        record = service.get_extraction(document_id, extraction_id)
        if not record:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Extraction '{extraction_id}' not found for document '{document_id}'.",
            )
        return record
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))

