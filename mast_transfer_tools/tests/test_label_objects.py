"""
Tests of the Label data structure in isolation
(does not include any tests of files described by labels).
"""

# Caution for readers: mast_transfer_tools.labels and hypothesis.strategies
# both define a class named DataObject.  They are completely different
# and we need to use both of them in these tests.  To reduce the risk
# of confusion, they are always referred to by qualified names:
# labels.DataObject and st.DataObject respectively.

from datetime import date
from inspect import getmembers
from itertools import chain
from pathlib import Path
from re import compile as re_compile
from string import ascii_letters, ascii_lowercase, digits
from typing import Any, Literal, Sequence

import pytest
from hypothesis import given, strategies as st

from mast_transfer_tools import labels
from mast_transfer_tools.label_meta import (
    ExplicitNull,
    LabelElement,
    to_yaml_repr,
)

# Some tests are parametrized over every subclass of LabelElement we
# have, except for LabelElement itself, which is (informally) an
# abstract base class.
ALL_LABEL_TYPES = [
    kv[1]
    for kv in getmembers(
        labels,
        lambda obj: (
            isinstance(obj, type)
            and issubclass(obj, LabelElement)
            and obj is not LabelElement
        ),
    )
]

# Some tests need to know the set of all keys that are permitted for
# _some_ LabelElement subclass.
ALL_PERMITTED_KEYS = frozenset(
    chain.from_iterable(cls.permitted_keys for cls in ALL_LABEL_TYPES)
)

# Some tests need to know what file types are supported.
# This is not a strictly defined set, because any type we don't
# support detailed validation of, we can still upload as a blob.
# The important thing for this list is to cover all the file types
# we _can_ validate in detail, plus a couple we can't.)
#
# This is a list, not a set, because st.sampled_from requires an
# ordered collection.
ALL_TESTED_FILETYPES = [
    "asdf",
    "fits",
    "parquet",
    "pdf",
    "txt",
]

REJECT_IDENTIFIERS = re_compile(r"(?is)\A(?:true|false|null|undefined|none)\Z")


@st.composite
def st_identifier(
    draw: st.DrawFn,
    *,
    uppercase: bool = True,
    lowercase: bool = True,
    digits: bool = True,
    underscore: bool = True,
) -> str:
    """
    Generate a random string that is a valid Python identifier.
    Only ASCII characters are used.  Also handy for "random filename
    with no extension".  Will never produce "true", "false", "none",
    "null", or "undefined", case insensitively.

    Passing False for 'uppercase', 'lowercase', 'digits', and/or 'underscore'
    will exclude that type of character from the generated identifiers.
    """
    first_cats: list[Literal["Ll", "Lu", "N"]] = []
    rest_cats: list[Literal["Ll", "Lu", "N"]] = []
    include_chars = []
    if uppercase:
        first_cats.append("Lu")
        rest_cats.append("Lu")
    if lowercase:
        first_cats.append("Ll")
        rest_cats.append("Ll")
    if digits:
        # digits are only allowed as second and subsequent char
        rest_cats.append("N")
    if underscore:
        include_chars.append("_")

    if not first_cats and not include_chars:
        raise ValueError(
            "at least one of 'uppercase', 'lowercase', 'underscore'"
            " must be True"
        )

    while True:
        # This is equivalent to
        #    st.from_regex("[A-Za-z_][A-Za-z0-9_]*", fullmatch=True)
        # but slightly faster.
        first = draw(
            st.characters(
                codec="ascii",
                categories=first_cats,
                include_characters=include_chars,
            )
        )
        rest = draw(
            st.text(
                st.characters(
                    codec="ascii",
                    categories=rest_cats,
                    include_characters=include_chars,
                ),
                min_size=0,
                max_size=11,
            )
        )
        ident = first + rest
        if not REJECT_IDENTIFIERS.fullmatch(ident):
            return ident


@pytest.mark.parametrize("ltype", ALL_LABEL_TYPES)
def test_empty(ltype: type[LabelElement]) -> None:
    """
    Test that each concrete LabelObject subclass is constructible from
    an empty input dictionary.  (The result might have errors logged.)
    """

    EXPECTED_ERRORS = {
        "TimeInfo": {
            "/delivery_start_date": ["must always be defined"],
        },
        "ColumnObject": {
            "/name": ["must always be defined"],
            "/dtype": ["must always be defined"],
        },
        "FITSColumnObject": {
            "/name": ["must always be defined"],
            "/dtype": ["must always be defined"],
        },
        "ParquetColumnObject": {
            "/name": ["must always be defined"],
            "/dtype": ["must always be defined"],
        },
        "ASDFDataObject": {
            "/objtype": ["must be defined for ASDF files"],
            "/name": ["ASDF data objects must have names"],
        },
        "FITSDataObject": {
            "/objtype": ["must be defined for FITS files"],
        },
        "FilePattern": {
            "/pattern": ["must always be defined"],
        },
        "Filetype": {
            "/filename": ["should have at least one pattern"],
        },
        "DeliveryMeta": {
            "/schema_version": ["must always be defined"],
        },
        "Label": {
            "/dataset": ["must always be defined"],
            "/delivery_id": ["must always be defined"],
            "/delivery_meta": ["must always be defined"],
            "/delivery_meta/schema_version": ["must always be defined"],
            "/time": ["must always be defined"],
            "/time/delivery_start_date": ["must always be defined"],
        },
    }

    obj = ltype.from_blank()
    assert obj.errors == EXPECTED_ERRORS.get(ltype.__name__, {})


def st_improper_input_dict(
    ltype: type[LabelElement],
) -> st.SearchStrategy[dict[str, str]]:
    """
    Parametric strategy: Given a LabelObject subclass, produce an input
    dictionary that doesn't contain any of that label's permitted
    keys, but does contain one or more random keys that aren't permitted.
    """

    # Because this is one of the most probable and troublesome
    # mistakes, test keys that are permitted for some _other_ kind of
    # label object.
    permitted = sorted(ltype.permitted_keys)
    forbidden = sorted(ALL_PERMITTED_KEYS - ltype.permitted_keys)
    assert len(forbidden) > 0

    # Also test keys that are a short edit distance from a permitted key.
    @st.composite
    def typoed_key(draw: st.DrawFn, permitted: Sequence[str]) -> str:
        """Strategy: Select one of the keys in PERMITTED and then
        modify it with one typo.  (Just one typo because otherwise
        this is painfully slow.)

        The typos this can generate are: dropping a character,
        replacing a character with some other valid Python identifier
        character (approximated as "pick an ASCII letter that isn't
        the original"), or inserting an additional character either
        before or after the original.
        """
        base_key = draw(st.sampled_from(permitted))
        typo_position = draw(
            st.integers(min_value=0, max_value=len(base_key) - 1)
        )
        was = base_key[typo_position]
        not_was = ascii_letters.replace(was, "")

        return (
            base_key[:typo_position]
            + draw(
                st.one_of(
                    st.just(""),
                    st.sampled_from(not_was),
                    st.sampled_from(ascii_letters).map(lambda c: c + was),
                    st.sampled_from(ascii_letters).map(lambda c: was + c),
                )
            )
            + base_key[(typo_position + 1) :]
        )

    return st.dictionaries(
        st.one_of(
            st.sampled_from(forbidden),
            typoed_key(permitted),
        ),
        # The value assigned to each dictionary entry should be
        # an unparsed YAML scalar, i.e. a string.
        st_identifier(),
        min_size=1,
        max_size=5,
    )


# The only way to feed a value from pytest.mark.parametrize into
# a strategy constructor is to use st.data() to delay evaluation
# of the strategy constructor into the test function.
@pytest.mark.parametrize("ltype", ALL_LABEL_TYPES)
@given(data=st.data())
def test_improper_keys(
    ltype: type[LabelElement],
    data: st.DataObject,
) -> None:
    """
    Test that, when a concrete LabelObject is constructed from an
    input dictionary that contains none of the permitted keys, we get
    back an object with an errors list mentioning all of the keys that
    were provided.
    """
    input_dict = data.draw(st_improper_input_dict(ltype))
    obj = ltype.from_yaml(to_yaml_repr(input_dict), "/")
    # there might be other errors as well
    assert len(obj._errors) >= len(input_dict)
    for k in input_dict.keys():
        for e in obj._errors:
            if e[0] == f"/{k}":
                break
        else:
            raise AssertionError(f"no error mentioning {k}")


def st_good_TimeInfo() -> st.SearchStrategy[dict[str, date]]:
    """
    Strategy producing a valid TimeInfo input dictionary.
    """

    def ensure_obs_end_ge_start(inp: dict[str, date]) -> dict[str, date]:
        """
        Helper: If inp has both "observation_start_date" and
        "observation_end_date" keys, and the end date is earlier
        than the start date, swap their values.
        """
        start = inp.get("observation_start_date")
        end = inp.get("observation_end_date")
        if start is not None and end is not None and end < start:
            inp["observation_end_date"] = start
            inp["observation_start_date"] = end
        return inp

    return st.fixed_dictionaries(
        {
            "delivery_start_date": st.dates(),
        },
        optional={
            "observation_start_date": st.dates(),
            "observation_end_date": st.dates(),
        },
    ).map(ensure_obs_end_ge_start)


def check_TimeInfo(inp: dict[str, date], obj: labels.TimeInfo) -> None:
    """
    Test subroutine: Check that a TimeInfo object agrees with the
    input dictionary it was constructed from.
    """
    assert obj._errors == []
    assert obj.delivery_start_date == inp.get("delivery_start_date")
    assert obj.observation_start_date == inp.get("observation_start_date")
    assert obj.observation_end_date == inp.get("observation_end_date")


@given(inp=st_good_TimeInfo())
def test_good_TimeInfo(inp: dict[str, date]) -> None:
    """
    Generate random valid TimeInfo input dictionaries and verify
    that the constructed TimeInfo object matches what was passed in.
    """
    obj = labels.TimeInfo.from_yaml(to_yaml_repr(inp))
    check_TimeInfo(inp, obj)


local_part = st.text(
    alphabet=ascii_lowercase + digits,
    min_size=1,
    max_size=20,
)

simple_email = local_part.map(lambda s: f"{s}@example.com")


def st_good_Contacts() -> st.SearchStrategy[dict[str, list[str]]]:
    return st.fixed_dictionaries(
        {},
        optional={
            "archive": st.lists(simple_email, max_size=3),
            "provider": st.lists(simple_email, max_size=3),
        },
    )


def check_Contacts(inp: dict[str, Any], obj: labels.Contacts) -> None:
    """
    Test subroutine: Check that a Contacts object agrees
    with the input dictionary it was constructed from.
    """
    assert obj._errors == []
    assert obj.archive == inp.get("archive", [])
    assert obj.provider == inp.get("provider", [])


@given(inp=st_good_Contacts())
def test_good_Contacts(inp: dict[str, list[str]]) -> None:
    """
    Generate random valid Contacts input dictionaries and verify
    that the constructed Contacts object matches what was passed in.
    """
    obj = labels.Contacts.from_yaml(to_yaml_repr(inp))
    check_Contacts(inp, obj)


def st_good_ColumnObject(
    ty: type[labels.ColumnObject],
) -> st.SearchStrategy[dict[str, Any]]:
    """
    Parametric strategy producing a valid input dictionary for a
    specific concrete subclass of ColumnObject.
    """

    def maybe_compile_name_regex(inp: dict[str, Any]) -> dict[str, Any]:
        if inp.get("name_regex", False) or inp.get("repeated", False):
            inp["name"] = re_compile(inp["name"])
        return inp

    bst: st.SearchStrategy[dict[str, Any]] = st.fixed_dictionaries(
        {
            # We don't try to generate anything different for
            # name_regex=True, because generating interesting regexes
            # is a nightmare.
            "name": st_identifier(),
            "dtype": st.from_regex(ty._supported_dtype_re, fullmatch=True),
        },
        optional={
            "name_regex": st.booleans(),
            "repeated": st.booleans(),
        },
    )
    return bst.map(maybe_compile_name_regex)


def check_ColumnObject(
    inp: dict[str, Any],
    obj: labels.ColumnObject,
    *,
    allow_errors: bool = False,
) -> None:
    """
    Test subroutine: Check that a ColumnObject agrees with the input
    dictionary it was constructed from.
    """
    if not allow_errors:
        assert obj._errors == []
    assert obj.name == inp["name"]
    assert obj.dtype == inp["dtype"]
    assert obj.repeated == inp.get("repeated", False)
    # name_regex is forced to True if repeated is True
    assert obj.name_regex == (
        inp.get("name_regex", False) or inp.get("repeated", False)
    )


@pytest.mark.parametrize(
    "ty",
    [labels.ColumnObject, labels.FITSColumnObject, labels.ParquetColumnObject],
)
@given(st.data())  # see kvetching at test_improper_keys
def test_good_ColumnObject(
    ty: type[labels.ColumnObject], data: st.DataObject
) -> None:
    """
    For each concrete ColumnObject subclass, generate random valid
    input dictionaries and verify that constructed objects match
    what was passed in.
    """
    inp = data.draw(st_good_ColumnObject(ty))
    obj = ty.from_yaml(to_yaml_repr(inp))
    check_ColumnObject(inp, obj)


@pytest.mark.parametrize(
    "ty",
    [labels.ColumnObject, labels.FITSColumnObject, labels.ParquetColumnObject],
)
@given(st.data())  # see kvetching at test_improper_keys
def test_bad_dtype_ColumnObject(
    ty: type[labels.ColumnObject], data: st.DataObject
) -> None:
    """
    For each concrete ColumnObject subclass, generate input dictionaries
    with invalid dtype strings and check that constructed objects log
    an error.
    """
    # this regex has to be customized for each subclass
    if ty is labels.ColumnObject:
        regex = r"""(?xs)\A(?:
            # empty string
            | [Mm]8                   # timestamp or duration with no precision
            | (?: [^fuicObMV]         # incorrect type character
                | f[^248]             # incorrect float size
                | f[248].             # correct float size, trailing junk
                | [iu][^1248]         # incorrect int size
                | [iu][1248].         # correct int size, trailing junk
                | c[^18]              # incorrect complex size (1)
                | c1[^6]              # incorrect complex size (2)
                | c(?:8|16).          # correct complex size, trailing junk
                | [Mm][^8]            # incorrect timestamp or duration
                | [Mm]8[^\[]          # timestamp or duration + trailing junk
                | b[^1]               # we only support 'b1', not 'b<anything else>'
                | O.                  # correct misc code, trailing junk
                | V0                  # incorrect fixed-width size
                | V[^1-9]             # malformed fixed-width size (1)
                | V[1-9][0-9]*[^0-9]  # malformed fixed-width size (2)
              ) .*  # additional trailing junk allowed for these cases
        )\Z"""
    elif ty is labels.FITSColumnObject:
        # FITS does not support i1, m, M, or f2; the only difference is that
        # those cases have been removed
        regex = r"""(?xs)\A(?:
            # empty string
            | (?: [^fuicObV]          # incorrect type character
                | f[^48]              # incorrect float size
                | f[48].              # correct float size, trailing junk
                | i[^248]             # incorrect sint size
                | i[248].             # correct sint size, trailing junk
                | u[^1248]            # incorrect uint size
                | u[1248].            # correct uint size, trailing junk
                | c[^18]              # incorrect complex size (1)
                | c1[^6]              # incorrect complex size (2)
                | c(?:8|16).          # correct complex size, trailing junk
                | b[^1]               # we only support 'b1', not 'b<anything else>'
                | O.                  # correct misc code, trailing junk
                | V0                  # incorrect fixed-width size
                | V[^1-9]             # malformed fixed-width size (1)
                | V[1-9][0-9]*[^0-9]  # malformed fixed-width size (2)
              ) .*  # additional trailing junk allowed for these cases
        )\Z"""
    elif ty is labels.ParquetColumnObject:
        # Parquet does not support c or O; the only difference is that
        # those cases have been removed.
        regex = r"""(?xs)\A(?:
            # empty string
            | [Mm]8                   # timestamp or duration with no precision
            | (?: [^fuibMV]           # incorrect type character
                | f[^248]             # incorrect float size
                | f[248].             # correct float size, trailing junk
                | [iu][^1248]         # incorrect int size
                | [iu][1248].         # correct int size, trailing junk
                | [Mm][^8]            # incorrect timestamp or duration size
                | [Mm]8[^\[]          # timestamp or duration + trailing junk
                | b[^1]               # we only support 'b1', not 'b<anything else>'
                | V0                  # incorrect fixed-width size
                | V[^1-9]             # malformed fixed-width size (1)
                | V[1-9][0-9]*[^0-9]  # malformed fixed-width size (2)
              ) .*  # additional trailing junk allowed for these cases
        )\Z"""
    else:
        raise NotImplementedError(f"need bad dtype regex for {ty.__name__}")

    bad_dtype = data.draw(st.from_regex(regex, fullmatch=True))
    inp = {
        "name": "bad_dtype",
        "dtype": bad_dtype,
    }
    obj = ty.from_yaml(to_yaml_repr(inp))
    assert len(obj._errors) == 1
    assert obj._errors[0][0] == "/dtype"
    assert obj._errors[0][1].startswith(
        f"data type {bad_dtype!r} is not supported"
    )
    check_ColumnObject(inp, obj, allow_errors=True)


def st_good_ObjectMetadata(
    ty: type[labels.ObjectMetadata],
) -> st.SearchStrategy[dict[str, Any]]:
    """
    Parametric strategy producing a valid input dictionary for a
    specific concrete subclass of ObjectMetadata.  Note that, from
    one point of view, there *are* no valid input dictionaries for
    ASDFObjectMetadata; this wrinkle is ignored here and dealt with in
    check_ObjectMetadata.
    """
    optional = {
        "value_regex": st.booleans(),
        "objtype": st_identifier(),
    }
    if ty is labels.FITSObjectMetadata:
        optional["index"] = st.integers(min_value=0)
    return st.fixed_dictionaries(
        {
            "value": st.one_of(
                # st.text() - produces text that can be confused for numbers
                st_identifier(),
                st.integers(),
                st.floats(allow_nan=False),
                st.booleans(),
                st.just(ExplicitNull),
            ),
        },
        optional=optional,
    )


def check_ObjectMetadata(
    inp: dict[str, Any],
    obj: labels.ObjectMetadata,
    *,
    extra_errors: Sequence[tuple[str, str]] = [],
) -> None:
    """
    Test subroutine: Check that an ObjectMetadata object agrees
    with the input dictionary it was constructed from.
    """

    ty = type(obj)
    got_errors = set(obj._errors)
    expected_errors: set[tuple[str, str]] = set()
    expected_errors.update(extra_errors)
    if ty is labels.ASDFObjectMetadata and obj.value is not None:
        expected_errors.add(
            ("/", "per-object metadata constraints are not supported for ASDF")
        )
    assert got_errors == expected_errors

    assert obj.value == inp.get("value")
    assert obj.value_regex == inp.get("value_regex", False)
    assert obj.objtype == inp.get("objtype")
    assert obj.index == inp.get("index")


@pytest.mark.parametrize(
    "ty",
    [
        labels.ObjectMetadata,
        labels.FITSObjectMetadata,
        labels.ASDFObjectMetadata,
    ],
)
@given(st.data())  # see kvetching at test_improper_keys
def test_good_ObjectMetadata(
    ty: type[labels.ObjectMetadata], data: st.DataObject
) -> None:
    """
    For each concrete ObjectMetadata subclass, generate random valid
    input dictionaries and verify that constructed objects match
    what was passed in.
    """
    inp = data.draw(st_good_ObjectMetadata(ty))
    obj = ty.from_yaml(to_yaml_repr(inp))
    check_ObjectMetadata(inp, obj)


@pytest.mark.parametrize(
    "ty",
    [
        labels.ObjectMetadata,
        labels.ASDFObjectMetadata,
    ],
)
def test_bad_ObjectMetadata_index(ty: type[labels.ObjectMetadata]) -> None:
    """
    Test for rejection of an "index" property in ObjectMetadata other than
    FITSObjectMetadata.
    """
    inp = {"value": 1, "index": 2}
    obj = ty.from_yaml(to_yaml_repr(inp))
    check_ObjectMetadata(
        inp, obj, extra_errors=[("/index", "is only permitted for FITS files")]
    )


def test_ObjectMetadata_null_value() -> None:
    """
    Test of the distinction between ObjectMetadata.value being
    *absent* and its being explicitly specified as "null".
    """
    inpNull = {"objtype": "int", "value": ExplicitNull}
    omNull = labels.ObjectMetadata.from_yaml(to_yaml_repr(inpNull))
    check_ObjectMetadata(inpNull, omNull)

    inpAbsent = {"objtype": "int"}
    omAbsent = labels.ObjectMetadata.from_yaml(to_yaml_repr(inpAbsent))
    check_ObjectMetadata(inpAbsent, omAbsent)

    assert omNull.value is ExplicitNull
    assert omAbsent.value is None
    assert ExplicitNull is not None
    # intentional != comparison to None to validate ExplicitNull.__eq__
    assert ExplicitNull != None  # NOQA: E711


@st.composite
def st_good_DataObject(
    draw: st.DrawFn, ty: type[labels.DataObject], ix: int | None = None
) -> dict[str, Any]:
    """
    Parametric strategy producing a valid input dictionary for a
    specific concrete subclass of DataObject.  If 'ix' is given,
    this DataObject will be the object at that index within a
    Filetype.
    """

    required: dict[str, Any] = {}
    optional: dict[str, Any] = {
        "name_regex": st.booleans(),
        "repeated": st.booleans(),
        "optional": st.booleans(),
    }

    # choose whether to generate a table (with variable column types),
    # or a uniform array, or (ASDF only) a singular value
    if ty is labels.ASDFDataObject:
        fmts = ["array", "table", "value"]
    elif ty is labels.FITSDataObject and ix == 0:
        # primary HDU cannot be a table
        fmts = ["array"]
    else:
        fmts = ["array", "table"]

    fmt = draw(st.sampled_from(fmts))
    if fmt == "array":
        if ty is labels.FITSDataObject:
            dtype_re = labels.FITS_ARRAY_DTYPE_RE
        elif ty is labels.ParquetDataObject:
            dtype_re = labels.PARQUET_DTYPE_RE
        else:
            dtype_re = labels.SUPPORTED_DTYPE_RE
        required["dtype"] = st.from_regex(dtype_re, fullmatch=True)
        required["ndim"] = st.integers(min_value=1, max_value=5)

    elif fmt == "table":
        col_obj: type[labels.ColumnObject]
        if ty is labels.FITSDataObject:
            col_obj = labels.FITSColumnObject
        elif ty is labels.ParquetDataObject:
            col_obj = labels.ParquetColumnObject
        else:
            col_obj = labels.ColumnObject

        required["schema"] = st.lists(
            st_good_ColumnObject(col_obj), min_size=1, max_size=5
        )

    else:
        pass  # fully handled below

    if ty is labels.ASDFDataObject:
        # We don't try to generate anything different for name_regex=True,
        # because generating interesting regexes is a nightmare.
        required["name"] = st.one_of(
            st_identifier(), st.lists(st_identifier(), max_size=9)
        )

        if fmt == "array":
            required["objtype"] = st.sampled_from(["ndarray", "numpy.NDArray"])
        elif fmt == "table":
            required["objtype"] = st.sampled_from(
                [
                    "table",
                    "astropy.table.table.Table",
                    "pandas.DataFrame",
                ]
            )
        else:
            # keep this list in sync with the 'required["value"] =' table below
            objtype = draw(
                st.sampled_from(
                    [
                        "str",
                        "int",
                        "float",
                        "bool",
                        "null",
                        "list",
                        "dict",
                    ]
                )
            )
            required["objtype"] = st.just(objtype)

            value_regex = draw(st.booleans())
            if value_regex:
                required["value_regex"] = st.just(value=True)
                required["value"] = st_identifier()
            else:
                required["value_regex"] = st.just(value=False)
                # keep this table in sync with the "objtype =" list above
                required["value"] = (
                    {
                        "str": st.text(),
                        "int": st.integers(),
                        "float": st.floats(allow_nan=False),
                        "bool": st.booleans(),
                        "null": st.just(ExplicitNull),
                        "list": st.lists(
                            st.one_of(
                                # st.text() produces strings that can be confused
                                # for booleans, integers, and None
                                st_identifier(),
                                st.integers(),
                                st.booleans(),
                                st.floats(allow_nan=False),
                                st.just(ExplicitNull),
                            ),
                            max_size=4,
                        ),
                        "dict": st.dictionaries(
                            st.one_of(
                                # st.text() produces strings that can be confused
                                # for booleans, integers, and None
                                st_identifier(),
                                st.integers(),
                                st.booleans(),
                                st.just(ExplicitNull),
                            ),
                            st.one_of(
                                # st.text() produces strings that can be confused
                                # for booleans, integers, and None
                                st_identifier(),
                                st.integers(),
                                st.booleans(),
                                st.floats(allow_nan=False),
                                st.just(ExplicitNull),
                            ),
                        ),
                    }
                )[objtype]

        # metadata is forbidden for ASDF

    elif ty is labels.FITSDataObject:
        # If we're generating a complete Filetype (ix is not None),
        # apply the FITS rules about which objtypes can be at which
        # positions in a file.  If we're generating a FITSDataObject
        # in isolation, anything goes, but the objtype must still be
        # consistent with the schema.
        if fmt == "table":
            assert ix != 0
            otypes = st.just("bintable")
        elif ix is None:
            otypes = st.sampled_from(["primary", "image", "compimage"])
        elif ix == 0:
            otypes = st.just("primary")
        else:
            otypes = st.sampled_from(["image", "compimage"])
        required["objtype"] = otypes
        optional["name"] = st_identifier()
        optional["metadata"] = st.dictionaries(
            st_identifier(),
            st_good_ObjectMetadata(labels.FITSObjectMetadata),
            max_size=5,
        )

    elif ty is labels.ParquetDataObject:
        optional["objtype"] = st.just("table")
        optional["name"] = st_identifier()
        optional["metadata"] = st.dictionaries(
            st_identifier(),
            st_good_ObjectMetadata(labels.ObjectMetadata),
            max_size=5,
        )

    elif ty is labels.DataObject:
        optional["objtype"] = st_identifier()
        optional["name"] = st_identifier()
        optional["metadata"] = st.dictionaries(
            st_identifier(),
            st_good_ObjectMetadata(labels.ObjectMetadata),
            max_size=5,
        )

    else:
        raise NotImplementedError

    def maybe_compile_name_regex(inp: dict[str, Any]) -> dict[str, Any]:
        if "name" in inp:
            if inp.get("name_regex", False) or inp.get("repeated", False):
                name = inp["name"]
                if isinstance(name, str):
                    inp["name"] = re_compile(name)
                elif isinstance(name, list):
                    inp["name"] = [re_compile(seg) for seg in name]
        return inp

    return draw(
        st.fixed_dictionaries(required, optional=optional).map(
            maybe_compile_name_regex
        )
    )


def check_DataObject(inp: dict[str, Any], obj: labels.DataObject) -> None:
    """
    Test subroutine: Check that a DataObject agrees with the
    input dictionary it was constructed from.
    """
    assert obj._errors == []

    assert obj.name == inp.get("name")
    assert obj.dtype == inp.get("dtype")
    assert obj.ndim == inp.get("ndim")
    assert obj.repeated == inp.get("repeated", False)
    # name_regex is forced to True if repeated is True
    assert obj.name_regex == (
        inp.get("name_regex", False) or inp.get("repeated", False)
    )

    if inp.get("schema") is not None:
        assert isinstance(inp["schema"], list)
        assert isinstance(obj.schema, list)
        for sinp, sobj in zip(inp["schema"], obj.schema):
            check_ColumnObject(sinp, sobj)

    if not inp.get("metadata"):
        assert obj.metadata == {}
    else:
        assert set(inp["metadata"].keys()) == set(obj.metadata.keys())
        for mtag, minp in inp["metadata"].items():
            check_ObjectMetadata(minp, obj.metadata[mtag])


@pytest.mark.parametrize(
    "ty",
    [
        labels.DataObject,
        labels.ASDFDataObject,
        labels.FITSDataObject,
        labels.ParquetDataObject,
    ],
)
@given(st.data())  # see kvetching at test_improper_keys
def test_good_DataObject(
    ty: type[labels.DataObject], data: st.DataObject
) -> None:
    """
    For each concrete DataObject subclass, generate random valid
    input dictionaries and verify that constructed objects match
    what was passed in.
    """
    inp = data.draw(st_good_DataObject(ty))
    obj = ty.from_yaml(to_yaml_repr(inp))
    check_DataObject(inp, obj)


def st_good_FiletypeValidationOptions() -> st.SearchStrategy[dict[str, Any]]:
    """
    Strategy producing a valid ObjectMetadata input dictionary.
    """
    return st.fixed_dictionaries(
        {}, optional={"skip": st.lists(st_identifier(), max_size=3)}
    )


def check_FiletypeValidationOptions(
    inp: dict[str, Any], obj: labels.FiletypeValidationOptions
) -> None:
    """
    Test subroutine: Check that a FiletypeValidationOptions object
    agrees with the input dictionary it was constructed from.
    """
    assert obj._errors == []
    assert obj.skip == inp.get("skip", [])


@given(inp=st_good_FiletypeValidationOptions())
def test_good_FiletypeValidationOptions(inp: dict[str, Any]) -> None:
    """
    Generate random valid FiletypeValidationOptions input
    dictionaries and verify that the constructed
    FiletypeValidationOptions object matches what was passed in.
    """
    obj = labels.FiletypeValidationOptions.from_yaml(to_yaml_repr(inp))
    check_FiletypeValidationOptions(inp, obj)


def st_good_GlobalValidationOptions() -> st.SearchStrategy[dict[str, Any]]:
    """
    Strategy producing a valid GlobalValidationOptions input dictionary.
    """
    return st.fixed_dictionaries(
        {},
        optional={
            "skip": st.lists(st_identifier()),
            "missing_filetypes_ok": st.booleans(),
            "no_assigned_filetype_ok": st.booleans(),
        },
    )


def check_GlobalValidationOptions(
    inp: dict[str, Any], obj: labels.GlobalValidationOptions
) -> None:
    """
    Test subroutine: Check that a GlobalValidationOptions object agrees
    with the input dictionary it was constructed from.
    """
    assert obj._errors == []
    assert obj.skip == inp.get("skip", [])
    assert obj.missing_filetypes_ok == inp.get("missing_filetypes_ok", False)
    assert obj.no_assigned_filetype_ok == inp.get(
        "no_assigned_filetype_ok", False
    )


@given(inp=st_good_GlobalValidationOptions())
def test_good_GlobalValidationOptions(inp: dict[str, Any]) -> None:
    """
    Generate random valid GlobalValidationOptions input dictionaries
    and verify that the constructed GlobalValidationOptions object
    matches what was passed in.
    """
    obj = labels.GlobalValidationOptions.from_yaml(to_yaml_repr(inp))
    check_GlobalValidationOptions(inp, obj)


def st_good_DeliveryMeta() -> st.SearchStrategy[dict[str, Any]]:
    """
    Strategy producing a valid ObjectMetadata input dictionary.
    """
    return st.fixed_dictionaries(
        {
            "schema_version": st.from_regex("[0-9a-z.]+", fullmatch=True),
        },
        optional={
            "global_validation_options": st_good_GlobalValidationOptions(),
        },
    )


def check_DeliveryMeta(inp: dict[str, Any], obj: labels.DeliveryMeta) -> None:
    """
    Test subroutine: Check that a DeliveryMeta object agrees
    with the input dictionary it was constructed from.
    """
    assert obj._errors == []
    assert obj.schema_version == inp.get("schema_version", None)
    check_GlobalValidationOptions(
        inp.get("global_validation_options", {}), obj.global_validation_options
    )


@given(inp=st_good_DeliveryMeta())
def test_good_DeliveryMeta(inp: dict[str, Any]) -> None:
    """
    Generate random valid DeliveryMeta input dictionaries and verify
    that the constructed DeliveryMeta object matches what was passed in.
    """
    obj = labels.DeliveryMeta.from_yaml(to_yaml_repr(inp))
    check_DeliveryMeta(inp, obj)


@st.composite
def st_good_FilePattern(
    draw: st.DrawFn, std: str, *, include: bool | None = None
) -> dict[str, Any]:
    """
    Strategy producing a valid FilePattern input dictionary.
    """
    file_stem = draw(st_identifier())
    compression = draw(st.sampled_from(["", ".gz", ".zstd", ".bz2", ".xz"]))
    pattern = re_compile(f"{file_stem}\\.{std}{compression}")

    return draw(
        st.fixed_dictionaries(
            {
                "pattern": st.just(pattern),
                "include": st.just(include)
                if include is not None
                else st.booleans(),
            }
        )
    )


def check_FilePattern(inp: dict[str, Any], obj: labels.FilePattern) -> None:
    """
    Test subroutine: Check that a FilePattern object agrees
    with the input dictionary it was constructed from.
    """
    assert obj._errors == []
    assert obj.pattern == inp.get("pattern", r"\A(?!)")
    assert obj.include == inp.get("include", False)


@pytest.mark.parametrize("ftype", ALL_TESTED_FILETYPES)
@given(st.data())  # see kvetching at test_improper_keys
def test_good_FilePattern(ftype: str, data: st.DataObject) -> None:
    """x
    For each file format standard where we support data validation,
    plus two common ones where we don't, generate random valid
    FilePattern input dictionaries and verify that the constructed
    FilePattern object matches what was passed in.
    """
    inp = data.draw(st_good_FilePattern(ftype))
    obj = labels.FilePattern.from_yaml(to_yaml_repr(inp))
    check_FilePattern(inp, obj)


@st.composite
def st_good_DataObject_list(
    draw: st.DrawFn,
    ty: type[labels.DataObject],
) -> list[dict[str, Any]]:
    if ty is labels.ParquetDataObject:
        n = 1  # Parquet Filetypes always contain exactly one DataObject
    else:
        n = draw(st.integers(min_value=1, max_value=5))
    return [draw(st_good_DataObject(ty, ix=ix)) for ix in range(n)]


@st.composite
def st_good_Filetype(draw: st.DrawFn, std: str) -> dict[str, Any]:
    required: dict[str, st.SearchStrategy] = {}
    optional: dict[str, st.SearchStrategy] = {}

    # the filenames field must always have at least one include pattern
    filenames = [draw(st_good_FilePattern(std, include=True))]
    filenames.extend(
        draw(st.lists(st_good_FilePattern(std), min_size=0, max_size=3))
    )
    # and all the exclude patterns must be sorted after all the include patterns
    filenames.sort(key=lambda fp: (not fp["include"], fp["pattern"].pattern))

    required["filename"] = st.just(filenames)

    ignore = draw(st.booleans())
    if ignore:
        required["ignore"] = st.just(ignore)
    else:
        required["standard"] = st.just(std)
        optional["validation_options"] = st_good_FiletypeValidationOptions()

        data_obj_class: type[labels.DataObject] | None = (
            {
                "asdf": labels.ASDFDataObject,
                "fits": labels.FITSDataObject,
                "parquet": labels.ParquetDataObject,
            }
        ).get(std)
        if data_obj_class is not None:
            optional["objects"] = st_good_DataObject_list(data_obj_class)

    return draw(st.fixed_dictionaries(required, optional=optional))


def check_Filetype(inp: dict[str, Any], obj: labels.Filetype) -> None:
    """
    Test subroutine: Check that a Filetype object agrees with the
    input dictionary it was constructed from.
    """
    assert obj._errors == []
    assert obj.ignore == inp.get("ignore", False)
    assert obj.standard == inp.get("standard", "unspecified")
    check_FiletypeValidationOptions(
        inp.get("validation_options", {}), obj.validation_options
    )
    for pinp, pobj in zip(inp.get("filename", []), obj.filename):
        check_FilePattern(pinp, pobj)
    for dinp, dobj in zip(inp.get("objects", []), obj.objects):
        check_DataObject(dinp, dobj)


@pytest.mark.parametrize("ftype", ALL_TESTED_FILETYPES)
@given(st.data())  # see kvetching at test_improper_keys
def test_good_Filetype(ftype: str, data: st.DataObject) -> None:
    """
    For each file format standard where we support data validation,
    plus two common ones where we don't, generate random valid
    input dictionaries and verify that constructed objects match
    what was passed in.
    """
    inp = data.draw(st_good_Filetype(ftype))
    obj = labels.Filetype.from_yaml(to_yaml_repr(inp))
    check_Filetype(inp, obj)


def st_good_Label() -> st.SearchStrategy[dict[str, Any]]:
    """
    Strategy producing a valid input dictionary for a complete Label.
    """
    return st.fixed_dictionaries(
        {
            "dataset": st_identifier(),
            "delivery_id": st.one_of(st.integers(), st_identifier()),
            "time": st_good_TimeInfo(),
            "delivery_meta": st_good_DeliveryMeta(),
        },
        optional={
            "contacts": st_good_Contacts(),
            "shared_name_patterns": st.dictionaries(
                st_identifier(),
                st_identifier(),
            ),
            "filetypes": st.dictionaries(
                st_identifier(),
                st.sampled_from(ALL_TESTED_FILETYPES).flatmap(
                    st_good_Filetype
                ),
            ),
            # a crude approximation; custom_metadata is supposed to
            # accept *anything*
            "custom_metadata": st.dictionaries(
                st_identifier(), st.one_of(st.integers(), st_identifier())
            ),
        },
    )


def check_Label(inp: dict[str, Any], obj: labels.Label) -> None:
    """
    Test subroutine: Check that a Label object agrees with the
    input dictionary it was constructed from.
    """
    assert obj._errors == []

    assert obj.dataset == inp["dataset"]
    assert obj.delivery_id == inp["delivery_id"]
    assert obj.shared_name_patterns == inp.get("shared_name_patterns", {})

    check_TimeInfo(inp["time"], obj.time)
    check_Contacts(inp.get("contacts", {}), obj.contacts)
    check_DeliveryMeta(inp.get("delivery_meta", {}), obj.delivery_meta)

    inp_filetypes = inp.get("filetypes", {})
    inp_filetype_keys = sorted(inp_filetypes.keys())
    assert sorted(obj.filetypes.keys()) == inp_filetype_keys
    for k in inp_filetype_keys:
        check_Filetype(inp_filetypes[k], obj.filetypes[k])


@given(inp=st_good_Label())
def test_good_Label(inp: dict[str, Any]) -> None:
    """
    Generate random valid Label input dictionaries and verify
    that the constructed Label object matches what was passed in.
    """
    obj = labels.Label.from_yaml(to_yaml_repr(inp))
    check_Label(inp, obj)


def test_covered_files_local(label_file: Path, data_dir: Path) -> None:
    """Test finding local files and the filetypes that cover them."""
    label = labels.Label.from_file(label_file)
    text_filetype = label.filetypes["text"]

    covered_files = dict(label.covered_files_local(data_dir))

    assert covered_files == {
        Path("a.txt"): [text_filetype],
        Path("nested/b.txt"): [text_filetype],
    }
