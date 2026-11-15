import io
import os
import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import BinaryIO, Iterator, Optional, Union

from apps.api.config import Settings, load_settings


class StorageError(Exception):
    """Base exception for storage operations."""
    pass


class FileNotFoundInStorageError(StorageError):
    """Raised when an object key is not found in storage."""
    pass


class StorageBackend(ABC):
    """Abstract storage backend interface."""

    @abstractmethod
    def put_object(
        self,
        key: str,
        data: Union[bytes, BinaryIO],
        content_type: Optional[str] = None,
    ) -> int:
        """Store an object and return the number of bytes stored."""
        raise NotImplementedError

    @abstractmethod
    def get_object(self, key: str, chunk_size: int = 65536) -> Iterator[bytes]:
        """Stream an object's contents in chunks."""
        raise NotImplementedError

    @abstractmethod
    def get_object_bytes(self, key: str) -> bytes:
        """Read and return an object's entire contents."""
        raise NotImplementedError

    @abstractmethod
    def delete_object(self, key: str) -> bool:
        """Delete an object. Returns True if deleted, False if not found."""
        raise NotImplementedError

    @abstractmethod
    def exists(self, key: str) -> bool:
        """Check whether an object exists in storage."""
        raise NotImplementedError


class FileSystemStorage(StorageBackend):
    """Real filesystem-backed storage with strict path-traversal prevention."""

    def __init__(self, base_dir: Union[str, Path]):
        self.base_dir = Path(base_dir).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _resolve_path(self, key: str) -> Path:
        sanitized_key = key.lstrip("/\\")
        target_path = (self.base_dir / sanitized_key).resolve()
        try:
            target_path.relative_to(self.base_dir)
        except ValueError as exc:
            raise StorageError(f"Path traversal detected for storage key: {key}") from exc
        return target_path

    def put_object(
        self,
        key: str,
        data: Union[bytes, BinaryIO],
        content_type: Optional[str] = None,
    ) -> int:
        target = self._resolve_path(key)
        target.parent.mkdir(parents=True, exist_ok=True)

        try:
            if isinstance(data, (bytes, bytearray)):
                target.write_bytes(data)
                return len(data)
            else:
                data.seek(0)
                bytes_written = 0
                with target.open("wb") as f:
                    while True:
                        chunk = data.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                        bytes_written += len(chunk)
                return bytes_written
        except Exception as exc:
            if target.exists():
                try:
                    target.unlink()
                except Exception:
                    pass
            raise StorageError(f"Failed to write object to filesystem: {exc}") from exc

    def get_object(self, key: str, chunk_size: int = 65536) -> Iterator[bytes]:
        target = self._resolve_path(key)
        if not target.is_file():
            raise FileNotFoundInStorageError(f"Object not found: {key}")

        try:
            with target.open("rb") as f:
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    yield chunk
        except Exception as exc:
            raise StorageError(f"Failed to read object from filesystem: {exc}") from exc

    def get_object_bytes(self, key: str) -> bytes:
        target = self._resolve_path(key)
        if not target.is_file():
            raise FileNotFoundInStorageError(f"Object not found: {key}")
        try:
            return target.read_bytes()
        except Exception as exc:
            raise StorageError(f"Failed to read object: {exc}") from exc

    def delete_object(self, key: str) -> bool:
        target = self._resolve_path(key)
        if target.is_file():
            try:
                target.unlink()
                return True
            except Exception as exc:
                raise StorageError(f"Failed to delete object from filesystem: {exc}") from exc
        return False

    def exists(self, key: str) -> bool:
        target = self._resolve_path(key)
        return target.is_file()


class S3DocumentStorage(StorageBackend):
    """S3-compatible object storage backend supporting AWS S3 and local MinIO."""

    def __init__(
        self,
        bucket: str,
        endpoint_url: Optional[str] = None,
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        region: str = "us-east-1",
    ):
        import boto3
        from botocore.config import Config

        self.bucket = bucket
        self.endpoint_url = endpoint_url
        self.region = region

        config = Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            retries={"max_attempts": 3, "mode": "standard"},
        )
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=config,
        )

    def put_object(
        self,
        key: str,
        data: Union[bytes, BinaryIO],
        content_type: Optional[str] = None,
    ) -> int:
        from botocore.exceptions import ClientError

        extra_args = {}
        if content_type:
            extra_args["ContentType"] = content_type

        try:
            if isinstance(data, (bytes, bytearray)):
                body = data
                size = len(data)
            else:
                data.seek(0)
                body = data.read()
                size = len(body)

            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=body,
                **extra_args,
            )
            return size
        except ClientError as exc:
            raise StorageError(f"Failed to put object in S3/MinIO: {exc}") from exc

    def get_object(self, key: str, chunk_size: int = 65536) -> Iterator[bytes]:
        from botocore.exceptions import ClientError

        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            body = response["Body"]
            while True:
                chunk = body.read(chunk_size)
                if not chunk:
                    break
                yield chunk
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in ("NoSuchKey", "404"):
                raise FileNotFoundInStorageError(f"Object not found in S3/MinIO: {key}") from exc
            raise StorageError(f"Failed to get object from S3/MinIO: {exc}") from exc

    def get_object_bytes(self, key: str) -> bytes:
        from botocore.exceptions import ClientError

        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            return response["Body"].read()
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in ("NoSuchKey", "404"):
                raise FileNotFoundInStorageError(f"Object not found in S3/MinIO: {key}") from exc
            raise StorageError(f"Failed to get object bytes from S3/MinIO: {exc}") from exc

    def delete_object(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as exc:
            raise StorageError(f"Failed to delete object from S3/MinIO: {exc}") from exc

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in ("404", "NoSuchKey"):
                return False
            raise StorageError(f"Failed to check object existence in S3/MinIO: {exc}") from exc


_storage_singleton: Optional[StorageBackend] = None


def create_storage_backend(settings: Optional[Settings] = None) -> StorageBackend:
    """Instantiate the configured storage backend."""
    if settings is None:
        settings = load_settings()

    if settings.storage_backend == "s3" and settings.s3_bucket:
        return S3DocumentStorage(
            bucket=settings.s3_bucket,
            endpoint_url=settings.s3_endpoint_url,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            region=settings.s3_region,
        )
    return FileSystemStorage(base_dir=settings.storage_dir)


def get_storage_service() -> StorageBackend:
    """FastAPI dependency to provide the active storage backend."""
    global _storage_singleton
    if _storage_singleton is None:
        _storage_singleton = create_storage_backend()
    return _storage_singleton
