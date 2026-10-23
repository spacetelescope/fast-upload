"""Tests and fakes for UploadClient."""

from __future__ import annotations

import json
import threading
from collections import Counter
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import pandas as pd
import pytest

from mast_transfer_tools.tests.mock_buckets import (
    FakeBucketRegistry,
    FakeMutableBucket,
)
import mast_transfer_tools.upload.client as upload_client_mod
from mast_transfer_tools.tests.mock_s3log import (
    MockS3TSVReader,
    MockS3TSVWriter,
)
from mast_transfer_tools.upload.client import UploadClient
import mast_transfer_tools.utilz.name_reference as names
import mast_transfer_tools.config as conf


COLLECTION = "HLSP"
PROVENANCE = "TESS-SPOC"
DELIVERY_ID = "42"
TRANSFER_TYPE = "staging"

NETCONF_PARAMS = {
    "LOCK_STALENESS_THRESHOLD": 3600,
    "BUCKET_STEM": "test-buckets",
    "AVAILABILITY_ZONE_ID": "use1-az1",
    "INIT_LAMBDA_ARN": "arn:aws:lambda:test-region:123:function:test-init",
}

CONTROL_BUCKET = names.control_bucket(
    NETCONF_PARAMS["BUCKET_STEM"],
    COLLECTION,
    PROVENANCE,
    DELIVERY_ID,
    NETCONF_PARAMS["AVAILABILITY_ZONE_ID"],
)
TRANSFER_BUCKET = names.transfer_bucket(
    NETCONF_PARAMS["BUCKET_STEM"], COLLECTION, PROVENANCE, DELIVERY_ID, TRANSFER_TYPE
)
AGENT_ID = "client-agent-1"


@dataclass(frozen=True)
class LambdaInvokeCall:
    """Recorded call to ``FakeLambdaClient.invoke()``."""

    function_name: str
    payload: bytes
    kwargs: dict[str, Any]

    @property
    def text_payload(self) -> str:
        """Return the invoke payload decoded as UTF-8 text."""
        return self.payload.decode("utf-8")

    @property
    def json_payload(self) -> dict[str, Any]:
        """Return the invoke payload parsed as JSON."""
        parsed = json.loads(self.text_payload)
        if not isinstance(parsed, dict):
            raise TypeError("lambda payload JSON was not an object")
        return parsed


class FakeLambdaClient:
    """Minimal mock of the Lambda client used by ``UploadClient``.

    The fake records every ``invoke()`` call and returns a dict containing a
    readable ``Payload`` object, matching the only part of boto3's response
    shape that ``UploadClient.initiate_transfer()`` currently consumes.
    """

    def __init__(
        self,
        response: str | bytes = b"success",
        *,
        invoke_exception: BaseException | None = None,
    ) -> None:
        self.response = response
        self.invoke_exception = invoke_exception
        self.calls: list[LambdaInvokeCall] = []

    def invoke(
        self,
        *,
        FunctionName: str,
        Payload: bytes,
        **kwargs: Any,
    ) -> dict[str, BytesIO]:
        """Record one invocation and return the configured response."""
        self.calls.append(
            LambdaInvokeCall(
                function_name=FunctionName,
                payload=Payload,
                kwargs=dict(kwargs),
            )
        )
        if self.invoke_exception is not None:
            raise self.invoke_exception
        response = self.response
        if isinstance(response, str):
            response = response.encode("utf-8")
        return {"Payload": BytesIO(response)}

    @property
    def last_call(self) -> LambdaInvokeCall:
        """Return the most recent invoke call."""
        if not self.calls:
            raise AssertionError("FakeLambdaClient has not been invoked")
        return self.calls[-1]


class FakeSSMClient:
    """
    Fake SSM client that only returns fixed network configuration parameters.
    """

    def get_parameter(
        self,
        Name: str,
        WithDecryption: bool,  # noqa: FBT001
    ) -> dict[str, dict[str, str]]:
        if WithDecryption is not True:
            raise ValueError("WithDecryption munst be True")
        if Name != conf.NETWORK_CONFIG_PARAMETER:
            raise ValueError(
                "Name must be the configured value of NETWORK_CONFIG_PARAMETER"
            )
        return {"Parameter": {"Value": json.dumps(NETCONF_PARAMS)}}


@dataclass(frozen=True)
class SessionClientCall:
    """Recorded call to ``FakeSession.client()``."""

    service_name: str
    config: Any = None
    kwargs: dict[str, Any] | None = None


class FakeSession:
    """Minimal mock session for upload-client tests."""

    def __init__(
        self,
        lambda_client: FakeLambdaClient | None = None,
        ssm_client: FakeSSMClient | None = None,
    ) -> None:
        self.lambda_client = (
            FakeLambdaClient() if lambda_client is None else lambda_client
        )
        self.ssm_client = FakeSSMClient() if ssm_client is None else ssm_client
        self.client_calls: list[SessionClientCall] = []

    def client(
        self,
        service_name: str,
        *,
        config: Any = None,
        **kwargs: Any,
    ) -> FakeLambdaClient | FakeSSMClient:
        """Return the fake client for ``service_name == 'lambda' or 'ssm'``."""
        self.client_calls.append(
            SessionClientCall(
                service_name=service_name,
                config=config,
                kwargs=dict(kwargs),
            )
        )
        if service_name == "lambda":
            return self.lambda_client
        if service_name == "ssm":
            return self.ssm_client
        raise NotImplementedError(
            f"FakeSession only implements client('lambda' | 'ssm'), got "
            f"client({service_name!r})"
        )


@dataclass
class UploadClientRig:
    """Small test harness for an UploadClient connected to fake AWS objects."""

    client: UploadClient
    registry: FakeBucketRegistry
    fake_session: FakeSession
    source: Path
    monkeypatch: pytest.MonkeyPatch
    bucket_factory: Callable[..., Any]

    @property
    def control_bucket(self) -> FakeMutableBucket:
        return self.registry[CONTROL_BUCKET]

    @property
    def transfer_bucket(self) -> FakeMutableBucket:
        return self.registry[TRANSFER_BUCKET]

    def connect(self) -> None:
        # Patch Bucket only while UploadClient._connect() constructs its bucket
        # handles. Keeping Bucket patched to a plain factory after connect() is
        # a footgun because client.DataRoot also uses that global name in
        # isinstance checks. The fake bucket objects themselves remain attached
        # to the client after this context exits.
        with self.monkeypatch.context() as m:
            m.setattr(upload_client_mod, "Bucket", self.bucket_factory)
            self.client.connect()

    def launch(self) -> None:
        self.connect()
        self.client.initiate_transfer()
        self.client.write_index()

    def close(self) -> None:
        if self.client.state != "quit":
            self.client.quit()

    def assert_ready_and_locked(self) -> None:
        assert self.client.state == "ready"
        assert self.control_bucket.read("lock/client") == AGENT_ID
        assert isinstance(self.client.reader, MockS3TSVReader)
        assert isinstance(self.client.logger, MockS3TSVWriter)

    def assert_unlocked_and_stopped(self) -> None:
        assert self.client.state == "quit"
        assert self.client.locked is False
        assert "lock/client" not in self.control_bucket.objects
        assert self.client.logger.stopped is True

    def queue_all_transfers(self) -> list[str]:
        queued = []
        while True:
            file = self.client.transfer_next_file()
            if file is None:
                return queued
            queued.append(file.relpath)

    def wait_for_transfers(self, *, timeout: float = 2) -> None:
        for future in self.client.futures:
            future.result(timeout=timeout)

    def transfer_one_expected(
        self, expected_relpath: str
    ) -> upload_client_mod.ClientFile:
        file = self.client.transfer_next_file()
        assert file is not None
        assert file.relpath == expected_relpath
        self.client.futures[-1].result(timeout=2)
        return file

    def push_validation_successes(self, paths: Sequence[str]) -> None:
        self.client.reader.push(
            [
                {"category": "validation", "ref": path, "status": "ok"}
                for path in paths
            ]
        )

    def push_stop(self) -> None:
        self.client.reader.push(
            {"category": "stop", "ref": "self", "status": "ok"}
        )

    def file_states(self) -> dict[str, str]:
        return {file.relpath: file.state for file in self.client.file_list}

    def transfer_log(self) -> list[dict[str, Any]]:
        return [
            row
            for row in self.client.logger.rows
            if row["category"] == "transfer"
        ]

    def uploaded_index(self) -> pd.DataFrame:
        return pd.read_csv(
            BytesIO(self.control_bucket.objects[self.client.index_key]),
        )


def make_source_tree(tmp_path: Path, files: Mapping[str, str]) -> Path:
    """Create a local upload source tree from relative paths to text."""
    source = tmp_path / "source"
    source.mkdir()
    for relpath, text in files.items():
        path = source / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return source


def bucket_factory_for(registry: FakeBucketRegistry) -> Callable[..., Any]:
    """Return the Bucket replacement used while UploadClient connects."""

    def bucket_factory(
        bucket_name: str, *_args: Any, **_kwargs: Any
    ) -> FakeMutableBucket:
        return registry.get_or_make(bucket_name)

    return bucket_factory


def install_upload_client_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch non-Bucket collaborators to deterministic fakes."""
    monkeypatch.setattr(upload_client_mod, "S3TSVReader", MockS3TSVReader)
    monkeypatch.setattr(upload_client_mod, "S3TSVWriter", MockS3TSVWriter)
    monkeypatch.setattr(
        upload_client_mod.time, "sleep", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(UploadClient, "dump_state", lambda self: None)
    monkeypatch.setattr(
        UploadClient, "cmessage", lambda self, text, mtype: None
    )


def make_upload_client_rig(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    files: Mapping[str, str],
    *,
    registry: FakeBucketRegistry | None = None,
    fake_session: FakeSession | None = None,
    transfer_bucket_files: Mapping[str, str | bytes] | None = None,
    n_threads: int = 1,
    max_retries: int = 2,
) -> UploadClientRig:
    """Build a local-source UploadClient wired to in-memory AWS fakes."""
    source = make_source_tree(tmp_path, files)
    registry = FakeBucketRegistry() if registry is None else registry
    fake_session = FakeSession() if fake_session is None else fake_session

    if transfer_bucket_files is not None and TRANSFER_BUCKET not in registry:
        registry.make(TRANSFER_BUCKET, files=dict(transfer_bucket_files))

    install_upload_client_fakes(monkeypatch)
    bucket_factory = bucket_factory_for(registry)

    client = upload_client_mod.UploadClient(
        collection=COLLECTION,
        provenance=PROVENANCE,
        delivery_id=DELIVERY_ID,
        file_index=pd.DataFrame({"path": list(files)}),
        transfer_type=TRANSFER_TYPE,
        source=source,
        lambda_client_config={},
        own_agent_id=AGENT_ID,
        n_threads=n_threads,
        max_retries=max_retries,
    )
    client.session = fake_session

    return UploadClientRig(
        client, registry, fake_session, source, monkeypatch, bucket_factory
    )


def assert_lambda_was_invoked_for_upload(fake_session: FakeSession) -> None:
    call = fake_session.lambda_client.last_call
    assert call.function_name == NETCONF_PARAMS["INIT_LAMBDA_ARN"]

    payload = call.json_payload
    assert payload["collection"] == COLLECTION
    assert payload["provenance"] == PROVENANCE
    assert "dataset" not in payload
    assert payload["delivery_id"] == DELIVERY_ID
    assert payload["transfer_type"] == TRANSFER_TYPE
    assert payload["agent_id"] == AGENT_ID
    assert 10000 <= payload["idempotency_key"] <= 99999


def validator_log_text(*rows: tuple[str, str, str, str]) -> str:
    """Build old validator log text from (ref, status, message, agent_id)."""
    return "".join(
        f"2026-01-01T00:00:{i:02d}Z\tvalidation\t{ref}\t{status}\t{message}\t{agent_id}\n"
        for i, (ref, status, message, agent_id) in enumerate(rows)
    )


def test_upload_client_local_happy_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A small local-to-S3 transfer story with validator success."""
    rig = make_upload_client_rig(
        monkeypatch,
        tmp_path,
        {
            "alpha.txt": "alpha",
            "nested/beta.txt": "beta",
        },
    )
    client = rig.client

    rig.connect()
    rig.assert_ready_and_locked()

    client.initiate_transfer()
    assert_lambda_was_invoked_for_upload(rig.fake_session)

    client.write_index()
    assert client.index_key in rig.control_bucket.objects

    first = client.transfer_next_file()
    second = client.transfer_next_file()
    assert first is not None
    assert second is not None

    rig.wait_for_transfers()

    assert rig.transfer_bucket.read("alpha.txt") == "alpha"
    assert rig.transfer_bucket.read("nested/beta.txt") == "beta"
    assert [call["key"] for call in rig.transfer_bucket.calls["put"]] == [
        "alpha.txt",
        "nested/beta.txt",
    ]

    rig.push_validation_successes(["alpha.txt", "nested/beta.txt"])
    assert client.update() is False
    assert client.state == "pending_validation"
    assert all(file.state == "valid" for file in client.file_list)

    rig.push_stop()
    assert client.update() is True
    assert client.validation_complete is True
    rig.assert_unlocked_and_stopped()


def test_validator_success_before_transfer_marks_file_invalid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Validator success for an untransferred file is protocol corruption."""
    rig = make_upload_client_rig(
        monkeypatch,
        tmp_path,
        {"alpha.txt": "alpha"},
    )
    client = rig.client

    rig.connect()

    try:
        client.initiate_transfer()
        client.write_index()

        file = client.file_list[0]

        assert client.state == "ready"
        assert file.state == "ready"
        assert rig.transfer_bucket.objects == {}
        assert rig.transfer_bucket.calls["put"] == []

        client.reader.push(
            {
                "category": "validation",
                "ref": "alpha.txt",
                "status": "ok",
                "message": "validator reported success before transfer",
            }
        )

        assert client.update() is False

        assert file.state == "invalid"
        assert file.message is not None
        assert "before transfer" in file.message.lower()
        assert rig.transfer_bucket.objects == {}
        assert rig.transfer_bucket.calls["put"] == []

    finally:
        rig.close()


def test_upload_client_resumes_interrupted_upload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Resume from prior validator logs without reuploading valid objects."""
    registry = FakeBucketRegistry()
    control_bucket = registry.make(CONTROL_BUCKET)
    transfer_bucket_files = {
        "alpha.txt": "alpha-old-valid-object",
        "gamma.txt": "gamma-old-invalid-object",
    }

    control_bucket.put(
        validator_log_text(
            ("alpha.txt", "ok", "previously valid", "validator-agent-1"),
            (
                "beta.txt",
                "ok",
                "previously valid but missing",
                "validator-agent-1",
            ),
            ("gamma.txt", "failure", "bad old file", "validator-agent-1"),
            ("ignored.txt", "ok", "not in this manifest", "validator-agent-1"),
        ),
        upload_client_mod.names.log_key(TRANSFER_TYPE, "validator"),
        literal_str=True,
    )

    rig = make_upload_client_rig(
        monkeypatch,
        tmp_path,
        {
            "alpha.txt": "alpha-new",
            "beta.txt": "beta-new",
            "gamma.txt": "gamma-new",
            "delta.txt": "delta-new",
        },
        registry=registry,
        transfer_bucket_files=transfer_bucket_files,
    )
    client = rig.client

    try:
        rig.connect()

        rig.assert_ready_and_locked()
        assert rig.file_states() == {
            "alpha.txt": "valid",
            "beta.txt": "ready",
            "gamma.txt": "ready",
            "delta.txt": "ready",
        }

        client.initiate_transfer()
        client.write_index()

        uploaded_index = rig.uploaded_index()
        will_transfer = dict(
            zip(uploaded_index["path"], uploaded_index["will_transfer"])
        )
        assert will_transfer == {
            "alpha.txt": False,
            "beta.txt": True,
            "gamma.txt": True,
            "delta.txt": True,
        }

        queued = rig.queue_all_transfers()
        rig.wait_for_transfers()

        assert queued == ["beta.txt", "gamma.txt", "delta.txt"]

        assert (
            rig.transfer_bucket.read("alpha.txt") == "alpha-old-valid-object"
        )
        assert rig.transfer_bucket.read("beta.txt") == "beta-new"
        assert rig.transfer_bucket.read("gamma.txt") == "gamma-new"
        assert rig.transfer_bucket.read("delta.txt") == "delta-new"

        assert [call["key"] for call in rig.transfer_bucket.calls["put"]] == [
            "beta.txt",
            "gamma.txt",
            "delta.txt",
        ]

        assert rig.file_states() == {
            "alpha.txt": "valid",
            "beta.txt": "done",
            "gamma.txt": "done",
            "delta.txt": "done",
        }

    finally:
        rig.close()


def test_one_file_exhausts_retries_while_other_file_validates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failed upload can become complete by exhausting retries."""
    registry = FakeBucketRegistry()
    transfer_bucket = registry.make(TRANSFER_BUCKET)
    original_put = transfer_bucket.put
    failed_puts: list[str] = []

    def put_with_persistent_beta_failure(
        obj: Any = b"",
        key: str | None = None,
        **kwargs: Any,
    ) -> None:
        if key == "beta.txt":
            failed_puts.append(key)
            raise OSError("synthetic persistent upload failure")
        return original_put(obj, key, **kwargs)

    monkeypatch.setattr(
        transfer_bucket,
        "put",
        put_with_persistent_beta_failure,
    )

    rig = make_upload_client_rig(
        monkeypatch,
        tmp_path,
        {
            "alpha.txt": "alpha",
            "beta.txt": "beta",
        },
        registry=registry,
        max_retries=2,
    )
    client = rig.client

    try:
        rig.launch()

        alpha = rig.transfer_one_expected("alpha.txt")
        assert alpha.state == "done"
        assert rig.transfer_bucket.read("alpha.txt") == "alpha"

        beta = rig.transfer_one_expected("beta.txt")
        assert beta.state == "failed"
        assert beta.retries == 1
        assert beta.can_transfer is True
        assert beta.no_retry is False

        same_beta = rig.transfer_one_expected("beta.txt")
        assert same_beta is beta
        assert beta.state == "failed"
        assert beta.retries == 2
        assert beta.can_transfer is False
        assert beta.no_retry is True

        assert failed_puts == ["beta.txt", "beta.txt"]
        assert "beta.txt" not in rig.transfer_bucket.objects

        assert client.transfer_next_file() is None
        assert client.transfer_complete is True
        assert client.validation_complete is False

        rig.push_validation_successes(["alpha.txt"])
        assert client.update() is False

        assert alpha.state == "valid"
        assert beta.state == "failed"
        assert beta.no_retry is True
        assert client.validation_complete is True
        assert client.n_complete == 2
        assert client.state == "pending_validation"

        rig.push_stop()
        assert client.update() is True

        assert client.validation_complete is True
        rig.assert_unlocked_and_stopped()

        assert [(row["ref"], row["status"]) for row in rig.transfer_log()] == [
            ("alpha.txt", "ok"),
            ("beta.txt", "error"),
            ("beta.txt", "error"),
        ]

    finally:
        rig.close()


def test_concurrent_transfer_scheduling_uploads_each_file_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Concurrent scheduling does not skip or duplicate manifest files."""
    n_files = 12
    n_threads = 4
    paths = [f"file-{i:02d}.txt" for i in range(n_files)]

    registry = FakeBucketRegistry()
    transfer_bucket = registry.make(TRANSFER_BUCKET)

    release_puts = threading.Event()
    enough_puts_started = threading.Event()
    started_put_keys: list[str] = []
    started_put_lock = threading.Lock()

    original_put = transfer_bucket.put

    def blocking_put(
        obj: Any = b"",
        key: str | None = None,
        **kwargs: Any,
    ) -> None:
        with started_put_lock:
            if key is not None:
                started_put_keys.append(key)
            if len(started_put_keys) >= n_threads:
                enough_puts_started.set()

        if not release_puts.wait(timeout=5):
            raise TimeoutError("transfer gate was never released")

        return original_put(obj, key, **kwargs)

    monkeypatch.setattr(transfer_bucket, "put", blocking_put)

    rig = make_upload_client_rig(
        monkeypatch,
        tmp_path,
        {path: f"contents for {path}" for path in paths},
        registry=registry,
        n_threads=n_threads,
    )
    client = rig.client

    try:
        rig.launch()

        queued = rig.queue_all_transfers()

        assert queued == paths
        assert len(client.futures) == n_files

        assert enough_puts_started.wait(timeout=2)
        assert len(started_put_keys) == n_threads

        in_flight_states = {file.state for file in client.file_list}
        assert in_flight_states <= {"pending", "transferring"}
        assert "transferring" in in_flight_states

        assert client.transfer_next_file() is None
        assert len(client.futures) == n_files
        assert client.transfer_complete is False

        release_puts.set()
        rig.wait_for_transfers()

        assert client.transfer_complete is True
        assert client.validation_complete is False

        assert sorted(rig.transfer_bucket.objects) == paths
        for path in paths:
            assert rig.transfer_bucket.read(path) == f"contents for {path}"

        put_keys = [call["key"] for call in rig.transfer_bucket.calls["put"]]
        assert sorted(put_keys) == paths
        assert Counter(put_keys) == Counter(paths)

        assert {file.state for file in client.file_list} == {"done"}
        assert Counter(
            (row["ref"], row["status"]) for row in rig.transfer_log()
        ) == Counter((path, "ok") for path in paths)

        rig.push_validation_successes(paths)
        assert client.update() is False
        assert client.state == "pending_validation"
        assert client.validation_complete is True
        assert {file.state for file in client.file_list} == {"valid"}

        rig.push_stop()
        assert client.update() is True
        rig.assert_unlocked_and_stopped()

    finally:
        release_puts.set()
        rig.close()
