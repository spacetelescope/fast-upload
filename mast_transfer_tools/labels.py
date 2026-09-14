"""
A MAST 'label' describes a data set that is, or will be, archived
at MAST as a single logical entity.  It is roughly comparable to a PDS
(Planetary Data System) label, but at a higher level of abstraction and
with different emphasis.

Labels are stored on disk in YAML format.
"""

import re
import logging

from pathlib import Path
from datetime import date
from functools import partial
from io import TextIOBase
from typing import (
    Any,
    Callable,
    ClassVar,
    Iterable,
    Self,
    TYPE_CHECKING,
)

from mast_transfer_tools.utilz.shims import path_walk
from mast_transfer_tools.label_meta import (
    ExplicitNullT,
    FieldNode,
    LabelElement,
    YAMLAny,
    special_field,
    decode_as_str,
    decode_as_regex,
    decode_as_list,
    decode_as_dict,
    decode_as_element,
)

from yaml import ScalarNode, SequenceNode

if TYPE_CHECKING:
    from hostess.aws.s3 import Bucket


LOG = logging.getLogger(__name__)

# We mostly follow the numpy convention for vector type strings.
SUPPORTED_DTYPE_NAMES = (
    "f2",  # half-float
    "f4",  # 32-bit float
    "f8",  # 64-bit float
    "u1",  # 8-bit unsigned integer
    "u2",  # 16-bit unsigned integer
    "u4",  # 32-bit unsigned integer
    "u8",  # 64-bit unsigned integer
    "i1",  # 8-bit signed integer
    "i2",  # 16-bit signed integer
    "i4",  # 32-bit signed integer
    "i8",  # 64-bit signed integer
    "c8",  # 64-bit complex float or integer; implementation varies by format
    "c16",  # 128-bit complex float or integer; implementation varies by format
    "O",  # catchall for variable-length or pointer to variable-length data
    "b1",  # boolean / logical ("b" by itself is equivalent to "i1")
    # presently we only support 64-bit timestamps (M) and timedeltas (m)
    # timestamps and timedeltas require a precision tag: Y(ears),
    # M(onths), W(eeks), D(ays), h(ours), m(inutes), s(econds), or
    # s(econds) with a standard SI fractional scale prefix: ms
    # (milliseconds), us (microseconds), ns, ps, fs, as.  Any of
    # these can have a numeric prefix; for example, if the actual
    # clock granularity of some data recorder was 250 microseconds,
    # that could be put in an 'M8[250us]' numpy array.
    # numpy accepts μs as another name for us, but we don't.
    r"M8\[(?:[1-9][0-9]*)?(?:[YMWDhms]|[munpfa]s)\]",
    r"m8\[(?:[1-9][0-9]*)?(?:[YMWDhms]|[munpfa]s)\]",
    # catchall for fixed-width fields that are not interpretable as
    # another listed type. "n" must be specified, e.g.  "V5" for a
    # fixed-width 5-byte field.
    r"V[1-9][0-9]*",
)
"""Data types we support."""

SUPPORTED_DTYPE_RE = re.compile(
    r"\A(?:" + "|".join(SUPPORTED_DTYPE_NAMES) + r")\Z"
)
"""Regex for data types we support."""


# FITS standard does not support i1, M, m, or f2
FITS_ARRAY_DTYPES = ("f8", "f4", "i8", "i4", "i2", "u8", "u4", "u2", "u1")
FITS_TABLE_DTYPES = FITS_ARRAY_DTYPES + (
    r"V[1-9][0-9]*",
    "c8",
    "c16",
    "O",
    "b1",
)

FITS_ARRAY_DTYPE_RE = re.compile(
    r"\A(?:" + "|".join(FITS_ARRAY_DTYPES) + r")\Z"
)
FITS_TABLE_DTYPE_RE = re.compile(
    r"\A(?:" + "|".join(FITS_TABLE_DTYPES) + r")\Z"
)

FITS_HDU_TYPES = ("primary", "image", "compimage", "bintable")
FHT_FOR_ERROR = ", ".join(FITS_HDU_TYPES)


# Parquet does not support complex numbers, and it supports only a
# small subset of the timestamp and timedelta precisions understood
# by numpy.  It _understands_ timestamps with a precision of seconds,
# but converts them to milliseconds on write, so we should never see
# 'M8[s]' coming from a Parquet file.  Similarly, numeric prefixes
# (250us) get rescaled and erased on write.
PARQUET_DTYPES = tuple(
    dt for dt in SUPPORTED_DTYPE_NAMES if dt[0] not in ("c", "M", "m")
) + (
    r"M8\[(?:D|[mun]s)\]",
    r"m8\[(?:s|[mun]s)\]",
)
PARQUET_DTYPE_RE = re.compile(r"\A(?:" + "|".join(PARQUET_DTYPES) + r")\Z")


# ASDF embeds python type ids
ASDF_TABLE_TYPES = {
    "numpy.ndarray",
    "astropy.table.table.table",
    "pyarrow.lib.table",
    "pandas.dataframe",
}
# pass a list, not a generator, so the scan finishes before the update begins
ASDF_TABLE_TYPES.update([s.split(".")[-1] for s in ASDF_TABLE_TYPES])

ASDF_TABLE_TYPES_FOR_ERROR = ", ".join(
    sorted(ASDF_TABLE_TYPES, key=lambda t: ("." in t, t))
)

STANDARDS_SUPPORTING_DATA_VALIDATION = ("asdf", "fits", "parquet")
SSDV_FOR_ERROR = ", ".join(STANDARDS_SUPPORTING_DATA_VALIDATION)


class TimeInfo(LabelElement):
    """"""
    # Might be better to allow this to be None?
    delivery_start_date: date = special_field(
        required=True, default_factory=lambda: date(1900, 1, 1)
    )
    observation_start_date: date | None = None
    observation_end_date: date | None = None


class Contacts(LabelElement):
    provider: list[str]
    archive: list[str]

    _EMAIL_LHS_RE: ClassVar[re.Pattern[str]] = re.compile(
        r"(?ius)^[\d\w.!#$%&'*+/=?^`{|}~-]+@"
    )

    @classmethod
    def _syntax_check_email(cls, addr: str) -> str | None:
        """
        Subroutine of validate_label: do a basic syntax check on the
        alleged email address ADDR.  Returns None if the address
        passes the syntax check, or an error message if it fails.
        """

        # The only way to be _sure_ an email address is valid is to send
        # a test message, but we can at least weed out complete
        # nonsense.  This algorithm is closely related to the basic
        # syntax check described by HTML5 for <input type="email">; the
        # biggest difference is that we permit arbitrary Unicode letters
        # and digits on the left-hand side of the @.

        if "@" not in addr:
            return f"{addr!r} has no @domain component"

        m = cls._EMAIL_LHS_RE.match(addr)
        if m is None:
            return f"{addr!r} has forbidden characters in the user@ part"

        domain = addr[m.end() :]
        if "." not in domain:
            return f"{addr!r} doesn't end with a fully qualified domain name"
        if "@" in domain:
            return f"{addr!r} contains too many @-signs"

        # NOTE: more thorough checks of the domain component are difficult
        # because, unfortunately, whether a domain name is legitimate -
        # even just syntactically - is a contentious question;
        # see https://github.com/kjd/idna/issues/18 for the tip
        # of the iceberg.  Perhaps the most reasonable check would
        # be to look the name up in the DNS and see if it has any
        # MX records, but: that would involve talking to the network and is
        # not a realistic solution in context. Also, Python's stdlib doesn't
        # offer any way to look up MX records specifically, and Python's
        # stdlib is stuck on IDNA 2003.
        return None

    @classmethod
    def _validate_label(
        cls, spec: dict[str, Any], lpath: str
    ) -> list[tuple[str, str]]:
        errors = []
        lp = "" if lpath == "/" else lpath
        # All elements of 'provider' and 'archive' must be email
        # addresses.
        for grp in ("provider", "archive"):
            addrs = spec[grp]
            assert isinstance(addrs, list)
            for i, addr in enumerate(addrs):
                assert isinstance(addr, str)
                err = cls._syntax_check_email(addr)
                if err is not None:
                    errors.append((f"{lp}/{grp}/{i}", err))
        return errors


def decode_columnobject_name(
    val: FieldNode,
    lpath: str,
    spec: dict[str, Any],
) -> str | re.Pattern[str]:
    """
    Decode ColumnObject.name as string(s) or regex(es) depending on the
    name_regex and repeated flags.
    """
    if spec["name_regex"] or spec["repeated"]:
        return decode_as_regex(val, lpath)
    else:
        return decode_as_str(val, lpath)


class ColumnObject(LabelElement):
    """"""
    repeated: bool = False
    name_regex: bool = False
    ndim: int = 0
    name: str | re.Pattern[str] = special_field(
        required=True,
        default="<name missing>",
        decode_with_spec=decode_columnobject_name,
    )
    dtype: str = special_field(required=True, default="i4")

    _parent_file_format: ClassVar[str | None] = None
    _supported_dtype_re: ClassVar[re.Pattern[str]] = SUPPORTED_DTYPE_RE

    @classmethod
    def _validate_label(
        cls, spec: dict[str, Any], lpath: str
    ) -> list[tuple[str, str]]:
        errors = []
        lp = "" if lpath == "/" else lpath

        spec["name_regex"] |= spec["repeated"]

        dtype = spec["dtype"]
        assert isinstance(dtype, str)
        if not cls._supported_dtype_re.fullmatch(dtype):
            err = f"data type {dtype!r} is not supported"
            if cls._parent_file_format is not None:
                err += f" in {cls._parent_file_format} tables"
            errors.append((f"{lp}/dtype", err))

        return errors


class FITSColumnObject(ColumnObject):
    """"""
    _supported_dtype_re = FITS_TABLE_DTYPE_RE
    _parent_file_format = "FITS"


class ParquetColumnObject(ColumnObject):
    """"""
    _supported_dtype_re = PARQUET_DTYPE_RE
    _parent_file_format = "Parquet"


class ObjectMetadata(LabelElement):
    """"""
    value: int | float | bool | ExplicitNullT | str | None = None
    value_regex: bool = False
    objtype: str | None = None
    index: int | None = None

    @classmethod
    def _validate_label(
        cls, spec: dict[str, Any], lpath: str
    ) -> list[tuple[str, str]]:
        errors = []
        lp = "" if lpath == "/" else lpath

        if spec["index"] is not None:
            errors.append((f"{lp}/index", "is only permitted for FITS files"))
        return errors


class ASDFObjectMetadata(ObjectMetadata):
    """"""
    @classmethod
    def _validate_label(
        cls, spec: dict[str, Any], lpath: str
    ) -> list[tuple[str, str]]:
        errors = super()._validate_label(spec, lpath)
        if spec["value"] is not None or spec["objtype"] is not None:
            errors.append(
                (
                    lpath,
                    "per-object metadata constraints are not supported for ASDF",
                )
            )
        return errors


class FITSObjectMetadata(ObjectMetadata):
    """"""
    @classmethod
    def _validate_label(
        cls, spec: dict[str, Any], lpath: str
    ) -> list[tuple[str, str]]:
        # override superclass's rejection of the 'index' field
        return []


def decode_dataobject_name(
    val: FieldNode,
    lpath: str,
    spec: dict[str, Any],
) -> Any:
    """
    Decode DataObject.name as string(s) or regex(es) depending on the
    name_regex and repeated flags.
    """
    decoder: Callable[[Any, str], str] | Callable[[Any, str], re.Pattern[str]]

    if spec["name_regex"] or spec["repeated"]:
        decoder = decode_as_regex
    else:
        decoder = decode_as_str

    if isinstance(val, SequenceNode):
        return decode_as_list(val, lpath, decode_element=decoder)
    else:
        return decoder(val, lpath)


class DataObject(LabelElement):
    """
    Represents a single data object -- an array, table, etc. -- in a filetype.
    """
    name: str | list[str] | re.Pattern[str] | list[re.Pattern[str]] | None = (
        special_field(
            default=None,
            decode_with_spec=decode_dataobject_name,
        )
    )
    objtype: str | None = None
    schema: list[ColumnObject]
    name_regex: bool = False
    dtype: str | None = None
    ndim: int | None = None
    repeated: bool = False
    optional: bool = False
    value: (
        int
        | float
        | bool
        | ExplicitNullT
        | str
        | list[str | int | float | bool | ExplicitNullT]
        | dict[
            str | int | bool | ExplicitNullT,
            str | int | float | bool | ExplicitNullT,
        ]
        | None
    ) = None
    value_regex: bool = False
    metadata: dict[str, ObjectMetadata]

    @classmethod
    def _validate_label(
        cls, spec: dict[str, Any], lpath: str
    ) -> list[tuple[str, str]]:
        errors = []
        lp = "" if lpath == "/" else lpath

        spec["name_regex"] |= spec["repeated"]

        dtype = spec["dtype"]
        ndim = spec["ndim"]
        schema = spec["schema"]
        assert isinstance(schema, list)
        if schema != []:
            if dtype is not None or ndim is not None:
                errors.append(
                    (
                        f"{lp}/schema",
                        "may not be defined along with ndim"
                        " and/or dtype (this would mean that the object both"
                        " has and does not have a multi-field data type)",
                    )
                )
        if dtype is not None and not SUPPORTED_DTYPE_RE.fullmatch(dtype):
            errors.append(
                (f"{lp}/dtype", f"{dtype!r} is not a supported data type")
            )

        return errors

    @property
    def nice_name(self) -> str:
        if isinstance(self.name, list) and len(self.name) == 1:
            name = self.name[0]
        else:
            name = self.name
        if isinstance(name, str):
            return name
        if isinstance(name, re.Pattern):
            return name.pattern
        return str(
            [n.pattern if isinstance(n, re.Pattern) else n for n in self.name]
        )


class ASDFDataObject(DataObject):
    """"""
    # Can't just re-declare as dict[str, ASDFObjectMetadata] because
    # that fails type-checking because "dict is invariant".  I used to
    # understand what that meant :-/
    metadata: dict[str, ObjectMetadata] = special_field(
        decode_value=partial(decode_as_element, element=ASDFObjectMetadata)
    )

    @classmethod
    def _validate_label(
        cls, spec: dict[str, Any], lpath: str
    ) -> list[tuple[str, str]]:
        errors = super()._validate_label(spec, lpath)
        lp = "" if lpath == "/" else lpath

        if spec["name"] is None:
            errors.append((f"{lp}/name", "ASDF data objects must have names"))

        if spec["value"] is not None and (
            spec["ndim"] is not None
            or spec["dtype"] is not None
            or spec["schema"] != []
        ):
            errors.append(
                (
                    f"{lp}/value",
                    "may not be defined along with ndim, dtype, or schema"
                    " (this would mean that the object both is and is not"
                    " table- or array-like)",
                )
            )

        if (objtype := spec["objtype"]) is not None:
            objtype = objtype.lower()
            spec["objtype"] = objtype
            if not all(str.isidentifier(lbl) for lbl in objtype.split(".")):
                errors.append(
                    (
                        f"{lp}/objtype",
                        f"for ASDF objects, must be a valid Python type name;"
                        f" {objtype!r} is not valid",
                    )
                )

            if objtype not in ASDF_TABLE_TYPES and spec["schema"] != []:
                errors.append(
                    (
                        f"{lp}/schema",
                        f"can only be defined for ASDF objects of types"
                        f" {ASDF_TABLE_TYPES_FOR_ERROR}; not for {objtype!r}",
                    )
                )

            if "array" not in objtype:
                if spec["ndim"] is not None:
                    errors.append(
                        (
                            f"{lp}/ndim",
                            f"can only be defined for array-like objects;"
                            f" not for {objtype!r}",
                        )
                    )
                if spec["dtype"] is not None:
                    errors.append(
                        (
                            f"{lp}/dtype",
                            f"can only be defined for array-like objects;"
                            f" not for {objtype!r}",
                        )
                    )

        else:  # objtype is None
            errors.append((f"{lp}/objtype", "must be defined for ASDF files"))

        return errors


class FITSDataObject(DataObject):
    """"""
    # see notes in ASDFDataObject
    metadata: dict[str, ObjectMetadata] = special_field(
        decode_value=partial(
            decode_as_dict,
            decode_kv_key=decode_as_str,
            decode_kv_val=partial(
                decode_as_element, element=FITSObjectMetadata
            ),
        )
    )
    schema: list[ColumnObject] = special_field(
        decode_value=partial(
            decode_as_list,
            decode_element=partial(
                decode_as_element, element=FITSColumnObject
            ),
        )
    )

    @classmethod
    def _validate_label(
        cls, spec: dict[str, Any], lpath: str
    ) -> list[tuple[str, str]]:
        errors = super()._validate_label(spec, lpath)
        lp = "" if lpath == "/" else lpath

        if isinstance(spec["name"], list):
            errors.append(
                (f"{lp}/name", "for FITS files, must be a string, not a list")
            )
        if spec["value"] is not None:
            errors.append(
                (f"{lp}/value", "may only be defined for ASDF files")
            )
        if spec["objtype"] is None:
            errors.append((f"{lp}/objtype", "must be defined for FITS files"))
        else:
            objtype = spec["objtype"].lower()
            spec["objtype"] = objtype
            if objtype not in FITS_HDU_TYPES:
                errors.append(
                    (
                        f"{lp}/objtype",
                        f"{objtype!r} is not a recognized HDU type for FITS files."
                        f" Must be one of: {FHT_FOR_ERROR}",
                    )
                )
            if objtype != "bintable" and spec["schema"] != []:
                errors.append(
                    (f"{lp}/schema", "can only be specified for bintable HDUs")
                )
            if objtype not in ("primary", "image", "compimage"):
                if spec["ndim"] is not None:
                    errors.append(
                        (
                            f"{lp}/ndim",
                            "can only be specified for primary, image,"
                            " and compimage HDUs.",
                        )
                    )
                if spec["dtype"] is not None:
                    errors.append(
                        (
                            f"{lp}/dtype",
                            "can only be specified for primary, image,"
                            " and compimage HDUs.",
                        )
                    )
        if (dtype := spec["dtype"]) is not None:
            if SUPPORTED_DTYPE_RE.fullmatch(
                dtype
            ) and not FITS_ARRAY_DTYPE_RE.fullmatch(dtype):
                errors.append(
                    (
                        f"{lp}/dtype",
                        f"{dtype!r} is not a supported FITS array element type",
                    )
                )

        return errors


class ParquetDataObject(DataObject):
    """"""
    # see notes in ASDFDataObject
    schema: list[ColumnObject] = special_field(
        decode_value=partial(
            decode_as_list,
            decode_element=partial(
                decode_as_element, element=ParquetColumnObject
            ),
        )
    )

    @classmethod
    def _validate_label(
        cls, spec: dict[str, Any], lpath: str
    ) -> list[tuple[str, str]]:
        errors = super()._validate_label(spec, lpath)
        lp = "" if lpath == "/" else lpath

        if spec["value"] is not None:
            errors.append(
                (f"{lp}/value", "may only be defined for ASDF files")
            )
        if (objtype := spec["objtype"]) is not None:
            objtype = objtype.lower()
            spec["objtype"] = objtype
            if objtype != "table":
                errors.append(
                    (
                        f"{lp}/objtype",
                        "must be 'table' in Parquet files (or just don't define it)",
                    )
                )
        else:
            spec["objtype"] = "table"
        if isinstance(spec["name"], list):
            errors.append(
                (
                    f"{lp}/name",
                    "for Parquet files, must be a string, not a list",
                )
            )
        return errors


class FiletypeValidationOptions(LabelElement):
    """"""
    skip: list[str]
    object_check_hook: str | None = None


class GlobalValidationOptions(LabelElement):
    """"""
    skip: list[str]
    missing_filetypes_ok: bool = False
    no_assigned_filetype_ok: bool = False


class DeliveryMeta(LabelElement):
    """"""
    schema_version: str = special_field(
        required=True, default="<schema version missing>"
    )
    global_validation_options: GlobalValidationOptions


class FilePattern(LabelElement):
    """"""
    pattern: re.Pattern[str] = special_field(required=True, default=r"\A(?!)")
    include: bool = True


def decode_as_filepattern(val: FieldNode, lpath: str) -> FilePattern:
    """"""
    if not isinstance(val, ScalarNode):
        return decode_as_element(val, lpath, element=FilePattern)

    include = True

    def process_exclude_marker(pat: str) -> str:
        nonlocal include
        if pat.startswith("(?!)"):
            include = False
            pat = pat.removeprefix("(?!)")
        return pat

    pattern = decode_as_regex(
        val, lpath, adjust_pattern=process_exclude_marker
    )
    return FilePattern(
        lpath=lpath, _errors=[], pattern=pattern, include=include
    )


def decode_as_filepatterns(val: FieldNode, lpath: str) -> list[FilePattern]:
    """"""
    pats = decode_as_list(val, lpath, decode_element=decode_as_filepattern)
    # sort all the exclusions after all the inclusions
    # OR on regex substring search is associative and commutative, so it's
    # fine to put each group in a canonical order
    # (future optimization: merge each group into one big regex)
    pats.sort(key=lambda fp: (not fp.include, fp.pattern.pattern))
    return pats


def decode_objects_with_std(
    val: FieldNode, lpath: str, spec: dict[str, Any]
) -> list[DataObject]:
    """
    Decode Filetype.objects as the appropriate DataObject subclass
    based on Filetype.standard.
    """
    obj_t: type[DataObject]
    match spec["standard"].lower():
        case "asdf":
            obj_t = ASDFDataObject
        case "fits":
            obj_t = FITSDataObject
        case "parquet":
            obj_t = ParquetDataObject
        case _:
            obj_t = DataObject

    return decode_as_list(
        val,
        lpath,
        decode_element=partial(decode_as_element, element=obj_t),
    )


class Filetype(LabelElement):
    """"""
    filename: list[FilePattern] = special_field(
        decode_value=decode_as_filepatterns
    )
    ignore: bool = False
    # only required if there are objects (see below)
    standard: str = "unspecified"
    objects: list[DataObject] = special_field(
        decode_with_spec=decode_objects_with_std,
    )
    validation_options: FiletypeValidationOptions

    @classmethod
    def _validate_label(
        cls, spec: dict[str, Any], lpath: str
    ) -> list[tuple[str, str]]:
        errors = []
        lp = "" if lpath == "/" else lpath

        std = spec["standard"].lower()
        spec["standard"] = std

        if len(spec["filename"]) == 0:
            errors.append(
                (f"{lp}/filename", "should have at least one pattern")
            )
        # this check is sufficient because decode_as_filepatterns
        # sorts all exclude patterns after all include patterns
        elif not spec["filename"][0].include:
            errors.append(
                (
                    f"{lp}/filename",
                    "should have at least one inclusive pattern",
                )
            )

        if spec["ignore"]:
            if len(spec["objects"]) > 0:
                errors.append(
                    (
                        f"{lp}/objects",
                        "should not be defined when ignore is true",
                    )
                )
            if std != "unspecified":
                errors.append(
                    (f"{lp}/standard", "should not be set when ignore is true")
                )
            if len(spec["validation_options"].skip) > 0:
                errors.append(
                    (
                        f"{lp}/validation_options",
                        "should not be set when ignore is true",
                    )
                )

        if len(spec["objects"]) > 0:
            if std == "unspecified":
                errors.append(
                    (
                        f"{lp}/standard",
                        "must be defined if any objects are defined",
                    )
                )
            elif std not in STANDARDS_SUPPORTING_DATA_VALIDATION:
                errors.append(
                    (
                        f"{lp}/standard",
                        f"{std} does not support data validation,"
                        f" but objects are defined. Only {SSDV_FOR_ERROR}"
                        f" support data-level validation.",
                    )
                )
            elif std == "parquet" and len(spec["objects"]) > 1:
                errors.append(
                    (
                        f"{lp}/objects",
                        "Parquet files may define only one object.",
                    )
                )
            elif std == "fits" and spec["objects"][0].objtype != "primary":
                errors.append(
                    (
                        f"{lp}/objects/0/objtype",
                        "first HDU in a FITS file must be a 'primary' HDU",
                    )
                )

        return errors

    def covers_file(self, path: str | Path) -> bool:
        """True if this Filetype applies to the file at 'path'."""

        # This relies on all the exclude patterns being after all the
        # include patterns in `self.filename`.
        #
        # Only two cases can change the decision:
        # match with include = False, pat.include = True -> should include
        # match with include = True, pat.include = False -> should exclude
        include = False
        for pat in self.filename:
            if include != pat.include and pat.pattern.search(str(path)):
                include = pat.include
        return include


class Label(LabelElement):
    """"""
    dataset: str = special_field(required=True, default="<name missing>")
    delivery_id: int | str = special_field(
        required=True, default="<delivery_id missing>"
    )
    time: TimeInfo = special_field(required=True)
    contacts: Contacts
    shared_name_patterns: dict[str, str]
    filetypes: dict[str, Filetype]
    delivery_meta: DeliveryMeta = special_field(required=True)
    custom_metadata: YAMLAny | None = None

    @classmethod
    def from_file(cls, fname: Path, *, missing_ok: bool = False) -> Self:
        """Parse a label from the contents of the file FNAME.
        If FNAME does not exist and missing_ok is True, returns a
        vacuous label.
        """
        try:
            with open(fname, "rt", encoding="utf-8") as fp:
                return cls.from_text(fp.read())

        except FileNotFoundError:
            if missing_ok:
                return cls.from_blank()
            raise

    def serialize_to_file(self, fp: TextIOBase) -> None:
        """Serialize self to the file handle FP."""
        fp.write(self.as_text())

    def filetypes_for_filename(self, fn: Path | str) -> dict[str, Filetype]:
        """Return a list of all Filetypes defined in this label that cover a
        file of name FN."""
        return {
            name: ft
            for name, ft in self.filetypes.items()
            if ft.covers_file(fn)
        }

    def covers_file(self, path: Path | str) -> bool:
        return any(ft.covers_file(path) for ft in self.filetypes.values())

    def covered_files_local(
        self, dir: Path
    ) -> Iterable[tuple[Path, list[Filetype]]]:
        """Yield Path objects for all of the files below DIR, relative to DIR.
        Each is paired with a list of all the Filetype objects that can apply to it.
        DIR must be a directory on disk.
        """
        for entry in path_walk(dir):
            if entry.is_file(follow_symlinks=False):
                epath = dir / entry.path
                ftypes = []
                for ft in self.filetypes.values():
                    if ft.covers_file(epath):
                        ftypes.append(ft)
                # special case: the default location for the label itself
                # is {dir}/CONTENTS.YML. we don't want to make people describe
                # the label in the label, so if a file with exactly that path
                # exists and is not covered by the label, don't yield it.
                if ftypes or epath != dir / "CONTENTS.YML":
                    yield (entry.path, ftypes)

            elif entry.is_dir(follow_symlinks=False):
                continue

            elif entry.is_symlink():
                LOG.warn(
                    f"skipping symbolic link {entry.path}"
                    " (MAST does not accept these)"
                )

            else:
                from mast_transfer_tools.utilz.stat import a_filetype

                path = str(entry.path)
                what = a_filetype(entry.stat(follow_symlinks=False).st_mode)

                LOG.warn(
                    f"skipping {path}, which is {what}"
                    " (it does not make sense to archive these)"
                )

    def covered_files_s3(
        self, bucket: "Bucket", prefix: str
    ) -> Iterable[tuple[str, list[Filetype]]]:
        for key in bucket.ls(prefix, recursive=True, formatting="simple"):
            # we can assume every entry in the listing is a regular file
            # and they all begin with the prefix
            ftypes = []
            for ft in self.filetypes.values():
                if ft.covers_file(key):
                    ftypes.append(ft)
            # special case: the default location for the label itself
            # is {prefix}/CONTENTS.YML. we don't want to make people describe
            # the label in the label, so if a file with exactly that path
            # exists and is not covered by the label, don't yield it.
            if ftypes or key != f"{prefix}/CONTENTS.YML":
                yield (key, ftypes)
