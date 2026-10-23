from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence, Callable
from io import BytesIO
from types import SimpleNamespace

from astropy.io.fits import HDUList, ImageHDU, PrimaryHDU
from astropy.io.fits import open as fits_open
import numpy as np
import pandas as pd
import pytest

import mast_transfer_tools.server.core as validation_core_mod
import mast_transfer_tools.validation
from mast_transfer_tools.labels import Label
from mast_transfer_tools.server.core import (
    ValidationManager,
    ValidationSession,
)
from mast_transfer_tools.server.state import ValidationState
from mast_transfer_tools.s3log.helpers import LOG_FIELD_SPEC

from mast_transfer_tools.tests.mock_buckets import (
    FakeBucketRegistry,
    FakeMutableBucket,
)
from mast_transfer_tools.tests.mock_s3log import (
    MockS3TSVReader,
    MockS3TSVWriter,
)
from mast_transfer_tools.tests.test_label_parsing import MINIMAL_LABEL_YAML

COLLECTION = "empty"
PROVENANCE = "synthetic"
DELIVERY_ID = "0"
TRANSFER_TYPE = "staging"
CONFIG_BUCKET = "config-bucket"
CONTROL_BUCKET = "control-bucket"
TRANSFER_BUCKET = "transfer-bucket"
AGENT_ID = "validator-agent"


DEFAULT_VAL_SETTINGS = {
    "transfer_timeout": 10_000,
    "missing_timeout": 10_000,
    "loop_rate": 0,
    "n_val_threads": 1,
    "keepalive_threshold": 10_000,
    "az_id": "use1-az1",
}


DEFAULT_IDENTIFIERS = {
    "confb_name": CONFIG_BUCKET,
    "cb_name": CONTROL_BUCKET,
    "tb_name": TRANSFER_BUCKET,
    "collection": COLLECTION,
    "provenance": PROVENANCE,
    "delivery_id": DELIVERY_ID,
    "transfer_type": TRANSFER_TYPE,
    "agent_id": AGENT_ID,
}


def test_report_contains_raw_identity_and_canonical_label_path() -> None:
    session = ValidationSession.__new__(ValidationSession)
    session.identifiers = DEFAULT_IDENTIFIERS | {"collection": "HLSP", "provenance": "TESS-SPOC"}
    session.vstate = SimpleNamespace(n_completed=0, n_expected_files=0, n_failures=0, done=True)
    session.exception = None
    session.errors = {}
    report = session._format_sqs_report()
    assert report["collection"] == "HLSP"
    assert report["provenance"] == "TESS-SPOC"
    assert "dataset" not in report
    assert report["label_path"] == f"s3://{CONFIG_BUCKET}/{validation_core_mod.conf.LABEL_PREFIX}/hlsp-tess-spoc-0-label.yaml"


@pytest.mark.parametrize("field", ["collection", "provenance"])
@pytest.mark.parametrize("location", ["submitted", "accepted"])
def test_launch_rejects_mismatched_label_identity(
    monkeypatch: pytest.MonkeyPatch, field: str, location: str
) -> None:
    registry = FakeBucketRegistry()
    control = registry.get_or_make(CONTROL_BUCKET)
    config = registry.get_or_make(CONFIG_BUCKET)
    label_key = validation_core_mod.names.label_key(COLLECTION, PROVENANCE, DELIVERY_ID)
    changed = MINIMAL_LABEL_YAML.replace(
        f"{field}: {'empty' if field == 'collection' else 'synthetic'}", f"{field}: other"
    )
    control.put(changed if location == "submitted" else MINIMAL_LABEL_YAML, label_key, literal_str=True)
    config.put(changed if location == "accepted" else MINIMAL_LABEL_YAML,
               f"{validation_core_mod.conf.LABEL_PREFIX}/{label_key}", literal_str=True)
    install_validation_server_fakes(monkeypatch, registry)
    kwargs, error, exception = ValidationSession._init_launch_objs(
        "mast-fast", CONFIG_BUCKET, "use1-az4", CONTROL_BUCKET,
        COLLECTION, PROVENANCE, DELIVERY_ID, DEFAULT_VAL_SETTINGS,
        TRANSFER_BUCKET, TRANSFER_TYPE,
    )
    assert isinstance(exception, validation_core_mod.InvalidLabelError)
    assert "identity does not match" in str(exception)
    assert error["ref"] == label_key
    kwargs["logger"].stop()

FITS_LABEL_YAML = """
contacts:
  archive: []
  provider: []
collection: empty
provenance: synthetic
delivery_id: "0"
delivery_meta:
  schema_version: "0.0.1a0"
filetypes:
  sci:
    standard: FITS
    filename: .*\\.fits
    objects:
      - objtype: PRIMARY
        name: PRIMARY
        ndim: 0
      - objtype: IMAGE
        name: SCI
        dtype: i4
        ndim: 2
time:
  delivery_start_date: "2025-10-20"
"""


def make_sci_fits_blob(dtype: str) -> bytes:
    hdul = HDUList(
        [
            PrimaryHDU(),
            ImageHDU(
                np.arange(4, dtype=dtype).reshape(2, 2),
                name="SCI",
            ),
        ]
    )
    ostream = BytesIO()
    hdul.writeto(ostream)
    return ostream.getvalue()


def fake_loader_for_validation_server_fits_test(
    standard: str,
) -> Callable[[str, FakeMutableBucket], HDUList]:
    assert standard.lower() == "fits"

    def load_fits_from_fake_bucket(
        key: str, bucket: FakeMutableBucket
    ) -> HDUList:
        return fits_open(BytesIO(bucket.read(key, mode="rb")))

    return load_fits_from_fake_bucket


def bucket_factory_for(
    registry: FakeBucketRegistry,
) -> Callable[[str, ...], FakeMutableBucket]:
    """Return the Bucket replacement used by validation-core construction."""

    def bucket_factory(
        bucket_name: str, *_args: Any, **_kwargs: Any
    ) -> FakeMutableBucket:
        return registry.get_or_make(bucket_name)

    return bucket_factory


class FakeSQSClient:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def send_message(self, *, QueueUrl: str, MessageBody: str) -> None:
        if not isinstance(QueueUrl, str):
            raise TypeError("QueueUrl must be a string")
        if not isinstance(MessageBody, str):
            raise TypeError("MessageBody must be a string")


def make_fake_boto_client(name: str, **_: Any) -> object:
    if name == "sqs":
        return FakeSQSClient()
    return object()


def install_validation_server_fakes(
    monkeypatch: pytest.MonkeyPatch,
    registry: FakeBucketRegistry,
) -> None:
    """Patch validation-core external collaborators to deterministic fakes."""
    bucket_factory = bucket_factory_for(registry)

    monkeypatch.setattr(validation_core_mod, "Bucket", bucket_factory)
    monkeypatch.setattr(validation_core_mod, "S3TSVReader", MockS3TSVReader)
    monkeypatch.setattr(validation_core_mod, "S3TSVWriter", MockS3TSVWriter)
    monkeypatch.setattr(
        validation_core_mod, "make_boto_client", make_fake_boto_client
    )
    monkeypatch.setattr(
        validation_core_mod.time, "sleep", lambda *_a, **_kw: None
    )


def make_validation_index(
    paths: Sequence[str],
    *,
    type_: str | Mapping[str, str] | None = None,
    will_transfer: bool | Mapping[str, bool] = True,
    checksums: Mapping[str, str] | None = None,
) -> pd.DataFrame:
    records: list[dict[str, Any]] = []

    for path in paths:
        if isinstance(type_, Mapping):
            row_type = type_.get(path)
        else:
            row_type = type_

        if isinstance(will_transfer, Mapping):
            row_will_transfer = will_transfer.get(path, True)
        else:
            row_will_transfer = will_transfer

        records.append(
            {
                "path": path,
                "filename": Path(path).name,
                "type": row_type,
                "will_transfer": row_will_transfer,
                "status": "waiting" if row_will_transfer else "done",
                "n_fail": 0,
            }
        )

    index = pd.DataFrame.from_records(records)

    if checksums is not None:
        # Keep this object-y; NaN here is poison because validate_file()
        # checks `checksum is not None`.
        index["checksum"] = pd.Series(
            [checksums.get(path) for path in paths],
            dtype=object,
        )

    return index


@dataclass
class ValidationServerRig:
    session: ValidationSession
    registry: FakeBucketRegistry
    index: pd.DataFrame
    label: Label
    reader: MockS3TSVReader
    logger: MockS3TSVWriter
    manager: ValidationManager

    @property
    def control_bucket(self) -> FakeMutableBucket:
        return self.registry[CONTROL_BUCKET]

    @property
    def transfer_bucket(self) -> FakeMutableBucket:
        return self.registry[TRANSFER_BUCKET]

    def close(self) -> None:
        self.reader.stop()
        if not self.logger.stopped:
            self.logger.stop()
        self.manager.exc.shutdown(wait=True, cancel_futures=True)
        self.session._pipe_exec.shutdown(wait=True, cancel_futures=True)

    def put_transfer_files(self, files: Mapping[str, str | bytes]) -> None:
        for path, content in files.items():
            self.transfer_bucket.put(
                content, path, literal_str=isinstance(content, str)
            )

    @staticmethod
    def client_rows(
        rows: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        if isinstance(rows, Mapping):
            rows = [rows]

        cooked = []
        for row in rows:
            values = dict(row)
            values.setdefault("time", dt.datetime.now(dt.UTC).isoformat())
            values.setdefault("message", "")
            values.setdefault("agent_id", "client-agent")
            cooked.append(values)
        return cooked

    def push_client_rows(
        self,
        rows: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    ) -> None:
        self.reader.push(self.client_rows(rows))

    def push_transfer_successes(self, paths: Sequence[str]) -> None:
        self.push_client_rows(
            [
                {"category": "transfer", "ref": path, "status": "ok"}
                for path in paths
            ]
        )

    def push_transfer_failures(self, paths: Sequence[str]) -> None:
        self.push_client_rows(
            [
                {"category": "transfer", "ref": path, "status": "failure"}
                for path in paths
            ]
        )

    def push_keepalive(self) -> None:
        self.push_client_rows(
            {"category": "keepalive", "ref": "self", "status": "ok"}
        )

    def push_stop(self) -> None:
        self.push_client_rows(
            {"category": "stop", "ref": "self", "status": "ok"}
        )

    def push_client_shutdown_error(
        self, message: str = "client crashed"
    ) -> None:
        self.push_client_rows(
            {
                "category": "shutdown",
                "ref": "self",
                "status": "error",
                "message": message,
            }
        )

    def run_to_completion(self) -> None:
        """Run the validator loop synchronously.

        Tests using this must push a terminal client row, or this can spin
        forever. That is deliberate.
        """
        if not self.reader.running:
            self.reader.start()
        self.session._pipe_loop({})

    def validation_log(self) -> list[dict[str, Any]]:
        return [
            row for row in self.logger.rows if row["category"] == "validation"
        ]

    def shutdown_log(self) -> list[dict[str, Any]]:
        return [
            row for row in self.logger.rows if row["category"] == "shutdown"
        ]

    def stop_log(self) -> list[dict[str, Any]]:
        return [row for row in self.logger.rows if row["category"] == "stop"]

    def validator_statuses(self) -> dict[str, str]:
        return {row["ref"]: row["status"] for row in self.validation_log()}


def make_validation_server_rig(
    monkeypatch: pytest.MonkeyPatch,
    paths: Sequence[str],
    *,
    label: Label,
    registry: FakeBucketRegistry | None = None,
    transfer_bucket_files: Mapping[str, str | bytes] | None = None,
    checksums: Mapping[str, str] | None = None,
    type_: str | Mapping[str, str] | None = None,
    will_transfer: bool | Mapping[str, bool] = True,
    settings: Mapping[str, Any] | None = None,
    identifiers: Mapping[str, Any] | None = None,
    n_val_threads: int = 1,
) -> ValidationServerRig:
    registry = FakeBucketRegistry() if registry is None else registry
    control_bucket = registry.get_or_make(CONTROL_BUCKET)
    transfer_bucket = registry.get_or_make(TRANSFER_BUCKET)

    for key, value in (transfer_bucket_files or {}).items():
        transfer_bucket.put(value, key, literal_str=isinstance(value, str))

    install_validation_server_fakes(monkeypatch, registry)

    index = make_validation_index(
        paths,
        type_=type_,
        will_transfer=will_transfer,
        checksums=checksums,
    )

    val_settings = dict(DEFAULT_VAL_SETTINGS)
    val_settings["n_val_threads"] = n_val_threads
    if settings is not None:
        val_settings |= dict(settings)

    val_identifiers = dict(DEFAULT_IDENTIFIERS)
    if identifiers is not None:
        val_identifiers |= dict(identifiers)

    reader = MockS3TSVReader(
        bucket=control_bucket,
        key=validation_core_mod.names.log_key(TRANSFER_TYPE, "client"),
        fields=LOG_FIELD_SPEC,
    )
    logger = MockS3TSVWriter(
        bucket=control_bucket,
        key=validation_core_mod.names.log_key(TRANSFER_TYPE, "validator"),
        fields=LOG_FIELD_SPEC,
        fixed={"agent_id": val_identifiers["agent_id"]},
    )
    vstate = ValidationState(
        index=index,
        label=label,
        reader=reader,
        transfer_timeout=val_settings["transfer_timeout"],
        missing_timeout=val_settings["missing_timeout"],
    )
    manager = ValidationManager(
        transfer_bucket,
        index,
        label,
        n_threads=val_settings["n_val_threads"],
    )
    session = ValidationSession(
        vstate=vstate,
        manager=manager,
        logger=logger,
        settings=val_settings,
        identifiers=val_identifiers,
        index=index,
        label=label,
    )
    session.acquire_lock()

    return ValidationServerRig(
        session=session,
        registry=registry,
        index=index,
        label=label,
        reader=reader,
        logger=logger,
        manager=manager,
    )


def test_validation_session_happy_path_head_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    label = Label.from_text(MINIMAL_LABEL_YAML)

    rig = make_validation_server_rig(
        monkeypatch,
        ["alpha.txt", "nested/beta.txt"],
        label=label,
        transfer_bucket_files={
            "alpha.txt": "alpha",
            "nested/beta.txt": "beta",
        },
    )

    try:
        rig.push_transfer_successes(["alpha.txt", "nested/beta.txt"])
        rig.push_stop()

        rig.run_to_completion()

        assert rig.session.exception is None
        assert rig.validator_statuses() == {
            "alpha.txt": "ok",
            "nested/beta.txt": "ok",
        }

        assert len(rig.validation_log()) == 2
        assert len(rig.shutdown_log()) == 1
        assert rig.shutdown_log()[0]["status"] == "ok"
        assert len(rig.stop_log()) == 1

        assert rig.reader.stop_calls == 1
        assert rig.logger.stop_calls == 1

        assert rig.index.set_index("path").loc["alpha.txt", "status"] == "ok"
        assert (
            rig.index.set_index("path").loc["nested/beta.txt", "status"]
            == "ok"
        )

        assert [call["key"] for call in rig.transfer_bucket.calls["head"]] == [
            "alpha.txt",
            "nested/beta.txt",
        ]
        assert rig.transfer_bucket.calls["get"] == []
        assert rig.transfer_bucket.calls["read"] == []

    finally:
        rig.close()


def test_validation_session_happy_path_fits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    label = Label.from_text(FITS_LABEL_YAML)
    assert label.errors == {}

    monkeypatch.setattr(
        validation_core_mod,
        "loader_for",
        fake_loader_for_validation_server_fits_test,
    )

    rig = make_validation_server_rig(
        monkeypatch,
        ["science/example.fits"],
        label=label,
        type_="sci",
        transfer_bucket_files={
            "science/example.fits": make_sci_fits_blob("i4"),
        },
    )

    try:
        rig.push_transfer_successes(["science/example.fits"])
        rig.push_stop()

        rig.run_to_completion()

        assert rig.session.exception is None
        assert rig.validator_statuses() == {
            "science/example.fits": "ok",
        }

        assert len(rig.validation_log()) == 1
        assert rig.shutdown_log()[0]["status"] == "ok"
        assert len(rig.stop_log()) == 1

        assert (
            rig.index.set_index("path").loc["science/example.fits", "status"]
            == "ok"
        )

        assert [call["key"] for call in rig.transfer_bucket.calls["head"]] == [
            "science/example.fits",
        ]
        assert rig.transfer_bucket.calls["read"] == [
            {
                "key": "science/example.fits",
                "mode": "rb",
                "return_buffer": False,
                "start_byte": None,
                "end_byte": None,
            }
        ]
        assert rig.transfer_bucket.calls["get"] == []

    finally:
        rig.close()


def test_validation_session_val_failure_fits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    label = Label.from_text(FITS_LABEL_YAML)
    assert label.errors == {}

    monkeypatch.setattr(
        validation_core_mod,
        "loader_for",
        fake_loader_for_validation_server_fits_test,
    )

    rig = make_validation_server_rig(
        monkeypatch,
        ["science/example.fits"],
        label=label,
        type_="sci",
        transfer_bucket_files={
            "science/example.fits": make_sci_fits_blob("f8"),
        },
    )

    try:
        rig.push_transfer_successes(["science/example.fits"])
        rig.push_stop()

        rig.run_to_completion()

        assert rig.session.exception is None
        assert rig.validator_statuses() == {
            "science/example.fits": "failure",
        }

        assert len(rig.validation_log()) == 1
        assert rig.shutdown_log()[0]["status"] == "ok"
        assert len(rig.stop_log()) == 1

        assert (
            rig.index.set_index("path").loc["science/example.fits", "status"]
            == "failure"
        )

        assert [call["key"] for call in rig.transfer_bucket.calls["head"]] == [
            "science/example.fits",
        ]
        assert rig.transfer_bucket.calls["read"] == [
            {
                "key": "science/example.fits",
                "mode": "rb",
                "return_buffer": False,
                "start_byte": None,
                "end_byte": None,
            }
        ]
        assert rig.transfer_bucket.calls["get"] == []

    finally:
        rig.close()


FITS_HOOK_LABEL_YAML = """
contacts:
  archive: []
  provider: []
collection: empty
provenance: synthetic
delivery_id: "0"
delivery_meta:
  schema_version: "0.0.1a0"
filetypes:
  sci:
    standard: FITS
    filename: .*\\.fits
    validation_options:
      object_check_hook: fake_hook_module
time:
  delivery_start_date: "2025-10-20"
"""


def test_validation_session_object_check_hook_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    label = Label.from_text(FITS_HOOK_LABEL_YAML)
    assert label.errors == {}

    hook_calls = []

    def fake_hook(data: HDUList, spec: Any) -> dict[str, Any]:
        hook_calls.append((data, spec))
        return {"custom-rule": {"message": "hook saw the file"}}

    monkeypatch.setattr(
        validation_core_mod,
        "loader_for",
        fake_loader_for_validation_server_fits_test,
    )
    monkeypatch.setattr(
        mast_transfer_tools.validation,
        "load_object_check_hook",
        lambda module_name: fake_hook,
    )

    rig = make_validation_server_rig(
        monkeypatch,
        ["science/example.fits"],
        label=label,
        type_="sci",
        transfer_bucket_files={
            "science/example.fits": make_sci_fits_blob("i4"),
        },
    )

    try:
        rig.push_transfer_successes(["science/example.fits"])
        rig.push_stop()

        rig.run_to_completion()

        assert rig.session.exception is None
        assert rig.validator_statuses() == {
            "science/example.fits": "failure",
        }

        assert len(hook_calls) == 1
        assert hook_calls[0][1] is label.filetypes["sci"]

        row = rig.validation_log()[0]
        assert row["status"] == "failure"
        assert "hook:fake_hook_module" in repr(row["message"])
        assert "custom-rule" in repr(row["message"])

        assert [call["key"] for call in rig.transfer_bucket.calls["head"]] == [
            "science/example.fits",
        ]
        assert rig.transfer_bucket.calls["read"] == [
            {
                "key": "science/example.fits",
                "mode": "rb",
                "return_buffer": False,
                "start_byte": None,
                "end_byte": None,
            }
        ]
        assert rig.transfer_bucket.calls["get"] == []

    finally:
        rig.close()
