import hashlib
import json
import logging
import os
import posixpath
from datetime import datetime
from pathlib import PurePath
from typing import IO, Iterable, List, NamedTuple, Optional, Union

import boto3
import jsonschema
import mypy_boto3_s3
from botocore.errorfactory import ClientError
from django.conf import settings
from django.core.files.uploadedfile import InMemoryUploadedFile, TemporaryUploadedFile
from redis import Redis, exceptions

logger = logging.getLogger(__name__)


class S3PrefixPath(NamedTuple):
    Key: str


class S3Object(NamedTuple):
    name: str
    key: str
    last_modified: datetime
    size: int
    etag: str


class S3ObjectVersion:
    def __init__(
        self, name: str, data: mypy_boto3_s3.service_resource.ObjectVersion
    ) -> None:
        self.name = name
        self._data = data

    @property
    def id(self) -> str:
        """Returns the version id"""
        # NOTE id and version_id are the same thing
        return self._data.id

    @property
    def key(self) -> str:
        return self._data.key

    @property
    def last_modified(self) -> datetime:
        return self._data.last_modified

    @property
    def size(self) -> int:
        return self._data.size

    @property
    def e_tag(self) -> str:
        return self._data.e_tag

    @property
    def is_latest(self) -> bool:
        return self._data.is_latest


class S3ObjectWithVersions(NamedTuple):
    latest: S3ObjectVersion
    versions: List[S3ObjectVersion]


def redis_is_running() -> bool:
    try:
        connection = Redis(
            "redis", password=os.environ.get("REDIS_PASSWORD"), port=6379
        )
        connection.set("foo", "bar")
    except exceptions.ConnectionError:
        return False

    return True


def get_s3_session() -> boto3.Session:
    """Get a new S3 Session instance using Django settings"""

    session = boto3.Session(
        aws_access_key_id=settings.STORAGE_ACCESS_KEY_ID,
        aws_secret_access_key=settings.STORAGE_SECRET_ACCESS_KEY,
        region_name=settings.STORAGE_REGION_NAME,
    )
    return session


def get_s3_bucket() -> mypy_boto3_s3.service_resource.Bucket:
    """Get a new S3 Bucket instance using Django settings"""

    session = get_s3_session()

    # Get the bucket objects
    s3 = session.resource("s3", endpoint_url=settings.STORAGE_ENDPOINT_URL)
    return s3.Bucket(settings.STORAGE_BUCKET_NAME)


def get_s3_client() -> mypy_boto3_s3.Client:
    """Get a new S3 client instance using Django settings"""

    s3_client = boto3.client(
        "s3",
        region_name=settings.STORAGE_REGION_NAME,
        aws_access_key_id=settings.STORAGE_ACCESS_KEY_ID,
        aws_secret_access_key=settings.STORAGE_SECRET_ACCESS_KEY,
        endpoint_url=settings.STORAGE_ENDPOINT_URL,
    )
    return s3_client


def get_sha256(file: IO) -> str:
    """Return the sha256 hash of the file"""
    if type(file) is InMemoryUploadedFile or type(file) is TemporaryUploadedFile:
        return _get_sha256_memory_file(file)
    else:
        return _get_sha256_file(file)


def _get_sha256_memory_file(
    file: Union[InMemoryUploadedFile, TemporaryUploadedFile]
) -> str:
    BLOCKSIZE = 65536
    hasher = hashlib.sha256()

    for chunk in file.chunks(BLOCKSIZE):
        hasher.update(chunk)

    file.seek(0)
    return hasher.hexdigest()


def _get_sha256_file(file: IO) -> str:
    BLOCKSIZE = 65536
    hasher = hashlib.sha256()
    buf = file.read(BLOCKSIZE)
    while len(buf) > 0:
        hasher.update(buf)
        buf = file.read(BLOCKSIZE)
    file.seek(0)
    return hasher.hexdigest()


def safe_join(base: str, *paths: str) -> str:
    """
    A version of django.utils._os.safe_join for S3 paths.
    Joins one or more path components to the base path component
    intelligently. Returns a normalized version of the final path.
    The final path must be located inside of the base path component
    (otherwise a ValueError is raised).
    Paths outside the base path indicate a possible security
    sensitive operation.
    """
    base_path = base
    base_path = base_path.rstrip("/")
    paths = tuple(paths)

    final_path = base_path + "/"
    for path in paths:
        _final_path = posixpath.normpath(posixpath.join(final_path, path))
        # posixpath.normpath() strips the trailing /. Add it back.
        if path.endswith("/") or _final_path + "/" == final_path:
            _final_path += "/"
        final_path = _final_path
    if final_path == base_path:
        final_path += "/"

    # Ensure final_path starts with base_path and that the next character after
    # the base path is /.
    base_path_len = len(base_path)
    if not final_path.startswith(base_path) or final_path[base_path_len] != "/":
        raise ValueError(
            "the joined path is located outside of the base path" " component"
        )

    return final_path.lstrip("/")


def is_qgis_project_file(filename: str) -> bool:
    """Returns whether the filename seems to be a QGIS project file by checking the file extension."""
    path = PurePath(filename)

    if path.suffix in (".qgs", ".qgz"):
        return True

    return False


def get_qgis_project_file(project_id: str) -> Optional[str]:
    """Return the relative path inside the project of the qgs/qgz file or
    None if no qgs/qgz file is present"""

    bucket = get_s3_bucket()

    prefix = "projects/{}/files/".format(project_id)

    for obj in bucket.objects.filter(Prefix=prefix):
        if is_qgis_project_file(obj.key):
            path = PurePath(obj.key)
            return str(path.relative_to(*path.parts[:3]))

    return None


def check_s3_key(key: str) -> Optional[str]:
    """Check to see if an object exists on S3. It it exists, the function
    returns the sha256 of the file from the metadata"""

    client = get_s3_client()
    try:
        head = client.head_object(Bucket=settings.STORAGE_BUCKET_NAME, Key=key)
    except ClientError as e:
        if e.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
            return None
        else:
            raise e

    metadata = head["Metadata"]
    if "sha256sum" in metadata:
        return metadata["sha256sum"]
    else:
        return metadata["Sha256sum"]


def get_deltafile_schema_validator() -> jsonschema.Draft7Validator:
    """Creates a JSON schema validator to check whether the provided delta
    file is valid.

    Returns:
        jsonschema.Draft7Validator -- JSON Schema validator
    """
    schema_file = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "deltafile_01.json"
    )

    with open(schema_file) as f:
        schema_dict = json.load(f)

    jsonschema.Draft7Validator.check_schema(schema_dict)

    return jsonschema.Draft7Validator(schema_dict)


def get_s3_project_size(project_id: str) -> int:
    """Return the size in MiB of the project on the storage, included the
    exported files"""

    bucket = get_s3_bucket()

    prefix = "projects/{}/".format(project_id)
    total_size = 0

    for obj in bucket.objects.filter(Prefix=prefix):
        total_size += obj.size

    return round(total_size / (1024 * 1024), 3)


def get_project_files_with_versions(project_id: str) -> Iterable[S3ObjectWithVersions]:
    """Returns a list of files and their versions.

    Args:
        project_id (str): the project id

    Returns:
        Iterable[S3ObjectWithVersions]: the list of files
    """
    bucket = get_s3_bucket()
    prefix = f"projects/{project_id}/files/"

    return list_files_with_versions(bucket, prefix, strip_prefix=True)


def get_project_package_files(project_id: str) -> Iterable[S3Object]:
    """Returns a list of package files.

    Args:
        project_id (str): the project id

    Returns:
        Iterable[S3ObjectWithVersions]: the list of package files
    """
    bucket = get_s3_bucket()
    prefix = f"projects/{project_id}/export/"

    return list_files(bucket, prefix, strip_prefix=True)


def get_project_files_count(project_id: str) -> int:
    """Returns the number of files within a project."""
    bucket = get_s3_bucket()
    prefix = f"projects/{project_id}/files/"
    files = list(bucket.objects.filter(Prefix=prefix))

    return len(files)


def get_project_package_files_count(project_id: str) -> int:
    """Returns the number of package files within a project."""
    bucket = get_s3_bucket()
    prefix = f"projects/{project_id}/export/"
    files = list(bucket.objects.filter(Prefix=prefix))

    return len(files)


def get_s3_object_url(
    key: str, bucket: mypy_boto3_s3.service_resource.Bucket = get_s3_bucket()
) -> str:
    """Returns the block storage URL for a given key. The key may not exist in the bucket.

    Args:
        key (str): the object key
        bucket (boto3.Bucket, optional): Bucket for that object. Defaults to `get_s3_bucket()`.

    Returns:
        str: URL
    """
    return f"{settings.STORAGE_ENDPOINT_URL_EXTERNAL}/{bucket.name}/{key}"


def list_files(
    bucket: mypy_boto3_s3.service_resource.Bucket,
    prefix: str,
    strip_prefix: bool = True,
) -> Iterable[S3Object]:
    """Iterator that lists a bucket's objects under prefix."""
    for f in bucket.objects.filter(Prefix=prefix):
        if strip_prefix:
            start_idx = len(prefix)
            name = f.key[start_idx:]
        else:
            name = f.key

        yield S3Object(
            name=name,
            key=f.key,
            last_modified=f.last_modified,
            size=f.size,
            etag=f.e_tag,
        )


def list_versions(
    bucket: mypy_boto3_s3.service_resource.Bucket,
    prefix: str,
    strip_prefix: bool = True,
) -> Iterable[S3ObjectVersion]:
    """Iterator that lists a bucket's objects under prefix."""
    for v in bucket.object_versions.filter(Prefix=prefix):
        if strip_prefix:
            start_idx = len(prefix)
            name = v.key[start_idx:]
        else:
            name = v.key

        yield S3ObjectVersion(name, v)


def list_files_with_versions(
    bucket: mypy_boto3_s3.service_resource.Bucket,
    prefix: str,
    strip_prefix: bool = True,
) -> Iterable[S3ObjectWithVersions]:
    """Yields an object with all it's versions

    Returns:
        Iterable[S3ObjectWithVersions]: an iterator with the objects with their versions

    Yields:
        Iterator[Iterable[S3ObjectWithVersions]]: the object with its versions
    """
    last_key = None
    versions: List[S3ObjectVersion] = []
    latest: Optional[S3ObjectVersion] = None

    for v in list_versions(bucket, prefix, strip_prefix):
        if last_key != v.key:
            if last_key:
                assert latest

                yield S3ObjectWithVersions(latest, versions)

            latest = None
            versions = []
            last_key = v.key

        versions.append(v)

        if v.is_latest:
            latest = v

    if last_key:
        assert latest
        yield S3ObjectWithVersions(latest, versions)
