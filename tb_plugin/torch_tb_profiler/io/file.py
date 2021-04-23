import glob as py_glob
import os
import tempfile
from abc import ABC, abstractmethod
from collections import namedtuple

from .. import utils
from .utils import as_bytes, as_text

logger = utils.get_logger()

try:
    import boto3
    import botocore.exceptions

    S3_ENABLED = True
except ImportError:
    S3_ENABLED = False

try:
    from azure.storage.blob import ContainerClient
    BLOB_ENABLED = True
except ImportError:
    BLOB_ENABLED = False

_DEFAULT_BLOCK_SIZE = 16 * 1024 * 1024

# Registry of filesystems by prefix.
#
# Currently supports "s3://" URLs for S3 based on boto3 and falls
# back to local filesystem.
_REGISTERED_FILESYSTEMS = {}


def register_filesystem(prefix, filesystem):
    if ":" in prefix:
        raise ValueError("Filesystem prefix cannot contain a :")
    _REGISTERED_FILESYSTEMS[prefix] = filesystem


def get_filesystem(filename):
    """Return the registered filesystem for the given file."""
    prefix = ""
    index = filename.find("://")
    if index >= 0:
        prefix = filename[:index]
    if prefix.upper() in ('HTTP', 'HTTPS'):
        root, _ = parse_blob_url(filename)
        if root.lower().endswith('.blob.core.windows.net'):
            fs = _REGISTERED_FILESYSTEMS.get('blob', None)
        else:
            raise ValueError("Not supported file system for prefix %s" % root)
    else:
        fs = _REGISTERED_FILESYSTEMS.get(prefix, None)
    if fs is None:
        raise ValueError("No recognized filesystem for prefix %s" % prefix)
    return fs

# Data returned from the Stat call.
StatData = namedtuple("StatData", ["length"])

def parse_blob_url(url):
    from urllib import parse
    url_path = parse.urlparse(url)

    parts = url_path.path.lstrip('/').split('/', 1)
    return url_path.netloc, tuple(parts)

class BaseFileSystem(ABC):
    @abstractmethod
    def exists(self, filename):
        raise NotImplementedError

    @abstractmethod
    def abspath(self, path):
        raise NotImplementedError

    @abstractmethod
    def basename(self, path):
        raise NotImplementedError

    @abstractmethod
    def relpath(self, path, start):
        raise NotImplementedError

    @abstractmethod
    def join(self, path, *paths):
        raise NotImplementedError

    @abstractmethod
    def read(self, file, binary_mode=False, size=None, continue_from=None):
        raise NotImplementedError

    @abstractmethod
    def write(self, filename, file_content, binary_mode=False):
        raise NotImplementedError

    def download_file(self, filename):
        return filename

    def support_append(self):
        return False

    def append(self, filename, file_content, binary_mode=False):
        pass

    @abstractmethod
    def glob(self, filename):
        raise NotImplementedError

    @abstractmethod
    def isdir(self, dirname):
        raise NotImplementedError

    @abstractmethod
    def listdir(self, dirname):
        raise NotImplementedError

    @abstractmethod
    def makedirs(self, path):
        raise NotImplementedError

    @abstractmethod
    def stat(self, filename):
        raise NotImplementedError

class LocalFileSystem(BaseFileSystem):
    def __init__(self):
        pass

    def exists(self, filename):
        return os.path.exists(filename)

    def abspath(self, path):
        return os.path.abspath(os.path.expanduser(os.path.expandvars(path)))

    def basename(self, path):
        return os.path.basename(path)

    def relpath(self, path, start):
        return os.path.relpath(path, start)

    def join(self, path, *paths):
        return os.path.join(path, *paths)

    def read(self, filename, binary_mode=False, size=None, continue_from=None):
        mode = "rb" if binary_mode else "r"
        encoding = None if binary_mode else "utf8"
        if not self.exists(filename):
            raise FileNotFoundError(filename)

        offset = None
        if continue_from is not None:
            offset = continue_from.get("opaque_offset", None)
        with open(filename, mode, encoding=encoding) as f:
            if offset is not None:
                f.seek(offset)
            data = f.read(size)
            # The new offset may not be `offset + len(data)`, due to decoding
            # and newline translation.
            # So, just measure it in whatever terms the underlying stream uses.
            continuation_token = {"opaque_offset": f.tell()}
            return (data, continuation_token)

    def write(self, filename, file_content, binary_mode=False):
        """Writes string file contents to a file, overwriting any existing contents.
        """
        self._write(filename, file_content, "wb" if binary_mode else "w")

    def support_append(self):
        return True

    def append(self, filename, file_content, binary_mode=False):
        """Append string file contents to a file.
        """
        self._write(filename, file_content, "ab" if binary_mode else "a")

    def _write(self, filename, file_content, mode):
        encoding = None if "b" in mode else "utf8"
        with open(filename, mode, encoding=encoding) as f:
            compatify = as_bytes if "b" in mode else as_text
            f.write(compatify(file_content))

    def glob(self, filename):
        """Returns a list of files that match the given pattern(s)."""
        if isinstance(filename, str):
            return [
                matching_filename
                for matching_filename in py_glob.glob(filename)
            ]
        else:
            return [
                matching_filename
                for single_filename in filename
                for matching_filename in py_glob.glob(single_filename)
            ]

    def isdir(self, dirname):
        return os.path.isdir(dirname)

    def listdir(self, dirname):
        entries = os.listdir(dirname)
        entries = [item for item in entries]
        return entries

    def makedirs(self, path):
        os.makedirs(path, exist_ok=True)

    def stat(self, filename):
        """Returns file statistics for a given path."""
        # NOTE: Size of the file is given by .st_size as returned from
        # os.stat(), but we convert to .length
        file_length = os.stat(filename).st_size
        return StatData(file_length)

class S3FileSystem(BaseFileSystem):
    """Provides filesystem access to S3."""

    def __init__(self):
        if not boto3:
            raise ImportError("boto3 must be installed for S3 support.")
        self._s3_endpoint = os.environ.get("S3_ENDPOINT", None)
        access_key = os.environ.get("AWS_ACCESS_KEY_ID")
        secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY")
        if access_key and secret_key:
            boto3.setup_default_session(aws_access_key_id=access_key, aws_secret_access_key=secret_key)

    def exists(self, filename):
        """Determines whether a path exists or not."""
        client = boto3.client("s3", endpoint_url=self._s3_endpoint)
        bucket, path = self.bucket_and_path(filename)
        r = client.list_objects(Bucket=bucket, Prefix=path, Delimiter="/")
        if r.get("Contents") or r.get("CommonPrefixes"):
            return True
        return False

    def abspath(self, path):
        return path

    def basename(self, path):
        return path.split('/')[-1]

    def relpath(self, path, start):
        if not path.startswith(start):
            return path
        start = start.rstrip('/')
        begin = len(start) + 1 # include the ending slash '/'
        return path[begin:]

    def join(self, path, *paths):
        """Join paths with a slash."""
        return "/".join((path,) + paths)

    def read(self, filename, binary_mode=False, size=None, continue_from=None):
        """Reads contents of a file to a string."""
        s3 = boto3.resource("s3", endpoint_url=self._s3_endpoint)
        bucket, path = self.bucket_and_path(filename)
        args = {}

        # For the S3 case, we use continuation tokens of the form
        # {byte_offset: number}
        offset = 0
        if continue_from is not None:
            offset = continue_from.get("byte_offset", 0)

        endpoint = ""
        if size is not None:
            endpoint = offset + size

        if offset != 0 or endpoint != "":
            args["Range"] = "bytes={}-{}".format(offset, endpoint)

        logger.info("s3: starting reading file %s" % filename)
        try:
            stream = s3.Object(bucket, path).get(**args)["Body"].read()
        except botocore.exceptions.ClientError as exc:
            if exc.response["Error"]["Code"] in ["416", "InvalidRange"]:
                if size is not None:
                    # Asked for too much, so request just to the end. Do this
                    # in a second request so we don't check length in all cases.
                    client = boto3.client("s3", endpoint_url=self._s3_endpoint)
                    obj = client.head_object(Bucket=bucket, Key=path)
                    content_length = obj["ContentLength"]
                    endpoint = min(content_length, offset + size)
                if offset == endpoint:
                    # Asked for no bytes, so just return empty
                    stream = b""
                else:
                    args["Range"] = "bytes={}-{}".format(offset, endpoint)
                    stream = s3.Object(bucket, path).get(**args)["Body"].read()
            else:
                raise

        logger.info("s3: file %s download is done, size is %d" % (filename, len(stream)))
        # `stream` should contain raw bytes here (i.e., there has been neither
        # decoding nor newline translation), so the byte offset increases by
        # the expected amount.
        continuation_token = {"byte_offset": (offset + len(stream))}
        if binary_mode:
            return (bytes(stream), continuation_token)
        else:
            return (stream.decode("utf-8"), continuation_token)

    def write(self, filename, file_content, binary_mode=False):
        """Writes string file contents to a file."""
        client = boto3.client("s3", endpoint_url=self._s3_endpoint)
        bucket, path = self.bucket_and_path(filename)
        if binary_mode:
            if not isinstance(file_content, bytes):
                raise TypeError("File content type must be bytes")
        else:
            file_content = as_bytes(file_content)
        client.put_object(Body=file_content, Bucket=bucket, Key=path)

    def download_file(self, filename):
        fp = tempfile.NamedTemporaryFile('w+t', suffix='.%s' % self.basename(filename), delete=False)
        fp.close()

        logger.info("s3: starting downloading file %s as %s" % (filename, fp.name))
        s3 = boto3.client('s3')
        bucket, path = self.bucket_and_path(filename)
        with open(fp.name, 'wb') as downloaded_file:
            s3.download_fileobj(bucket, path, downloaded_file)
            logger.info("s3: file %s download is as %s" % (filename, fp.name))
            return fp.name

    def glob(self, filename):
        """Returns a list of files that match the given pattern(s)."""
        # Only support prefix with * at the end and no ? in the string
        star_i = filename.find("*")
        quest_i = filename.find("?")
        if quest_i >= 0:
            raise NotImplementedError(
                "{} not supported by compat glob".format(filename)
            )
        if star_i != len(filename) - 1:
            return []

        filename = filename[:-1]
        client = boto3.client("s3", endpoint_url=self._s3_endpoint)
        bucket, path = self.bucket_and_path(filename)
        p = client.get_paginator("list_objects")
        keys = []
        for r in p.paginate(Bucket=bucket, Prefix=path):
            for o in r.get("Contents", []):
                key = o["Key"][len(path) :]
                if key:
                    keys.append(filename + key)
        return keys

    def isdir(self, dirname):
        """Returns whether the path is a directory or not."""
        client = boto3.client("s3", endpoint_url=self._s3_endpoint)
        bucket, path = self.bucket_and_path(dirname)
        if not path.endswith("/"):
            path += "/"
        r = client.list_objects(Bucket=bucket, Prefix=path, Delimiter="/")
        if r.get("Contents") or r.get("CommonPrefixes"):
            return True
        return False

    def listdir(self, dirname):
        """Returns a list of entries contained within a directory."""
        client = boto3.client("s3", endpoint_url=self._s3_endpoint)
        bucket, path = self.bucket_and_path(dirname)
        p = client.get_paginator("list_objects")
        if not path.endswith("/"):
            path += "/"
        keys = []
        for r in p.paginate(Bucket=bucket, Prefix=path, Delimiter="/"):
            keys.extend(
                o["Prefix"][len(path) : -1] for o in r.get("CommonPrefixes", [])
            )
            for o in r.get("Contents", []):
                key = o["Key"][len(path) :]
                if key:  # Skip the base dir, which would add an empty string
                    keys.append(key)
        return keys

    def makedirs(self, dirname):
        """Creates a directory and all parent/intermediate directories."""
        if self.exists(dirname):
            return

        client = boto3.client("s3", endpoint_url=self._s3_endpoint)
        bucket, path = self.bucket_and_path(dirname)
        if not path.endswith("/"):
            path += "/"  # This will make sure we don't override a file
        client.put_object(Body="", Bucket=bucket, Key=path)

    def stat(self, filename):
        """Returns file statistics for a given path."""
        # NOTE: Size of the file is given by ContentLength from S3,
        # but we convert to .length
        client = boto3.client("s3", endpoint_url=self._s3_endpoint)
        bucket, path = self.bucket_and_path(filename)

        obj = client.head_object(Bucket=bucket, Key=path)
        return StatData(obj["ContentLength"])

    def bucket_and_path(self, url):
        """Split an S3-prefixed URL into bucket and path."""
        if url.startswith("s3://"):
            url = url[len("s3://") :]
        idx = url.index("/")
        bucket = url[:idx]
        path = url[(idx + 1) :]
        return bucket, path

class AzureBlobSystem(BaseFileSystem):
    """Provides filesystem access to S3."""

    def __init__(self):
        if not ContainerClient:
            raise ImportError("azure-storage-blob must be installed for Azure Blob support.")
        self.connection_string = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", None)

    def exists(self, filename):
        """Determines whether a path exists or not."""
        account, container, path = self.container_and_path(filename)
        client = self.create_container_client(account, container)
        blobs = client.list_blobs(name_starts_with=path, maxresults=1)
        for blob in blobs:
            dir_path = os.path.dirname(path)
            if dir_path:
                basename = os.path.basename(path)
                rel_path = blob.name[len(dir_path):]
                parts = rel_path.lstrip('/').split('/')
                return basename == parts[0]
            else:
                parts = blob.name.split('/')
                return path == parts[0]
        return False

    def abspath(self, path):
        return path

    def basename(self, path):
        return path.split('/')[-1]

    def relpath(self, path, start):
        if not path.startswith(start):
            return path
        start = start.rstrip('/')
        begin = len(start) + 1 # include the ending slash '/'
        return path[begin:]

    def join(self, path, *paths):
        """Join paths with a slash."""
        return "/".join((path,) + paths)

    def read(self, filename, binary_mode=False, size=None, continue_from=None):
        """Reads contents of a file to a string."""
        logger.info("azure blob: starting reading file %s" % filename)
        account, container, path = self.container_and_path(filename)
        client = self.create_container_client(account, container)
        blob_client = client.get_blob_client(path)
        if not blob_client.exists():
            raise FileNotFoundError("file %s doesn't exist!" % path)

        downloader = blob_client.download_blob(offset=continue_from, length=size)
        if continue_from is not None:
            continuation_token = continue_from + downloader.size
        else:
            continuation_token = downloader.size

        data = downloader.readall()
        logger.info("azure blob: file %s download is done, size is %d" % (filename, len(data)))
        if binary_mode:
            return as_bytes(data), continuation_token
        else:
            return as_text(data), continuation_token

    def write(self, filename, file_content, binary_mode=False):
        """Writes string file contents to a file."""
        account, container, path = self.container_and_path(filename)
        client = self.create_container_client(account, container)

        if binary_mode:
            if not isinstance(file_content, bytes):
                raise TypeError("File content type must be bytes")
        else:
            file_content = as_bytes(file_content)
        client.upload_blob(path, file_content)

    def download_file(self, filename):
        fp = tempfile.NamedTemporaryFile('w+t', suffix='.%s' % self.basename(filename), delete=False)
        fp.close()

        logger.info("azure blob: starting downloading file %s as %s" % (filename, fp.name))
        account, container, path = self.container_and_path(filename)
        client = self.create_container_client(account, container)
        blob_client = client.get_blob_client(path)
        if not blob_client.exists():
            raise FileNotFoundError("file %s doesn't exist!" % path)

        downloader = blob_client.download_blob()
        with open(fp.name, 'wb') as downloaded_file:
            data = downloader.readall()
            downloaded_file.write(data)
            logger.info("azure blob: file %s download is as %s, size is %d" % (filename, fp.name, len(data)))
            return fp.name

    def glob(self, filename):
        """Returns a list of files that match the given pattern(s)."""
        # Only support prefix with * at the end and no ? in the string
        star_i = filename.find("*")
        quest_i = filename.find("?")
        if quest_i >= 0:
            raise NotImplementedError(
                "{} not supported by compat glob".format(filename)
            )
        if star_i != len(filename) - 1:
            return []

        filename = filename[:-1]

        account, container, path = self.container_and_path(filename)
        client = self.create_container_client(account, container)
        blobs = client.list_blobs(name_starts_with=path)
        return [blob.name for blob in blobs]

    def isdir(self, dirname):
        """Returns whether the path is a directory or not."""
        account, container, path = self.container_and_path(dirname)
        client = self.create_container_client(account, container)
        blobs = client.list_blobs(name_starts_with=path, maxresults=1)

        for blob in blobs:
            dir_path = os.path.dirname(path)
            if dir_path:
                basename = os.path.basename(path)
                rel_path = blob.name[len(dir_path):]
                parts = rel_path.lstrip('/').split('/')
                return basename == parts[0] and len(parts) > 1
            else:
                parts = blob.name.split('/')
                return path == parts[0] and len(parts) > 1
        return False

    def listdir(self, dirname):
        """Returns a list of entries contained within a directory."""
        account, container, path = self.container_and_path(dirname)
        client = self.create_container_client(account, container)
        blob_iter = client.list_blobs(name_starts_with=path)
        items = []
        for blob in blob_iter:
            item = os.path.relpath(blob.name, path)
            if items not in items:
                items.append(item)
        return items

    def makedirs(self, dirname):
        """No need create directory since the upload blob will automatically create"""
        pass

    def stat(self, filename):
        """Returns file statistics for a given path."""
        account, container, path = self.container_and_path(filename)
        client = self.create_container_client(account, container)
        blob_client = client.get_blob_client(path)
        props = blob_client.get_blob_properties()
        return StatData(props.size)

    def walk(self, top, topdown=True, onerror=None):
        account, container, path = self.container_and_path(top)
        client = self.create_container_client(account, container)
        blobs = client.list_blobs(name_starts_with=path)
        results = {}
        for blob in blobs:
            dirname = os.path.dirname(blob.name)
            dirname = "https://{}/{}/{}".format(account, container, dirname)
            basename = os.path.basename(blob.name)
            results.setdefault(dirname, []).append(basename)
        for key, value in results.items():
            yield key, None, value

    def container_and_path(self, url):
        """Split an Azure blob -prefixed URL into container and blob path."""
        root, parts = parse_blob_url(url)
        if len(parts) != 2:
            raise ValueError("Invalid azure blob url %s" % url)
        return root, parts[0], parts[1]

    def create_container_client(self, account, container):
        if self.connection_string:
            client = ContainerClient.from_connection_string(self.connection_string, container)
        else:
            client = ContainerClient.from_container_url("https://{}/{}".format(account, container))
        return client

register_filesystem("", LocalFileSystem())
if S3_ENABLED:
    register_filesystem("s3", S3FileSystem())

if BLOB_ENABLED:
    register_filesystem("blob", AzureBlobSystem())

class File(object):
    def __init__(self, filename, mode):
        if mode not in ("r", "rb", "br", "w", "wb", "bw"):
            raise ValueError("mode {} not supported by File".format(mode))
        self.filename = filename
        self.fs = get_filesystem(self.filename)
        self.fs_supports_append = self.fs.support_append()
        self.buff = None
        self.buff_chunk_size = _DEFAULT_BLOCK_SIZE
        self.buff_offset = 0
        self.continuation_token = None
        self.write_temp = None
        self.write_started = False
        self.binary_mode = "b" in mode
        self.write_mode = "w" in mode
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        self.buff = None
        self.buff_offset = 0
        self.continuation_token = None

    def __iter__(self):
        return self

    def _read_buffer_to_offset(self, new_buff_offset):
        old_buff_offset = self.buff_offset
        read_size = min(len(self.buff), new_buff_offset) - old_buff_offset
        self.buff_offset += read_size
        return self.buff[old_buff_offset : old_buff_offset + read_size]

    def read(self, n=None):
        """Reads contents of file to a string.

        Args:
            n: int, number of bytes or characters to read, otherwise
                read all the contents of the file

        Returns:
            Subset of the contents of the file as a string or bytes.
        """
        if self.write_mode:
            raise OSError("File not opened in read mode")

        result = None
        if self.buff and len(self.buff) > self.buff_offset:
            # read from local buffer
            if n is not None:
                chunk = self._read_buffer_to_offset(self.buff_offset + n)
                if len(chunk) == n:
                    return chunk
                result = chunk
                n -= len(chunk)
            else:
                # add all local buffer and update offsets
                result = self._read_buffer_to_offset(len(self.buff))

        # read from filesystem
        read_size = max(self.buff_chunk_size, n) if n is not None else None
        (self.buff, self.continuation_token) = self.fs.read(self.filename, self.binary_mode, read_size, self.continuation_token)
        self.buff_offset = 0

        # add from filesystem
        if n is not None:
            chunk = self._read_buffer_to_offset(n)
        else:
            # add all local buffer and update offsets
            chunk = self._read_buffer_to_offset(len(self.buff))
        result = result + chunk if result else chunk

        return result

    def write(self, file_content):
        """Writes string file contents to file, clearing contents of the file
        on first write and then appending on subsequent calls.
        """
        if not self.write_mode:
            raise OSError("File not opened in write mode")

        if self.closed:
            raise OSError("File already closed")

        if self.fs_supports_append:
            if not self.write_started:
                # write the first chunk to truncate file if it already exists
                self.fs.write(self.filename, file_content, self.binary_mode)
                self.write_started = True
            else:
                # append the later chunks
                self.fs.append(self.filename, file_content, self.binary_mode)
        else:
            # add to temp file, but wait for flush to write to final filesystem
            if self.write_temp is None:
                mode = "w+b" if self.binary_mode else "w+"
                self.write_temp = tempfile.TemporaryFile(mode)

            compatify = as_bytes if self.binary_mode else as_text
            self.write_temp.write(compatify(file_content))

    def __next__(self):
        line = None
        while True:
            if not self.buff:
                # read one unit into the buffer
                line = self.read(1)
                if line and (line[-1] == "\n" or not self.buff):
                    return line
                if not self.buff:
                    raise StopIteration()
            else:
                index = self.buff.find("\n", self.buff_offset)
                if index != -1:
                    # include line until now plus newline
                    chunk = self.read(index + 1 - self.buff_offset)
                    line = line + chunk if line else chunk
                    return line

                # read one unit past end of buffer
                chunk = self.read(len(self.buff) + 1 - self.buff_offset)
                line = line + chunk if line else chunk
                if line and (line[-1] == "\n" or not self.buff):
                    return line
                if not self.buff:
                    raise StopIteration()

    def next(self):
        return self.__next__()

    def flush(self):
        if self.closed:
            raise OSError("File already closed")

        if not self.fs_supports_append:
            if self.write_temp is not None:
                # read temp file from the beginning
                self.write_temp.flush()
                self.write_temp.seek(0)
                chunk = self.write_temp.read()
                if chunk is not None:
                    # write full contents and keep in temp file
                    self.fs.write(self.filename, chunk, self.binary_mode)
                    self.write_temp.seek(len(chunk))

    def close(self):
        self.flush()
        if self.write_temp is not None:
            self.write_temp.close()
            self.write_temp = None
            self.write_started = False
        self.closed = True


def exists(filename):
    """Determines whether a path exists or not."""
    return get_filesystem(filename).exists(filename)

def abspath(path):
    return get_filesystem(path).abspath(path)

def basename(path):
    return get_filesystem(path).basename(path)

def relpath(path, start):
    return get_filesystem(path).relpath(path, start)

def join(path, *paths):
    return get_filesystem(path).join(path, *paths)

def download_file(filename):
    return get_filesystem(filename).download_file(filename)

def glob(filename):
    """Returns a list of files that match the given pattern(s)."""
    return get_filesystem(filename).glob(filename)

def isdir(dirname):
    """Returns whether the path is a directory or not."""
    return get_filesystem(dirname).isdir(dirname)

def listdir(dirname):
    """Returns a list of entries contained within a directory.

    The list is in arbitrary order. It does not contain the special entries "."
    and "..".
    """
    return get_filesystem(dirname).listdir(dirname)

def makedirs(path):
    """Creates a directory and all parent/intermediate directories."""
    return get_filesystem(path).makedirs(path)

def walk(top, topdown=True, onerror=None):
    """Recursive directory tree generator for directories.

    Args:
      top: string, a Directory name
      topdown: bool, Traverse pre order if True, post order if False.
      onerror: optional handler for errors. Should be a function, it will be
        called with the error as argument. Rethrowing the error aborts the walk.

    Errors that happen while listing directories are ignored.

    Yields:
      Each yield is a 3-tuple:  the pathname of a directory, followed by lists
      of all its subdirectories and leaf files.
      (dirname, [subdirname, subdirname, ...], [filename, filename, ...])
      as strings
    """
    fs = get_filesystem(top)
    if hasattr(fs, "walk"):
        yield from fs.walk(top, topdown, onerror)
    else:
        top = fs.abspath(top)
        listing = fs.listdir(top)

        files = []
        subdirs = []
        for item in listing:
            full_path = fs.join(top, item)
            if fs.isdir(full_path):
                subdirs.append(item)
            else:
                files.append(item)

        here = (top, subdirs, files)

        if topdown:
            yield here

        for subdir in subdirs:
            joined_subdir = fs.join(top, subdir)
            for subitem in walk(joined_subdir, topdown, onerror=onerror):
                yield subitem

        if not topdown:
            yield here

def stat(filename):
    """Returns file statistics for a given path."""
    return get_filesystem(filename).stat(filename)
