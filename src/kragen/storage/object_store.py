"""S3-compatible object storage helpers."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

import aioboto3
from botocore.config import Config
from botocore.exceptions import ClientError

from kragen.config import get_settings
from kragen.logging_config import get_logger

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)
_S3_PATH_STYLE_CONFIG = Config(s3={"addressing_style": "path"})

# Reuse aioboto3 session (lightweight; clients are still created per operation).
_aio_session: aioboto3.Session | None = None


def _session() -> aioboto3.Session:
    global _aio_session
    if _aio_session is None:
        _aio_session = aioboto3.Session()
    return _aio_session


async def ensure_bucket_exists() -> None:
    """Create bucket if missing (idempotent)."""
    settings = get_settings()
    async with _session().client(
        "s3",
        endpoint_url=settings.storage.endpoint_url,
        aws_access_key_id=settings.storage.access_key,
        aws_secret_access_key=settings.storage.secret_key,
        config=_S3_PATH_STYLE_CONFIG,
    ) as client:
        try:
            await client.head_bucket(Bucket=settings.storage.bucket)
        except ClientError:
            await client.create_bucket(Bucket=settings.storage.bucket)
            logger.info("s3_bucket_created", bucket=settings.storage.bucket)


async def put_bytes(*, key: str, body: bytes, content_type: str | None = None) -> str:
    """Upload bytes to configured bucket; return s3:// URI."""
    settings = get_settings()
    extra: dict[str, Any] = {}
    if content_type:
        extra["ContentType"] = content_type
    async with _session().client(
        "s3",
        endpoint_url=settings.storage.endpoint_url,
        aws_access_key_id=settings.storage.access_key,
        aws_secret_access_key=settings.storage.secret_key,
        config=_S3_PATH_STYLE_CONFIG,
    ) as client:
        await client.put_object(Bucket=settings.storage.bucket, Key=key, Body=body, **extra)
    return f"s3://{settings.storage.bucket}/{key}"


async def get_bytes(*, key: str) -> bytes:
    """Download bytes from the configured bucket by object key."""
    settings = get_settings()
    async with _session().client(
        "s3",
        endpoint_url=settings.storage.endpoint_url,
        aws_access_key_id=settings.storage.access_key,
        aws_secret_access_key=settings.storage.secret_key,
        config=_S3_PATH_STYLE_CONFIG,
    ) as client:
        response = await client.get_object(Bucket=settings.storage.bucket, Key=key)
        async with response["Body"] as stream:
            return await stream.read()


def sha256_hex(data: bytes) -> str:
    """Return hex digest for content-addressed metadata."""
    return hashlib.sha256(data).hexdigest()
