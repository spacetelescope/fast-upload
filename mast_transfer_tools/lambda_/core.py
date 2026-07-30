from __future__ import annotations

from base64 import b64encode, b64decode

import gzip

import json

import time

import random
from functools import partial
from pathlib import Path
from typing import Callable, TypedDict, Any

from botocore.exceptions import ClientError
from dustgoggles.structures import rmerge
from dustgoggles.dynamic import exc_report
from hostess.aws.ecs import ls_tasks, ECSTask
from hostess.aws.s3 import Bucket
from hostess.aws.utilities import init_client, make_boto_client
import yaml

import mast_transfer_tools.config as conf
from mast_transfer_tools.utilz.locks import check_lock, LockStatus
from mast_transfer_tools.errors import BucketLockedError, InvalidLockError
from mast_transfer_tools.lambda_._client_responses import (
    cbucket_err_response,
    conf_bucket_err_response,
    iconfig_err_response,
    tlock_err_response,
    noconfig_err_response,
    llock_err_response,
    lock_write_err_response,
    vtask_running_err_response,
    lambda_main_execution_error,
    task_run_err_response,
    pipeline_exec_success_msg,
)
from mast_transfer_tools.types import (
    TaskConfig,
    YAMLString,
    PipelineNetworkConfig,
    TransferType,
    LambdaEventParameters,
)
import mast_transfer_tools.utilz.name_reference as names


yload = partial(yaml.load, Loader=yaml.CLoader)


# NOTE: the TaskConfig typing here is a little sketchy. We expect to have a
# full TaskConfig _after_ merging the default task config and, if present, any
# dataset-specific task config, but individual task configs can contain any
# subset of those keys, which are interpreted as overrides.
def _load_tconfig(cbucket: Bucket, key: str) -> TaskConfig:
    return yload(cbucket.get(key).read().decode("utf-8"))


def read_task_config(
    event: LambdaEventParameters, netconf_params: PipelineNetworkConfig
) -> tuple[TaskConfig | None, str | None]:
    """
    Read a task configuration from the configuration bucket. If no special
    config exists for this dataset, just read the default config; otherwise,
    use anything in this dataset's config as overrides for the default config.

    Returns:
        config: TaskConfig if config(s) were found and parsed correctly; None
            otherwise
        exception_message: str if config(s) weren't found and parsed
            correctly, None otherwise
    """
    bucket_constructor = Bucket
    try:
        cbucket = bucket_constructor(netconf_params["CONFIG_BUCKET"])
        iconfigs: tuple[str] = cbucket.ls(
            f"{netconf_params['TASK_CONFIG_PREFIX']}/"
        )
    except Exception as ex:
        # the locations file is wrong, something is wrong with the lambda
        # function's permissions, etc.
        return None, conf_bucket_err_response(ex)
    configs = {}
    for k in ("default", event["dataset"]):
        objname = f"{k}-task-config.yaml"
        if (
            key := str(Path(netconf_params["TASK_CONFIG_PREFIX"], objname))
        ) in iconfigs:
            try:
                configs[k] = _load_tconfig(cbucket, key)
            except Exception as ex:
                # validation task config exists but is malformatted.
                # treat this as a failure: we don't know what to do.
                return None, iconfig_err_response(ex)
    if len(configs) == 0:
        # even the default validation task config is not present, so we don't
        # have any idea where to run things! hard error.
        return None, noconfig_err_response()
    elif len(configs) == 1:
        return tuple(configs.values())[0], None
    return rmerge(*configs.values()), None


class _CleanupDict(TypedDict):
    bucket: Bucket | None
    exid: int | None


def encode_kwargs(**kwargs: Any) -> str:
    """Compress and encode a kwarg blob for the validation client."""
    kwarg_json = json.dumps(kwargs)
    kwarg_gzip = gzip.compress(kwarg_json.encode("utf-8"))
    kwargblob = b64encode(kwarg_gzip)
    return kwargblob.decode("ascii")


def load_kwargs(kwargblob: str) -> LambdaEventParameters:
    """Load the encoded and compressed kwarg blob."""
    kwargstring = gzip.decompress(b64decode(kwargblob))
    return json.loads(kwargstring)


def run_validation_task(
    dataset: str,
    delivery_id: str,
    transfer_type: TransferType,
    tags: dict[str, str],
    config: TaskConfig,
) -> dict:
    """
    Run the validation task on Fargate.

    See boto3 docs for a more detailed definition of the return type:
    https://docs.aws.amazon.com/boto3/latest/reference/services/ecs/client/run_task.html
    """
    kwargblob = encode_kwargs(
        dataset=dataset, delivery_id=delivery_id, transfer_type=transfer_type
    )
    ecs = init_client("ecs")
    return ecs.run_task(
        cluster=config["cluster"],
        taskDefinition=config["family"],
        launchType="FARGATE",
        enableExecuteCommand=True,
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": [config["subnet_id"]],
                "securityGroups": [config["sg_id"]],
                "assignPublicIp": "ENABLED",
            }
        },
        tags=[
            *[{"key": k, "value": v} for k, v in tags.items()],
            {
                "key": "Name",
                "value": names.validation_task(dataset, delivery_id),
            },
        ],
        overrides={
            "containerOverrides": [
                {
                    "name": "main",
                    "environment": [
                        {"name": "KWARGBLOB", "value": kwargblob},
                    ],
                }
            ]
        },
    )


def lambda_cleanup(
    func: Callable[
        [
            LambdaEventParameters,
            object,
            PipelineNetworkConfig,
            dict[str, str],
            _CleanupDict,
        ],
        YAMLString,
    ]
) -> Callable[[LambdaEventParameters, object], YAMLString]:
    """
    Exists to avoid wrapping the whole body of main() in an ugly
    try-except-finally block. Basically `goto cleanup`
    """

    def with_lambda_cleanup(
        event: LambdaEventParameters, context: object
    ) -> YAMLString:
        cleanup_dict: _CleanupDict = {
            "bucket": None,
            "exid": None,
        }
        try:
            ssm = make_boto_client("ssm")

            netconf_response = ssm.get_parameter(
                Name=conf.NETWORK_CONFIG_PARAMETER, WithDecryption=True
            )
            netconf_params: PipelineNetworkConfig = json.loads(
                netconf_response["Parameter"]["Value"]
            )
            dataset, delivery_id, bucket_name, cbucket_name = unpack_locations(
                event, netconf_params
            )
            tags_response = ssm.get_parameter(
                Name=conf.RESOURCE_TAG_PARAMETER, WithDecryption=True
            )
            tags = json.loads(tags_response["Parameter"]["Value"])
            cleanup_dict["bucket"] = Bucket(cbucket_name)
            print(f"{event} w/ {cleanup_dict['bucket']}")
            result = func(event, context, netconf_params, tags, cleanup_dict)
        except Exception as ex:
            print(f"{exc_report(ex)}\n")
            result = lambda_main_execution_error(ex)
        if cleanup_dict["bucket"] is not None:
            cleanup_dict["bucket"].rm(names.lock_key("lambda"))
        print(f"returning with message \n\n{result}\n\n")
        return result

    return with_lambda_cleanup


def unpack_locations(
    event: LambdaEventParameters, netconf: PipelineNetworkConfig
) -> tuple[str, str, str, str]:
    dataset, delivery_id, ttype = (
        event[n] for n in ("dataset", "delivery_id", "transfer_type")
    )
    tbucket_name = names.transfer_bucket(
        netconf["BUCKET_STEM"], dataset, delivery_id, ttype
    )
    cbucket_name = names.control_bucket(
        netconf["BUCKET_STEM"],
        dataset,
        delivery_id,
        netconf["AVAILABILITY_ZONE_ID"],
    )
    return dataset, delivery_id, tbucket_name, cbucket_name


@lambda_cleanup
def main(
    event: LambdaEventParameters,
    # NOTE: in real operation, _context is a LambdaContext object as defined in
    # aws-lambda-python-runtime-interface-client. We don't actually use it
    # here, so we don't bother typing it in a more robust way.
    _context: object,
    netconf: PipelineNetworkConfig,
    tags: dict[str, str],
    cleanup_dict: _CleanupDict,
) -> YAMLString:
    """Entrypoint for upload init lambda."""
    dataset, delivery_id, tbucket_name, cbucket_name = unpack_locations(
        event, netconf
    )
    bucket_constructor = Bucket
    exid = random.randint(0, 1000000000)
    print(f"executing upload init lambda with parameters {event}\n")
    try:
        print(f"checking access to {cbucket_name}\n")
        cbucket = bucket_constructor(cbucket_name)
        cbucket.ls()
    except Exception as ex:
        # We can't list -- or maybe even see -- the transfer bucket.
        # Perhaps it doesn't even exist! This is a hard failure condition.
        print(f"{exc_report(ex)}\n")
        return cbucket_err_response(ex)
    llock_status = check_lock(
        cbucket,
        "lambda",
        staleness_threshold=netconf["LOCK_STALENESS_THRESHOLD"],
    )
    if llock_status == LockStatus.LOCKED:
        print(f"failed to acquire lock (status {llock_status}, bailing out\n")
        return llock_err_response(BucketLockedError())
    try:
        cbucket.put(str(exid), names.lock_key("lambda"), literal_str=True)
    except ClientError as ce:
        # this likely indicates a lambda function permissions
        # configuration error that must be addressed on the backend
        print(f"Could not write lock file ({ce})")
        return lock_write_err_response()
    # for deleting the lock file and such
    cleanup_dict["bucket"], cleanup_dict["exid"] = cbucket, exid
    tconfig, tconfig_err = read_task_config(event, netconf)
    print(f"using task config {tconfig}")
    if tconfig_err is not None:
        return tconfig_err
    vtask_name = names.validation_task(dataset, delivery_id)
    running_tasks = ls_tasks(cluster=tconfig["cluster"], name=vtask_name, status="RUNNING")
    if len(running_tasks) > 0:
        # This most likely indicates a duplicate execution. It could also
        # indicate a task that failed to stop when done.
        print("task is already running, bailing out. running task list:")
        print(running_tasks)
        return vtask_running_err_response()
    try:
        client_lock_status = check_lock(
            cbucket,
            "client",
            event["agent_id"],
            netconf["LOCK_STALENESS_THRESHOLD"],
        )
        if client_lock_status != LockStatus.HELD:
            raise InvalidLockError(
                f"Lock is not held by client with agent_id {event['agent_id']}"
            )
    except Exception as ex:
        print(f"{exc_report(ex)}\n")
        return tlock_err_response(ex)
    try:
        print("running task\n")
        run_validation_task(
            dataset, delivery_id, event["transfer_type"], tags, tconfig
        )
        task = ECSTask(ls_tasks(cluster=tconfig["cluster"], name=vtask_name)[0])
        print(f"found task '{task['task']}' with status '{task['status']}'")
        task.wait_while_pending(timeout=80)
        print("task is running\n")
    except Exception as ex:
        # this failure probably indicates that the task never started, or if
        # it did, never transitioned to the RUNNING state -- there's some
        # AWS-level issue like permissions, cluster config, etc. it's also
        # _possible_ that it entered RUNNING for a very brief time, so brief
        # we didn't see it between polls. This might happen if the entrypoint
        # command immediately fails, is a successful no-op, etc.
        print(f"{exc_report(ex)}\n")
        return task_run_err_response(ex)
    pipeline_timeout = 60
    start = time.time()
    # NOTE: this is not intended to be a full-on supervisor and is not
    #  responsible for the pipeline's behavior _after_ startup. But if the
    #  pipeline fails to start _at all_, we need to tell the client about it.
    #  The pipeline likely wasn't able to write to the S3 log object and the
    #  client has no access to Cloudwatch or ECS to know it failed.
    print("waiting on task\n")
    while True:
        logs = task.get_logs()
        if len(logs) > 0 and any(
            l.startswith("Initializing pipeline") for l in logs
        ):
            break
        if time.time() - start > pipeline_timeout:
            print("pipeline application failed to start (timeout)\n")
            return "pipeline application failed to start"
        time.sleep(0.1)
    print("pipeline is running, exiting successfully\n")
    return pipeline_exec_success_msg()
