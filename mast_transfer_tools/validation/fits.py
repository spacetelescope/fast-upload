"""
Validation of FITS data files
"""

import dataclasses
import re

from collections import defaultdict
from typing import Any

import numpy as np
from astropy.io import fits
from astropy.io.fits.hdu.base import _BaseHDU
from astropy.io.fits.card import Card

from mast_transfer_tools.labels import (
    DataObject,
    FiletypeValidationOptions,
    Filetype,
    ObjectMetadata,
)
from mast_transfer_tools.validation import generic
from mast_transfer_tools.utilz.english import repr_rx


def check_hdu_type(
    hdu: _BaseHDU, spec: DataObject, _valopts: FiletypeValidationOptions
) -> list[str]:
    """
    Does HDU have the primary / extension type given in `spec`?

    Returns:
        [] if match is ok, list of failures if not.
    """

    if spec.objtype is None:
        return []

    if isinstance(hdu, fits.CompImageHDU):
        got_type = "COMPIMAGE"
    elif isinstance(hdu, fits.PrimaryHDU):
        got_type = "PRIMARY"
    else:
        got_type = hdu.header.get("XTENSION", "").strip().upper()

    expected_type = spec.objtype.upper()
    if got_type != expected_type:
        return [f"wrong HDU type: expected {expected_type}, got {got_type}"]

    return []


def check_hdu_name(
    hdu: _BaseHDU, spec: DataObject, _valopts: FiletypeValidationOptions
) -> list[str]:
    if isinstance(spec.name, re.Pattern):
        if not spec.name.fullmatch(hdu.name):
            rx = repr_rx(spec.name)
            return [f"incorrect HDU name: {hdu.name!r} doesn't match {rx}"]

    else:
        if spec.name != hdu.name:
            return [
                f"incorrect HDU name: expected {spec.name}, got {hdu.name}"
            ]

    return []


def check_hdu_schema(
    hdu: _BaseHDU, spec: DataObject, _valopts: FiletypeValidationOptions
) -> dict[str, list[str]]:
    """
    Does this HDU match the schema specified in `spec`?

    Returns:
        {} if match is ok, dict of failures if not.
    """
    if spec.schema is None:
        return {}

    if not isinstance(hdu, fits.BinTableHDU):
        # schema validation only makes sense for bintable HDUs
        return {}

    # FITS_rec does not accurately report dtypes of the scaled columns,
    # which is a problem here specifically for unsigned integers (stored in
    # FITS as signed integers with offsets). This means that we must construct
    # a dtype from the individual columns.
    # See: https://github.com/astropy/astropy/issues/8862
    dtype = []
    for k, v in hdu.data.dtype.fields.items():
        col_dt = [k, generic.normalize_dt_rep(hdu.data[k].dtype)]
        if len(v) == 3:
            col_dt.append(v[2])
        dtype.append(tuple(col_dt))

    return generic.check_schema(np.dtype(dtype), spec)


def check_hdu_array_props(
    hdu: _BaseHDU, spec: DataObject, valopts: FiletypeValidationOptions
) -> list[str]:
    """
    Does `hdu` have the array properties specified in `spec`?

    Returns:
        [] if match is ok, list of failures if not.
    """
    if not isinstance(
        hdu, (fits.PrimaryHDU, fits.ImageHDU, fits.CompImageHDU)
    ):
        # NOTE: this should never get here, but there's no reason to treat it
        #  as a meaningful failure at this point -- it's not the checker's job
        #  to enforce label validation rules
        return []
    failures = []
    if spec.dtype is not None and "dtype" not in valopts.skip:
        if hdu.data is None:
            failures.append(
                f"invalid dtype: expected {spec.dtype}, got empty data section"
            )
        elif (dt := generic.normalize_dt_rep(hdu.data.dtype)) != spec.dtype:
            failures.append(f"invalid dtype: expected {spec.dtype}, got {dt}")
    if spec.ndim is not None and "ndim" not in valopts.skip:
        if hdu.data is None and spec.ndim != 0:
            failures.append(
                f"invalid dimensionality: expected {spec.ndim}, got data "
                f"section"
            )
        elif hdu.data is not None and hdu.data.ndim != spec.ndim:
            failures.append(
                f"invalid dimensionality: expected {spec.ndim}, got "
                f"{hdu.data.ndim}"
            )
    return failures


@dataclasses.dataclass
class HduMeta:
    """"""
    uniform: None | ObjectMetadata = None
    by_index: dict[int, ObjectMetadata] = dataclasses.field(
        default_factory=dict
    )


def check_hdu_meta(
    hdu: _BaseHDU, spec: DataObject, _valopts: FiletypeValidationOptions
) -> dict[str, list[str]]:
    """Check the metadata (i.e. header) of an individual HDU against `spec`."""
    if spec.metadata is None:
        return {}

    # TODO: This does a bunch of work (including label validation!)
    # that should be done by the label parser
    metadata: defaultdict[str, HduMeta] = defaultdict(HduMeta)
    for key, meta_spec in spec.metadata.items():
        key = Card.normalize_keyword(key)
        ix = meta_spec.index
        kspec = metadata[key]

        # a key with meta_spec.index == None conflicts with having
        # _any_ instances of the same key with meta_spec.index not None
        if ix is None:
            if kspec.uniform is not None:
                raise RuntimeError(
                    f"bad label: metadata has {key} twice with no index"
                )
            if len(kspec.by_index) > 0:
                conflicts = " ".join(str(i) for i in kspec.by_index.keys())
                raise RuntimeError(
                    f"bad label: metadata has {key} with no index"
                    f" and also with indices [{conflicts}]"
                )
            kspec.uniform = meta_spec

        else:
            if kspec.uniform is not None:
                conflicts = " ".join(str(i) for i in kspec.by_index.keys())
                raise RuntimeError(
                    f"bad label: metadata has {key} with no index"
                    f" and also with indices [{conflicts}]"
                )
            if ix in kspec.by_index:
                raise RuntimeError(
                    f"bad label: metadata has {key} twice with index {ix}"
                )
            kspec.by_index[ix] = meta_spec

    header: dict[str, Any] = {}
    try:
        for keyword, value in hdu.header.items():
            # I would like to think this is unnecessary but I don't trust
            # astropy quite enough
            keyword = Card.normalize_keyword(keyword)
            if keyword not in header:
                header[keyword] = []
            header[keyword].append(value)
    except fits.VerifyError as e:
        return {"": [f"Low-level FITS file verification error: {e}"]}

    failures = {}
    for key, meta_specs in metadata.items():
        card_values = header.get(key)

        if card_values is None:
            failures[key] = ["required metadata key missing"]
            continue

        if meta_specs.uniform is not None:
            assert len(meta_specs.by_index) == 0
            if len(card_values) > 1:
                failures[key] = [
                    f"metadata key appears {len(card_values)} times in header;"
                    f" label expects it to appear once."
                ]
                continue

            if (
                failure := generic.check_meta(
                    card_values[0],
                    meta_specs.uniform,
                )
            ) is not None:
                failures[key] = [failure]

        else:
            n_cards = len(card_values)
            n_specs = max(meta_specs.by_index.keys()) + 1
            if n_cards < n_specs:
                cardtimes = "once" if n_cards == 1 else f"{n_cards} times"
                spectimes = "once" if n_specs == 1 else f"{n_specs} times"
                failures[key] = [
                    f"metadata key appears {cardtimes} in header;"
                    f" label expects it to appear {spectimes}."
                ]
                continue

            key_failures = []
            for ix, mspec in meta_specs.by_index.items():
                value = card_values[ix]
                if (failure := generic.check_meta(value, mspec)) is not None:
                    key_failures.append(failure)
            if key_failures:
                failures[key] = key_failures

    return failures


def check_hdu(
    hdu: _BaseHDU, spec: DataObject, valopts: FiletypeValidationOptions
) -> dict[str, list[str]]:
    """
    Does `hdu` match `spec` (possibly skipping some checks as specified
    in `valopts`?

    Returns:
        dict like {check_name: failures}. Empty if all checks passed.
    """
    # this silly-looking internal organization limits the scopes of
    # the inner functions' variables so I can reuse their names
    # without running into type consistency issues
    def list_checks() -> None:
        for tag, check in [
            ("name", check_hdu_name),
            ("objtype", check_hdu_type),
            ("array_props", check_hdu_array_props),
        ]:
            if tag in valopts.skip:
                continue
            t_failures = check(hdu, spec, valopts)
            if t_failures:
                failures[tag] = t_failures

    def dict_checks() -> None:
        for tag, check in [
            ("schema", check_hdu_schema),
            ("metadata", check_hdu_meta),
        ]:
            if tag in valopts.skip:
                continue
            t_failures = check(hdu, spec, valopts)
            for key, k_failures in t_failures.items():
                failures[f"{tag}/{key}"] = k_failures

    failures: dict[str, list[str]] = {}
    list_checks()
    dict_checks()
    return failures


def check_file(hdul: fits.HDUList, spec: Filetype) -> dict[str, list[str]]:
    """
    Compare FILE to the expectations described by SPEC.
    Return a semi-structured description of the differences.
    An empty dict means no significant differences were found.
    """
    if "all" in spec.validation_options.skip:
        return {}
    failures = {}
    hdu_ix = 0
    for hdu_spec in spec.objects:
        if hdu_ix >= len(hdul):
            if not hdu_spec.optional:
                failures[f"{hdu_ix}/base"] = ["missing"]
            hdu_ix += 1
            continue

        while True:
            hdu = hdul[hdu_ix]
            hdu_failures = check_hdu(hdu, hdu_spec, spec.validation_options)
            for key, k_failures in hdu_failures.items():
                failures[f"{hdu_ix}/{key}"] = k_failures

            hdu_ix += 1
            if (
                hdu_failures.get("name") is not None
                or not hdu_spec.repeated
                or hdu_ix >= len(hdul)
            ):
                break
            assert isinstance(hdu_spec.name, re.Pattern)
            if not hdu_spec.name.fullmatch(hdul[hdu_ix].name):
                break

    while hdu_ix < len(hdul):
        failures[f"{hdu_ix}/base"] = ["extra"]
        hdu_ix += 1

    return failures
