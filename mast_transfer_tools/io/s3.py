"""
Dedicated S3 access utilities.  Potentially to be moved to hostess.aws.s3
in the future.
"""

import tempfile

from dataclasses import dataclass
from pathlib import Path
from io import BufferedIOBase
from os import SEEK_CUR, SEEK_END, SEEK_SET

from hostess.aws.s3 import Bucket


DEFAULT_CHUNK_SIZE = 1024 * 1024  # 1 MiB


@dataclass(slots=True)
class S3ReadRequest:
    """
    One block of data that we need to read from S3.  Internal to S3Reader.
    """
    # Byte position where the read starts
    start: int
    # Byte position where the read ends
    end: int
    # Chunk indices that will become cached if the read succeeds
    satisfies_chunk_indices: list[int]


class S3Reader(BufferedIOBase):
    """
    Read from an S3 object using the standard Python filelike API.

    Only supports binary buffered reading.  For text mode, wrap it in
    `io.TextIOWrapper`.  Not thread safe.

    The file is read in chunks from the network and cached locally in
    a temporary file.  Chunk size defaults to 1 MiB, controllable with
    the 'chunk_size' argument to the constructor.

    If 'cache_dir' is specified, the local cache files will be stored
    on the filesystem backing that directory; otherwise they will be
    stored in the default location for temporary files.  Regardless,
    the cache files will normally *not* be visible in the filesystem
    (except on Windows) and are not preserved when the S3Reader is
    closed.

    See `tempfile.gettempdir` for how the default location for
    temporary files is determined.
    """

    _bucket: Bucket
    _name: str
    _chunk_size: int
    _seek_pos: int

    _head_dict: dict[str, str]
    _object_size: int

    _cache_file: BufferedIOBase
    _chunk_is_cached: bytearray

    def __init__(
        self,
        bucket: Bucket,
        name: str | Path,
        *,
        chunk_size: int = 1024*1024,
        cache_dir: str | Path | None = None,
    ) -> None:
        super().__init__()

        self._bucket = bucket
        self._name = str(name)
        self._chunk_size = chunk_size

        # this will throw an exception if the S3 object doesn't exist
        self._head_dict = bucket.head(self._name)
        assert isinstance(self._head_dict, dict)
        self._object_size = int(self._head_dict["ContentLength"])

        # give the cache file a meaningful name in case we're on windows
        self._cache_file = tempfile.TemporaryFile(  # NOQA: SIM115
            buffering = -1,
            prefix='s3read-',
            suffix='.tmp',
            dir=cache_dir,
        )

        # byte N of _chunk_is_cached is 1 if that chunk of the file has been
        # retrieved from the server (it could be *bit* N for an additional
        # 8x space saving but it doesn't really seem worth the extra complexity)
        chunks, leftover = divmod(self._object_size, self._chunk_size)
        if leftover > 0:
            chunks += 1
        self._chunk_is_cached = bytearray(chunks)

    # public properties
    @property
    def name(self) -> str:
        return self._name

    @property
    def bucket(self) -> str:
        return self._bucket

    @property
    def chunk_size(self) -> int:
        return self._chunk_size

    @property
    def head(self) -> dict[str, str]:
        return self._head_dict

    # IOBase methods
    def close(self) -> None:
        if self.closed:
            return
        self._cache_file.close()
        self._chunk_is_cached = None
        super().close()  # sets self.closed

    def isatty(self) -> bool:
        return False

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    def seek(self, offset: int, whence: int = SEEK_SET, /) -> int:
        if self.closed:
            raise ValueError("I/O operation on closed file")
        if whence == SEEK_END:
            position = self._object_size + offset
        elif whence == SEEK_CUR:
            position = self.tell() + offset
        elif whence == SEEK_SET:
            position = offset
        else:
            raise ValueError(f"invalid whence: {whence}")
        return self._cache_file.seek(position, SEEK_SET)

    def tell(self) -> int:
        return self._cache_file.tell()

    # RawIOBase methods
    def read(self, size: int = -1, /) -> bytes:
        if size < 0:
            size = self._object_size
        self._ensure_next_n_retrieved(size)
        return self._cache_file.read(size)

    def read1(self, size: int = -1, /) -> bytes:
        if size < 0:
            size = self._object_size
        self._ensure_next_n_retrieved(size)
        return self._cache_file.read1(size)

    # Unnecessary to implement:
    # fileno, truncate, write, raw, detach are unsupported
    # default flush, readinto, readinto1, readline, readlines,
    # __del__, __enter__, __exit__ all do the Right Thing

    # Internal
    def _ensure_next_n_retrieved(self, to_read: int) -> None:
        if self.closed:
            raise ValueError("I/O operation on closed file")

        seek_pos = self.tell()
        obj_size = self._object_size
        available = obj_size - seek_pos
        to_read = min(to_read, available)

        if to_read <= 0:
            return

        assert not self._cache_file.closed
        assert self._chunk_is_cached is not None

        chunk_size = self._chunk_size
        # We're reading `to_read` bytes starting at position `seek_pos`.
        # How much of that is not already available locally?
        start_chunk_ix = seek_pos // chunk_size
        end_chunk_ix, leftover = divmod(seek_pos + to_read, chunk_size)
        if leftover:
            end_chunk_ix += 1

        requests = []
        for chunk_ix in range(start_chunk_ix, end_chunk_ix):
            if self._chunk_is_cached[chunk_ix]:
                continue

            chunk_start = chunk_ix * chunk_size
            chunk_end = min((chunk_ix + 1) * chunk_size, obj_size)

            # merge consecutive chunks so we can issue just one read request
            # for each contiguous run of bytes
            if requests and requests[-1].end == chunk_start:
                requests[-1].end = chunk_end
                requests[-1].satisfies_chunk_indices.append(chunk_ix)
            else:
                requests.append(S3ReadRequest(
                    start=chunk_start,
                    end=chunk_end,
                    satisfies_chunk_indices=[chunk_ix]
                ))

        for req in requests:
            block = self._bucket.get(
                key = self._name,
                start_byte = req.start,
                end_byte = req.end - 1,
            )
            # since that was not a multi-get, it should have thrown an
            # exception if it failed
            self._cache_file.seek(req.start)
            self._cache_file.write(block.getvalue())
            for ci in req.satisfies_chunk_indices:
                self._chunk_is_cached[ci] = 1

        self._cache_file.flush()
        self._cache_file.seek(seek_pos)
