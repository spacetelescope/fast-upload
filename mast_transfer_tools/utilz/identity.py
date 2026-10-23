import re


def identity_component(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9](?:[A-Za-z0-9_-]*[A-Za-z0-9])?", value
    ):
        raise ValueError(
            "must be a nonempty ASCII identifier with alphanumeric ends "
            "and only letters, digits, hyphens, or underscores"
        )
    return value.lower().replace("_", "-")


def identity_slug(collection: str, provenance: str) -> str:
    return f"{identity_component(collection)}-{identity_component(provenance)}"


def checked_name(name: str, limit: int = 63) -> str:
    if not 3 <= len(name) <= limit or not re.fullmatch(
        r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", name
    ):
        raise ValueError(
            f"invalid resource name (maximum {limit} characters): {name!r}"
        )
    return name
