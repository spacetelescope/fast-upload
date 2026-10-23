"""
Data types used throughout the mast-upload-tools which are not complicated
enough to need their own file.
"""
from dataclasses import dataclass
from typing import Literal, TypedDict, NotRequired

from pandas import DataFrame

from .labels import Label


YAMLString = str
"""alias for strings that can be parsed as valid YAML"""

TransferType = Literal["staging", "sample"]
"""Names of transfer categories"""

TransferEntity = Literal["validator", "client", "lambda"]
"""Named entities in transfer process"""


class TaskConfig(TypedDict):
    """
    Format of full validation task configuration. The default task
    configuration YAML object should contain all of these keys. Collection/
    provenance-specific objects may contain any subset.
    """
    az_id: str
    subnet_id: str
    # name of ecs cluster in which to run task
    cluster: str
    # security group in which to run task
    sg_id: str
    # ecs task family
    family: str


class ValPipeSettings(TypedDict):
    transfer_timeout: float
    missing_timeout: float
    loop_rate: float
    n_val_threads: int
    keepalive_threshold: float
    az_id: str
    staleness_threshold: NotRequired[int]


class ValIdent(TypedDict):
    confb_name: str
    cb_name: str
    tb_name: str
    collection: str
    provenance: str
    delivery_id: str
    transfer_type: TransferType
    agent_id: str
    label: Label
    index: DataFrame


class PipelineNetworkConfig(TypedDict):
    """
    AWS configuration settings necessary for executing various aspects of the
    pipeline but not for initial client auth. Components are expected to fetch
    them from Parameter Store.
    """

    AVAILABILITY_ZONE_ID: str
    BUCKET_STEM: str
    CONFIG_BUCKET: str
    INIT_LAMBDA_ARN: str
    LOCK_STALENESS_THRESHOLD: int
    TASK_CONFIG_PREFIX: str


ValidationStatus = Literal["success", "failure", "incomplete"]


class ValidationDetails(TypedDict):
    files_validated: int
    files_expected: int
    # note that this can include transfer failures that later succeed due to
    # e.g. transient network errors
    total_failures: int
    # filename: file error(s)
    errors: dict[str, str]


class ValidationSQSReport(TypedDict):
    """
    Format for SQS message sent by validation pipeline on exit
    (assuming successful init). Dumped to JSON in actual message.
    """

    collection: str
    provenance: str
    delivery_id: str
    completed_at: str
    transfer_type: TransferType
    label_path: str
    details: ValidationDetails
    validation_result: ValidationStatus
    pipeline_exception: str  # "None" if no exception


class PipelineLaunchParameters(TypedDict):
    """
    Information the validation pipeline expects to receive from the
    pipeline launch lambda on startup in normal operation.
    """
    collection: str
    provenance: str
    delivery_id: str
    transfer_type: TransferType


class LambdaEventParameters(TypedDict):
    """
    Information the pipeline launch lambda expects to receive from the upload
    client on invocation in normal operation.
    """
    collection: str
    provenance: str
    delivery_id: str
    transfer_type: TransferType
    agent_id: str


@dataclass
class CognitoConfiguration:
    domain: str
    client_id: str
    redirect_uri: str
    region: str
    user_pool_id: str
    identity_pool_id: str
