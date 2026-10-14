"""Behavioral tests for the upload-init Lambda boundary."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest
import yaml

import mast_transfer_tools.config as conf
import mast_transfer_tools.lambda_.core as lambda_core
import mast_transfer_tools.utilz.name_reference as names
from mast_transfer_tools.tests.mock_buckets import (
    FakeBucketRegistry,
    FakeMutableBucket,
)

DATASET = "dataset-one"
DELIVERY_ID = "delivery-one"
TRANSFER_TYPE = "staging"
AGENT_ID = "client-agent-1"
CONFIG_BUCKET = "fast-config"
TASK_CONFIG_PREFIX = "task-configs"

NETCONF = {
    "AVAILABILITY_ZONE_ID": "use1-az1",
    "BUCKET_STEM": "test-buckets",
    "CONFIG_BUCKET": CONFIG_BUCKET,
    "INIT_LAMBDA_ARN": "arn:aws:lambda:test-region:123:function:test-init",
    "LOCK_STALENESS_THRESHOLD": 3600,
    "TASK_CONFIG_PREFIX": TASK_CONFIG_PREFIX,
}
RESOURCE_TAGS = {"Project": "MAST FAST", "Environment": "test"}

CONTROL_BUCKET = names.control_bucket(
    NETCONF["BUCKET_STEM"],
    DATASET,
    DELIVERY_ID,
    NETCONF["AVAILABILITY_ZONE_ID"],
)

DEFAULT_TASK_CONFIG = {
    "az_id": "use1-az1",
    "subnet_id": "subnet-default",
    "cluster": "cluster-default",
    "sg_id": "sg-default",
    "family": "family-default",
    "cpu": "256",
    "memory": "512",
}


class FakeSSMClient:
    """Fake SSM client serving the Lambda's two parameter-store reads."""

    def __init__(
        self,
        *,
        netconf: dict[str, Any] | None = None,
        tags: dict[str, str] | None = None,
    ) -> None:
        self.netconf = dict(NETCONF if netconf is None else netconf)
        self.tags = dict(RESOURCE_TAGS if tags is None else tags)
        self.calls: list[dict[str, Any]] = []

    def get_parameter(
        self, *, Name: str, WithDecryption: bool
    ) -> dict[str, Any]:
        self.calls.append({"Name": Name, "WithDecryption": WithDecryption})
        if WithDecryption is not True:
            raise ValueError("Lambda must request decrypted parameters")
        if Name == conf.NETWORK_CONFIG_PARAMETER:
            value = self.netconf
        elif Name == "/mast-fast/resource-tags":
            value = self.tags
        else:
            raise ValueError(f"Unexpected parameter {Name!r}")
        return {"Parameter": {"Value": json.dumps(value)}}


@dataclass
class FakeECSClient:
    """Fake ECS client that records task launch requests."""

    run_task_calls: list[dict[str, Any]] = field(default_factory=list)
    run_task_exception: BaseException | None = None

    def run_task(self, **kwargs: Any) -> dict[str, Any]:
        self.run_task_calls.append(dict(kwargs))
        if self.run_task_exception is not None:
            raise self.run_task_exception
        return {"tasks": [{"taskArn": "arn:aws:ecs:task/test-task"}]}


class FakeECSTask:
    """Fake ECSTask with controllable logs."""

    def __init__(self, _task: dict[str, Any], logs: list[str] | None = None) -> None:
        self.logs = (
            ["Initializing pipeline for tests"] if logs is None else logs
        )
        self.wait_calls: list[dict[str, Any]] = []

    def wait_while_pending(self, *, timeout: int) -> None:
        self.wait_calls.append({"timeout": timeout})

    def get_logs(self) -> list[str]:
        return list(self.logs)


@dataclass
class LambdaRig:
    registry: FakeBucketRegistry
    ssm: FakeSSMClient
    ecs: FakeECSClient

    @property
    def control_bucket(self) -> FakeMutableBucket:
        return self.registry[CONTROL_BUCKET]

    @property
    def config_bucket(self) -> FakeMutableBucket:
        return self.registry[CONFIG_BUCKET]


def event(**overrides: Any) -> dict[str, Any]:
    base = {
        "dataset": DATASET,
        "delivery_id": DELIVERY_ID,
        "transfer_type": TRANSFER_TYPE,
        "agent_id": AGENT_ID,
    }
    base.update(overrides)
    return base


def put_task_config(
    rig: LambdaRig,
    name: str,
    config: dict[str, Any],
) -> None:
    rig.config_bucket.put(
        yaml.safe_dump(config),
        f"{TASK_CONFIG_PREFIX}/{name}-task-config.yaml",
        literal_str=True,
    )


def make_lambda_rig(
    monkeypatch: pytest.MonkeyPatch,
    *,
    default_config: dict[str, Any] | None = DEFAULT_TASK_CONFIG,
    dataset_config: dict[str, Any] | None = None,
    client_lock: str | None = AGENT_ID,
    running_tasks: list[dict[str, Any]] | None = None,
    ecs: FakeECSClient | None = None,
) -> LambdaRig:
    registry = FakeBucketRegistry()
    registry.make(CONTROL_BUCKET)
    registry.make(CONFIG_BUCKET)

    rig = LambdaRig(
        registry=registry,
        ssm=FakeSSMClient(),
        ecs=FakeECSClient() if ecs is None else ecs,
    )
    if client_lock is not None:
        rig.control_bucket.put(
            client_lock,
            names.lock_key("client"),
            literal_str=True,
        )
    if default_config is not None:
        put_task_config(rig, "default", default_config)
    if dataset_config is not None:
        put_task_config(rig, DATASET, dataset_config)

    def bucket_factory(
        bucket_name: str, *_args: Any, **_kwargs: Any
    ) -> FakeMutableBucket:
        return registry.get_or_make(bucket_name)

    def fake_ls_tasks(
        *,
        name: str,
        status: str | None = None,
        **_kwargs: Any,
    ) -> list[dict[str, Any]]:
        if status == "RUNNING":
            return list(running_tasks or [])
        return [{"name": name, "taskArn": "arn:aws:ecs:task/test-task"}]

    monkeypatch.setattr(lambda_core, "Bucket", bucket_factory)
    monkeypatch.setattr(lambda_core, "make_boto_client", lambda _: rig.ssm)
    monkeypatch.setattr(lambda_core, "init_client", lambda _: rig.ecs)
    monkeypatch.setattr(lambda_core, "ls_tasks", fake_ls_tasks)
    monkeypatch.setattr(lambda_core, "ECSTask", FakeECSTask)
    monkeypatch.setattr(lambda_core.time, "sleep", lambda *_args: None)
    monkeypatch.setattr(lambda_core.random, "randint", lambda *_args: 1234567)

    return rig


def response_from_lambda(result: str) -> dict[str, Any]:
    parsed = yaml.safe_load(result)
    assert isinstance(parsed, dict)
    return parsed


def only_run_task_call(rig: LambdaRig) -> dict[str, Any]:
    assert len(rig.ecs.run_task_calls) == 1
    return rig.ecs.run_task_calls[0]


def container_environment(run_task_call: dict[str, Any]) -> dict[str, str]:
    overrides = run_task_call["overrides"]["containerOverrides"]
    assert len(overrides) == 1
    environment = overrides[0]["environment"]
    return {item["name"]: item["value"] for item in environment}


def assert_lambda_lock_was_written_and_removed(rig: LambdaRig) -> None:
    assert {
        (call["key"], call["literal_str"])
        for call in rig.control_bucket.calls["put"]
    } >= {(names.lock_key("lambda"), True)}
    assert names.lock_key("lambda") not in rig.control_bucket.objects
    assert {call["key"] for call in rig.control_bucket.calls["rm"]} == {
        names.lock_key("lambda")
    }


def test_lambda_launches_validation_task_and_cleans_lambda_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = make_lambda_rig(monkeypatch)

    response = response_from_lambda(lambda_core.main(event(), object()))

    assert response == {"status": "success"}
    assert_lambda_lock_was_written_and_removed(rig)
    assert rig.control_bucket.read(names.lock_key("client")) == AGENT_ID

    run_task_call = only_run_task_call(rig)
    assert run_task_call["cluster"] == DEFAULT_TASK_CONFIG["cluster"]
    assert run_task_call["taskDefinition"] == DEFAULT_TASK_CONFIG["family"]
    assert run_task_call["launchType"] == "FARGATE"
    assert run_task_call["networkConfiguration"] == {
        "awsvpcConfiguration": {
            "subnets": [DEFAULT_TASK_CONFIG["subnet_id"]],
            "securityGroups": [DEFAULT_TASK_CONFIG["sg_id"]],
            "assignPublicIp": "ENABLED",
        }
    }
    assert {"key": "Project", "value": "MAST FAST"} in run_task_call["tags"]
    assert {
        "key": "Name",
        "value": names.validation_task(DATASET, DELIVERY_ID),
    } in run_task_call["tags"]

    task_kwargs = lambda_core.load_kwargs(
        container_environment(run_task_call)["KWARGBLOB"]
    )
    assert task_kwargs == {
        "dataset": DATASET,
        "delivery_id": DELIVERY_ID,
        "transfer_type": TRANSFER_TYPE,
    }


@pytest.mark.parametrize(
    "logs",
    [
        ["encountered unhandled exception: startup failed"],
        [
            "Initializing pipeline for tests",
            "encountered unhandled exception: startup failed",
        ],
    ],
    ids=["exception-only", "exception-and-initialization"],
)
def test_lambda_reports_pipeline_startup_exception(
    monkeypatch: pytest.MonkeyPatch,
    logs: list[str],
) -> None:
    rig = make_lambda_rig(monkeypatch)
    log_polls: list[None] = []

    class RecordingECSTask(FakeECSTask):
        def __init__(self, task: dict[str, Any]) -> None:
            super().__init__(task, logs)

        def get_logs(self) -> list[str]:
            log_polls.append(None)
            return super().get_logs()

    monkeypatch.setattr(lambda_core, "ECSTask", RecordingECSTask)
    time_calls: list[None] = []

    def start_time() -> float:
        if time_calls:
            pytest.fail("startup failure should return before checking timeout")
        time_calls.append(None)
        return 0

    monkeypatch.setattr(lambda_core.time, "time", start_time)

    response = lambda_core.main(event(), object())

    assert response == "pipeline application failed to start"
    assert log_polls == [None]
    assert time_calls == [None]
    assert_lambda_lock_was_written_and_removed(rig)


@pytest.mark.parametrize(
    ("case", "rig_kwargs", "expected_step"),
    [
        (
            "missing task config",
            {"default_config": None},
            "task config read",
        ),
        (
            "duplicate validation task",
            {
                "running_tasks": [
                    {"name": names.validation_task(DATASET, DELIVERY_ID)}
                ]
            },
            "validation task check",
        ),
        (
            "wrong client lock owner",
            {"client_lock": "some-other-client"},
            "lockfile check",
        ),
    ],
)
def test_lambda_cleans_lambda_lock_after_post_lock_failures(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    rig_kwargs: dict[str, Any],
    expected_step: str,
) -> None:
    rig = make_lambda_rig(monkeypatch, **rig_kwargs)

    response = response_from_lambda(lambda_core.main(event(), object()))

    assert response["status"] == "error", case
    assert response["step"] == expected_step, case
    assert_lambda_lock_was_written_and_removed(rig)


def test_lambda_refuses_to_launch_when_validation_task_is_already_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = make_lambda_rig(
        monkeypatch,
        running_tasks=[
            {
                "name": names.validation_task(DATASET, DELIVERY_ID),
                "lastStatus": "RUNNING",
            }
        ],
    )

    response = response_from_lambda(lambda_core.main(event(), object()))

    assert response == {
        "status": "error",
        "step": "validation task check",
        "details": "a validation task is already running for this transfer",
    }
    assert rig.ecs.run_task_calls == []
    assert_lambda_lock_was_written_and_removed(rig)


def test_dataset_task_config_overrides_default_task_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = make_lambda_rig(
        monkeypatch,
        dataset_config={
            "cluster": "cluster-for-dataset",
            "memory": "1024",
        },
    )

    response = response_from_lambda(lambda_core.main(event(), object()))

    assert response == {"status": "success"}
    run_task_call = only_run_task_call(rig)
    assert run_task_call["cluster"] == "cluster-for-dataset"
    assert run_task_call["taskDefinition"] == DEFAULT_TASK_CONFIG["family"]
    assert run_task_call["networkConfiguration"]["awsvpcConfiguration"] == {
        "subnets": [DEFAULT_TASK_CONFIG["subnet_id"]],
        "securityGroups": [DEFAULT_TASK_CONFIG["sg_id"]],
        "assignPublicIp": "ENABLED",
    }
    assert_lambda_lock_was_written_and_removed(rig)
