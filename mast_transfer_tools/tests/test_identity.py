import pytest

from mast_transfer_tools.utilz.identity import (
    ALLOWED_COLLECTIONS,
    identity_component,
    identity_slug,
    validate_collection,
    warn_if_test_collection,
)
from mast_transfer_tools.utilz import name_reference as names

EXPECTED_COLLECTIONS = (
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


def test_allowed_collection_values() -> None:
    assert ALLOWED_COLLECTIONS == frozenset(EXPECTED_COLLECTIONS)


@pytest.mark.parametrize("value", EXPECTED_COLLECTIONS)
def test_collection_case_matching(value: str) -> None:
    for spelling in (value, value.upper(), value.title()):
        assert validate_collection(spelling) == value


@pytest.mark.parametrize("value", ["test", "empty", "cli-smoke", "hlsp-typo"])
def test_unknown_collection(value: str) -> None:
    with pytest.raises(ValueError, match="unrecognized collection") as raised:
        validate_collection(value)
    assert ", ".join(EXPECTED_COLLECTIONS) in str(raised.value)
    assert identity_component(value) == value
    with pytest.raises(ValueError, match="unrecognized collection"):
        identity_slug(value, "synthetic")


@pytest.mark.parametrize("value", ["toy", "TOY", " \tToY\n"])
def test_toy_collection_warns(
    value: str, caplog: pytest.LogCaptureFixture
) -> None:
    warn_if_test_collection(value)
    assert "test-only" in caplog.text
    assert "is invalid for genuine submission" in caplog.text


def test_validation_and_naming_do_not_warn_for_toy(
    caplog: pytest.LogCaptureFixture,
) -> None:
    for invocation in range(2):
        assert validate_collection("toy") == "toy"
        assert identity_slug("toy", "synthetic") == "toy-synthetic"
        assert (
            names.label_key("toy", "synthetic", invocation)
            == f"toy-synthetic-{invocation}-label.yaml"
        )
    assert caplog.records == []


def test_test_collection_warning_is_not_process_cached(
    caplog: pytest.LogCaptureFixture,
) -> None:
    for invocation in range(2):
        warn_if_test_collection("toy")
        records = [
            record
            for record in caplog.records
            if "test-only" in record.getMessage()
        ]
        assert len(records) == invocation + 1


def test_identity_trims_whitespace(caplog: pytest.LogCaptureFixture) -> None:
    assert validate_collection(" \tHlSp\n") == "hlsp"
    assert identity_component(" \tTESS_SPOC\n") == "tess-spoc"
    assert identity_slug(" HLSP ", " TESS_SPOC ") == "hlsp-tess-spoc"
    assert "Normalized identity value" in caplog.text


@pytest.mark.parametrize(
    ("value", "expected"),
    [("HlSp", "hlsp"), ("tess_spoc", "tess-spoc"), (" hlsp ", "hlsp")],
)
def test_normalization_notice(
    value: str, expected: str, caplog: pytest.LogCaptureFixture
) -> None:
    assert identity_component(value) == expected
    assert (
        f"Normalized identity value {value!r} to {expected!r}." in caplog.text
    )


def test_normalized_values_are_quiet(caplog: pytest.LogCaptureFixture) -> None:
    assert validate_collection("hlsp") == "hlsp"
    assert identity_component("tess-spoc") == "tess-spoc"
    assert identity_slug("hlsp", "tess-spoc") == "hlsp-tess-spoc"
    warn_if_test_collection("hlsp")
    assert caplog.records == []


def test_identity_names() -> None:
    assert identity_slug("HLSP", "TESS_SPOC") == "hlsp-tess-spoc"
    assert (
        names.transfer_bucket("mast-fast", "HLSP", "TESS-SPOC", "42", "sample")
        == "mast-fast-hlsp-tess-spoc-42-sample"
    )
    assert (
        names.control_bucket(
            "mast-fast", "HLSP", "TESS-SPOC", "42", "use1-az4"
        )
        == "mast-fast-hlsp-tess-spoc-42-control--use1-az4--x-s3"
    )
    assert (
        names.label_key("HLSP", "TESS-SPOC", 42)
        == "hlsp-tess-spoc-42-label.yaml"
    )
    assert (
        names.index_key("HLSP", "TESS-SPOC", "42", "sample")
        == "hlsp-tess-spoc-42-sample-index.csv"
    )
    assert (
        names.task_config_key("HLSP", "TESS-SPOC")
        == "hlsp/tess-spoc-task-config.yaml"
    )
    with pytest.raises(ValueError, match="unrecognized collection"):
        names.task_config_key("HLSP-TESS", "SPOC")


@pytest.mark.parametrize(
    "value",
    ["", " ", "../bad", "bad/path", "-bad", "bad_", "caf\u00e9", None, 123],
)
def test_invalid_identity_component(value: object) -> None:
    with pytest.raises(ValueError, match="nonempty ASCII identifier"):
        identity_component(value)


def test_bucket_length_boundaries() -> None:
    prefix = "mast-fast-hlsp-"
    suffix = "-42-control--use1-az4--x-s3"
    provenance = "a" * (63 - len(prefix) - len(suffix))
    assert (
        len(
            names.control_bucket(
                "mast-fast", "HLSP", provenance, "42", "use1-az4"
            )
        )
        == 63
    )
    with pytest.raises(ValueError, match="maximum 63"):
        names.control_bucket(
            "mast-fast", "HLSP", provenance + "a", "42", "use1-az4"
        )
