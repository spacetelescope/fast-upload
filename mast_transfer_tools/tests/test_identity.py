import pytest

from mast_transfer_tools.utilz.identity import (
    identity_component,
    identity_slug,
)
from mast_transfer_tools.utilz import name_reference as names


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
    assert names.task_config_key("HLSP-TESS", "SPOC") != names.task_config_key(
        "HLSP", "TESS-SPOC"
    )


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
