"""The S3 client. Swapping the object store means changing config, not this file's callers.

Two clients, one store. The internal one performs operations from inside the
network. The public one only signs, offline: its endpoint is the address the
browser reaches, and a signature is bound to the host it was made for.
The API never carries object bytes except the first few it inspects.
"""

from dataclasses import dataclass

import boto3
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import ClientError

from app.config import Settings
from app.storage.content_types import ALLOWED_CONTENT_TYPES, SNIFF_BYTES

# No signed URL lives longer than this, whatever the caller asks for.
MAX_PRESIGN_TTL_SECONDS = 900


@dataclass(frozen=True)
class PresignedPost:
    url: str
    fields: dict[str, str]


@dataclass(frozen=True)
class ObjectInfo:
    size_bytes: int
    head: bytes


def _client(endpoint: str, region: str, access_key_id: str, secret_access_key: str) -> BaseClient:
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=region,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            # Newer boto3 adds checksums that non-AWS stores do not all accept.
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            connect_timeout=3,
            read_timeout=10,
            retries={"max_attempts": 2, "mode": "standard"},
        ),
    )


def _clamp_ttl(seconds: int) -> int:
    return max(1, min(seconds, MAX_PRESIGN_TTL_SECONDS))


class ObjectStore:
    def __init__(self, settings: Settings) -> None:
        access_key_id = settings.s3_access_key_id_file.read_text().strip()
        secret_access_key = settings.s3_secret_access_key_file.read_text().strip()
        self._bucket = settings.s3_bucket
        self._max_upload_bytes = settings.max_upload_bytes
        self._operations = _client(
            settings.s3_endpoint, settings.s3_region, access_key_id, secret_access_key
        )
        self._signing = _client(
            settings.s3_public_endpoint, settings.s3_region, access_key_id, secret_access_key
        )

    def presign_upload(
        self, key: str, content_type: str, max_bytes: int, ttl_seconds: int
    ) -> PresignedPost:
        """A POST policy that pins the key, one content type and a size range.

        The policy is the control: the store rejects any upload that differs.
        `max_bytes` is the size the client declared, capped at the global limit.
        """
        if content_type not in ALLOWED_CONTENT_TYPES:
            raise ValueError("content type is not allowed")
        if not 1 <= max_bytes <= self._max_upload_bytes:
            raise ValueError("size is outside the allowed range")
        signed = self._signing.generate_presigned_post(
            Bucket=self._bucket,
            Key=key,
            Fields={"Content-Type": content_type},
            Conditions=[
                {"Content-Type": content_type},
                ["content-length-range", 1, max_bytes],
            ],
            ExpiresIn=_clamp_ttl(ttl_seconds),
        )
        return PresignedPost(url=signed["url"], fields=signed["fields"])

    def presign_download(
        self, key: str, content_type: str, content_disposition: str, ttl_seconds: int
    ) -> str:
        """A GET URL whose response type and disposition are fixed by the signature."""
        return self._signing.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self._bucket,
                "Key": key,
                "ResponseContentType": content_type,
                "ResponseContentDisposition": content_disposition,
            },
            ExpiresIn=_clamp_ttl(ttl_seconds),
        )

    def inspect(self, key: str) -> ObjectInfo | None:
        """Size and leading bytes of an object, or None if it does not exist."""
        try:
            size = self._operations.head_object(Bucket=self._bucket, Key=key)["ContentLength"]
            body = self._operations.get_object(
                Bucket=self._bucket, Key=key, Range=f"bytes=0-{SNIFF_BYTES - 1}"
            )["Body"]
            return ObjectInfo(size_bytes=size, head=body.read(SNIFF_BYTES))
        except ClientError as error:
            if error.response["Error"]["Code"] in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise

    def put(self, key: str, body: bytes, content_type: str) -> None:
        """Server-side write, for the seed and tests. Uploads from clients are presigned."""
        self._operations.put_object(
            Bucket=self._bucket, Key=key, Body=body, ContentType=content_type
        )

    def delete(self, key: str) -> None:
        self._operations.delete_object(Bucket=self._bucket, Key=key)
