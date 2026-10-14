"""
Tests of parsing and serialization of Label objects from/to YAML.
"""

from datetime import date
from io import StringIO, UnsupportedOperation
from textwrap import dedent

from yaml import load as yaml_load, BaseLoader

from mast_transfer_tools.labels import Label, DeliveryMeta, TimeInfo


def check_omitted_label_props(label: Label) -> tuple[DeliveryMeta, TimeInfo]:
    assert label.filetypes == {}
    assert label.lpath == "/"
    assert label.shared_name_patterns == {}

    tm = label.time
    assert isinstance(tm, TimeInfo)
    assert tm.lpath == "/time"
    assert tm.observation_end_date is None
    assert tm.observation_start_date is None

    dm = label.delivery_meta
    assert isinstance(dm, DeliveryMeta)

    gvo = dm.global_validation_options
    assert gvo.missing_filetypes_ok is False
    assert gvo.no_assigned_filetype_ok is False
    assert gvo.skip == []

    return dm, tm


# This is the least amount of information you can put in a
# YAML-form label and have it parse without errors
MINIMAL_LABEL_YAML = dedent("""\
    dataset: empty
    delivery_id: 0
    time:
        delivery_start_date: 2025-10-20
    delivery_meta:
        schema_version: 0.0.1a0
""")

# This is the Python dict literal corresponding to what you get if
# you parse the minimal label, write it back out, and then read it
# in again using yaml.BaseLoader
MINIMAL_LABEL_DICT = {
    "contacts": {
        "archive": [],
        "provider": [],
    },
    "dataset": "empty",
    "delivery_id": "0",
    "delivery_meta": {
        "global_validation_options": {
            "missing_filetypes_ok": "false",
            "no_assigned_filetype_ok": "false",
            "skip": [],
        },
        "schema_version": "0.0.1a0",
    },
    "filetypes": {},
    "shared_name_patterns": {},
    "time": {
        "delivery_start_date": "2025-10-20",
    },
}


def test_minimal_label_parse() -> None:
    label = Label.from_text(MINIMAL_LABEL_YAML)
    assert label.errors == {}
    # All the omitted properties should have been filled in.
    dm, tm = check_omitted_label_props(label)

    assert label.dataset == "empty"
    assert label.delivery_id == 0

    assert dm.schema_version == "0.0.1a0"
    assert tm.delivery_start_date == date(2025, 10, 20)


def test_minimal_label_serialize() -> None:
    label = Label.from_text(MINIMAL_LABEL_YAML)
    assert label.errors == {}

    text_label = label.as_text()
    dict_label = yaml_load(text_label, Loader=BaseLoader)
    assert dict_label == MINIMAL_LABEL_DICT


def test_label_serialize_to_file_rewinds_and_distinguishes_labels() -> None:
    labels = [
        Label.from_text(MINIMAL_LABEL_YAML),
        Label.from_text(MINIMAL_LABEL_YAML.replace("dataset: empty", "dataset: other")),
    ]
    readbacks = []
    for label in labels:
        output = StringIO()
        label.serialize_to_file(output)
        readbacks.append(output.read())

    assert readbacks == [label.as_text() for label in labels]
    assert readbacks[0] != readbacks[1]


def test_label_serialize_to_non_seekable_file() -> None:
    class NonSeekableStringIO(StringIO):
        def seekable(self) -> bool:
            return False

        def seek(self, offset: int, whence: int = 0) -> int:
            raise UnsupportedOperation("not seekable")

    label = Label.from_text(MINIMAL_LABEL_YAML)
    output = NonSeekableStringIO()
    label.serialize_to_file(output)

    assert output.getvalue() == label.as_text()


def test_empty_label_parse() -> None:
    # Parsing the empty string should not crash (but it does produce
    # a label with a bunch of errors flagged).
    label = Label.from_text("")
    assert label.errors == {
        "/": ["wrong type; expected a mapping, not an absent value"],
        "/dataset": ["must always be defined"],
        "/delivery_id": ["must always be defined"],
        "/delivery_meta": ["must always be defined"],
        "/delivery_meta/schema_version": ["must always be defined"],
        "/time": ["must always be defined"],
        "/time/delivery_start_date": ["must always be defined"],
    }

    dm, tm = check_omitted_label_props(label)

    assert label.dataset == "<name missing>"
    assert label.delivery_id == "<delivery_id missing>"

    assert dm.schema_version == "<schema version missing>"
    assert tm.delivery_start_date == date(1900, 1, 1)
