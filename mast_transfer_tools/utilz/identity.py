import logging
import re

LOG = logging.getLogger(__name__)

ALLOWED_COLLECTIONS = frozenset(
    (
        "ccsp",
        "hlsp",
        "hst",
        "jwst",
        "k2",
        "mccm",
        "opo",
        "ps1",
        "roman",
        "sdss",
        "tess",
        "toy",
    )
)


def identity_component(value: str) -> str:
    original = value
    if isinstance(value, str):
        value = value.strip()
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9](?:[A-Za-z0-9_-]*[A-Za-z0-9])?", value
    ):
        raise ValueError(
            "must be a nonempty ASCII identifier with alphanumeric ends "
            "and only letters, digits, hyphens, or underscores"
        )
    normalized = value.lower().replace("_", "-")
    if normalized != original:
        LOG.warning(
            "Normalized identity value %r to %r.", original, normalized
        )
    return normalized


def validate_collection(value: str) -> str:
    normalized = identity_component(value)
    if normalized not in ALLOWED_COLLECTIONS:
        raise ValueError(
            f"unrecognized collection {value!r}; expected one of: "
            f"{', '.join(sorted(ALLOWED_COLLECTIONS))}"
        )
    return normalized


def warn_if_test_collection(value: str) -> None:
    if validate_collection(value) == "toy":
        LOG.warning(
            "Collection 'toy' is test-only and is invalid for genuine submission."
        )


def identity_slug(collection: str, provenance: str) -> str:
    return (
        f"{validate_collection(collection)}-{identity_component(provenance)}"
    )


def checked_name(name: str, limit: int = 63) -> str:
    if not 3 <= len(name) <= limit or not re.fullmatch(
        r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", name
    ):
        raise ValueError(
            f"invalid resource name (maximum {limit} characters): {name!r}"
        )
    return name
