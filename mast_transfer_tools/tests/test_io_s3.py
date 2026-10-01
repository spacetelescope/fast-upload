from os import SEEK_CUR, SEEK_END, SEEK_SET

from mast_transfer_tools.io.s3 import S3Reader
from mast_transfer_tools.tests.mock_buckets import FakeReadOnlyDataBucket

from hypothesis import given, strategies as st


# we cut the chunk size down to 1KiB to increase the odds of tickling
# bugs in the chunk caching logic and to avoid needing to generate
# multi-megabyte test files
CHUNK_SIZE = 1024
MAX_FILE_SIZE = 128 * CHUNK_SIZE


@st.composite
def fake_file(draw: st.DrawFn) -> tuple[str, int]:
    dt = draw(
        st.sampled_from(
            [
                "u1",
                "u2",
                "u4",
                "i1",
                "i2",
                "i4",
            ]
        )
    )
    itemsize = int(dt[1])
    nelem = draw(st.integers(min_value=0, max_value=MAX_FILE_SIZE))
    length = nelem * itemsize
    name = f"{dt}-{length}"
    return name, length


def do_test_S3Reader_read(
    name: str, length: int, read_seq: list[tuple[int, int]]
) -> None:
    bucket = FakeReadOnlyDataBucket("test_S3Reader")
    data = bucket.get_test_file(name)

    with S3Reader(bucket, name, chunk_size=CHUNK_SIZE) as reader:
        # test some of the basic properties en passant
        assert reader.chunk_size == CHUNK_SIZE
        assert reader.name == name
        assert reader.bucket is bucket
        assert reader.head["ContentLength"] == str(length)

        assert not reader.closed
        assert reader.readable()
        assert reader.seekable()
        assert not reader.writable()
        assert not reader.isatty()

        for blk_pos, blk_end in read_seq:
            exp_end = min(blk_end, length)
            exp_len = exp_end - blk_pos
            req_len = blk_end - blk_pos
            pos = reader.seek(blk_pos, SEEK_SET)
            assert pos == blk_pos
            blk = reader.read(req_len)
            assert len(blk) == exp_len
            assert blk == data[blk_pos:exp_end]


@given(tc=fake_file())
def test_S3Reader_read_whole_file(tc: tuple[str, int]) -> None:
    name, length = tc
    do_test_S3Reader_read(name, length, [(0, length)])


def test_S3Reader_seek_from_logical_end() -> None:
    bucket = FakeReadOnlyDataBucket("test_S3Reader")
    name = "u1-100"
    data = bucket.get_test_file(name)

    with S3Reader(bucket, name, chunk_size=CHUNK_SIZE) as reader:
        assert reader.seek(0, SEEK_END) == len(data)
        assert reader.seek(-4, SEEK_END) == len(data) - 4
        assert reader.read(4) == data[-4:]
        assert reader.seek(-2, SEEK_CUR) == len(data) - 2
        assert reader.read(2) == data[-2:]


@given(tc=fake_file(), data=st.data())
def test_S3Reader_read_whole_file_chunks(
    tc: tuple[str, int], data: st.DataObject
) -> None:
    name, length = tc

    # read sequentially in chunks that add up to the whole file
    splits = sorted(
        data.draw(
            st.lists(
                st.integers(min_value=min(1, length), max_value=length),
                min_size=1,
                max_size=max(20, length // 64),
                unique=True,
            )
        )
    )
    seq = []
    prev = 0
    for end in splits:
        assert prev <= end
        seq.append((prev, end))
        end = prev
    if prev < length:
        seq.append((prev, length))

    do_test_S3Reader_read(name, length, seq)


@given(tc=fake_file(), data=st.data())
def test_S3Reader_read_random_access(
    tc: tuple[str, int], data: st.DataObject
) -> None:
    name, length = tc

    # random access reads, possibly overlapping, not necessarily
    # the entire file
    positions = data.draw(
        st.lists(
            st.integers(min_value=0, max_value=length),
            min_size=2,
            max_size=max(20, length // 64),
        )
    )
    lengths = data.draw(
        st.lists(
            st.integers(min_value=0, max_value=length // 8),
            min_size=len(positions),
            max_size=len(positions),
        )
    )

    do_test_S3Reader_read(
        name, length, [(p, p + l) for p, l in zip(positions, lengths)]
    )
