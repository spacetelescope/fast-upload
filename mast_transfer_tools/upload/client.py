from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import datetime as dt
import json
from io import BytesIO
from pathlib import Path
import random
import re
from string import ascii_lowercase
import time
from typing import Literal, Any

import botocore.config
import botocore.session
import pandas as pd
from botocore.exceptions import ClientError
from dustgoggles.dynamic import exc_report
from hostess.aws.s3 import Bucket
from rich.console import Console
from rich.padding import Padding

import mast_transfer_tools.utilz.name_reference as names
from mast_transfer_tools.s3log.helpers import LOG_FIELD_SPEC
from mast_transfer_tools.upload.cognito import (
    get_authenticated_cognito_manager
)
import mast_transfer_tools.config as conf
from mast_transfer_tools.types import TransferType, PipelineNetworkConfig
from mast_transfer_tools.errors import BucketLockedError
from mast_transfer_tools.s3log.s3tsvreader import S3TSVReader
from mast_transfer_tools.s3log.s3tsvwriter import S3TSVWriter


@dataclass
class ClientFile:
    """Representation of a file potentially intended for transfer."""
    abspath: str
    relpath: str
    state: Literal[
        "ready",
        "pending",
        "failed",
        "transferring",
        "done",
        "valid",
        "invalid"
    ]
    """
    Definitions of values:
        ready: file has not yet been transferred to S3 or queued for transfer,
            but we plan to transfer it.
        pending: file is queued for transfer to S3.
        failed: file failed to transfer to S3 due to network or other error.
            This state does not indicate validation failure: a 'failed' file
            is not available to the validator at all. If retries < max_retries,
            the file is eligible for another attempt at transfer.
        transferring: file is currently transferring to S3.
        done: file has been transferred to S3 but not yet validated.
        valid: file has been marked as present and valid by the validator.
        invalid: file has been marked as missing/invalid by the validator,
            or the validation process appears to be irregular/invalid (e.g.,
            the validator reports it valid despite us not transferring it).
    """

    retries: int
    """How many times have we attempted to retry a failed transfer"""

    max_retries: int
    """How many times may we retry transfer"""

    message: str | None = None
    """Details, if any, about failure / invalidity / etc."""

    checksum: str | None = None
    """CRC32 bytes, base64-encoded, for example 'yH2GHg==', if provided"""

    @property
    def can_transfer(self) -> bool:
        return (
            self.state == "ready"
            or (self.state == "failed" and self.retries < self.max_retries)
        )
    """Is it ok to queue this file for transfer?"""

    @property
    def no_retry(self) -> bool:
        return self.state == "failed" and self.retries >= self.max_retries
    """Does this file appear to be impossible to transfer?"""

    @property
    def is_complete(self) -> bool:
        return self.state in ("invalid", "valid") or self.no_retry
    """Is there nothing left to do with this file at all?"""


class DataRoot:
    """
    Compatibility layer for S3 -> S3 and local -> S3 operations.
    """

    def __init__(self, source: Path | Bucket) -> None:
        self.source = source
        if not isinstance(self.source, (Bucket, Path)):
            raise TypeError(f"base must be Bucket or Path, got {type(source)}")
        self.is_local = isinstance(self.source, Path)
        self.name = str(source) if self.is_local else source.name

    def cp(self, source: ClientFile, destination_bucket: Bucket) -> str | None:
        """Upload a file from local to S3, or initiate S3-to-S3 object copy."""

        # NOTE: there is no direct mechanism for asserting a checksum
        # during a copy-object operation -- S3 already has a checksum for the
        # object or it doesn't.  So the upload client doesn't do anything with
        # that information in the bucket-to-bucket copy case.
        if self.is_local is False:
            return self.source.cp(
                source.abspath, source.relpath, destination_bucket.name
            )
        return destination_bucket.put(
            source.abspath, source.relpath, checksum=source.checksum
        )

    def ls(self, key: str | Path | None = None) -> tuple[str, ...]:
        """List the contents of a directory or prefix."""

        if self.is_local is False:
            return self.source.ls() if key is None else self.source.ls(key)
        key = "" if key is None else key
        return tuple(str(f) for f in (self.source / Path(key)).iterdir())

    name: str
    is_local: bool
    source: Bucket | Path


class NothingToDoError(ValueError):
    """all files already validated."""


class UploadClientError(ClientError):
    """
    alias for botocore ClientError, which does a bunch of complicated stuff
    with running botocore operations when initialized, so is very inconvenient
    to just spuriously instantiate
    """

    def __init__(self, *args: Any) -> None:
        Exception.__init__(self, *args)


# color scheme for mock client printing
CCOLORS = {
    "error": "red1",
    "warning": "salmon1",
    "info": "white",
    "complete": "steel_blue1",
    "success": "chartreuse3",
}


class UploadClient:
    """
    Manager class for data provider-side portions of file transfer and
    validation pipeline. Calls lambda to start the validation pipeline, talks
    to the validation pipeline via S3, uploads / transfers files, informs user
    about things.
    """
    def __init__(
        self,
        *,
        collection: str,
        provenance: str,
        delivery_id: str,
        file_index: pd.DataFrame,
        transfer_type: TransferType,
        source: str | Path | Bucket,
        lambda_client_config: dict | None = None,
        own_agent_id: str | None = None,
        n_threads: int = 1,
        debug: bool = False,
        max_retries: int = 2,
    ) -> None:
        if own_agent_id is None:
            self.own_agent_id = "".join(
                random.choices(ascii_lowercase, k=11)
            )
        else:
            self.own_agent_id = own_agent_id
        self.max_retries = max_retries
        self.debug = debug
        self.console = Console()
        self.cognito_manager = None
        self.file_index = file_index
        self.state: Literal[
            "initializing",
            "unconnected",
            "ready",
            "running",
            "pending_validation",
            "complete",
            "crashed",
            "quit"
        ] = "initializing"
        self.locked = False
        self.authenticated = False
        self.n_threads = n_threads
        self.index_key = names.index_key(
            collection, provenance, delivery_id, transfer_type
        )
        self.netconf_params = None
        self.session = None
        self.n_failures = 0
        self.lock_key = names.lock_key("client")
        if lambda_client_config is None:
            self.lambda_client_config = botocore.config.Config()
        else:
            self.lambda_client_config = lambda_client_config
        if isinstance(source, str) and source.startswith("s3://"):
            self.dataroot = None
            self.source_bucket_name = source.removeprefix("s3://")
        elif isinstance(source, Bucket):
            self.dataroot = None
            self.source_bucket_name = source.name
        elif isinstance(source, (Path, str)):
            self.dataroot = DataRoot(Path(source))
            self.source_bucket_name = None
        else:
            raise TypeError("Source must be a path or a bucket")
        self.file_list: list[ClientFile] = []
        for _, row in file_index.iterrows():
            cf = ClientFile(
                abspath=self._abspath(row['path']),
                relpath=row['path'],
                state="ready",
                retries=0,
                max_retries=max_retries
            )
            if 'checksum' in file_index.columns:
                cf.checksum = row['checksum']
            self.file_list.append(cf)
        try:
            try:
                if self.dataroot is not None:
                    self.dataroot.ls()
            except (ClientError, FileNotFoundError) as e:
                raise UploadClientError(
                    f"Cannot list contents of passed source "
                    f"{self.dataroot.source}. Unable to initialize client."
                ) from e
            self.identifiers = {
                "cb_name": None,
                "tb_name": None,
                "source": (
                    self.dataroot.name
                    if self.dataroot is not None
                    else self.source_bucket_name
                ),
                "collection": collection,
                "provenance": provenance,
                "delivery_id": delivery_id,
                "transfer_type": transfer_type,
                "agent_id": own_agent_id,
            }
            self.futures = []
        except BaseException as e:
            self.crash(e)
        self.init_lambda_arn = None
        self.control_bucket = None
        self.transfer_bucket = None
        self.state = "unconnected"

    def cognito_authenticate(self) -> None:
        """Retrieve tokens/creds from Cognito for AWS operations."""
        self.cmessage("attempting cognito authentication", "info")
        cmanager = get_authenticated_cognito_manager(cogconf=conf.COGCONFIG)
        role_suffix_for_delivery = (
            f"{names.identity_slug(self.identifiers['collection'], self.identifiers['provenance'])}-{self.identifiers['delivery_id']}"
            f"-role"
        )
        cmanager.get_credentials(role_suffix=role_suffix_for_delivery)
        csession = cmanager.make_refreshing_session()
        self.cognito_manager = cmanager
        self.session = csession
        self.cmessage("cognito authentication succeeded", "success")

    def _populate_config(self) -> None:
        if self.session is None:
            raise ValueError(
                "Do not call this method without an authenticated session"
            )
        ssm = self.session.client("ssm")
        response = ssm.get_parameter(
            Name=conf.NETWORK_CONFIG_PARAMETER,
            WithDecryption=True
        )
        self.netconf_params: PipelineNetworkConfig = json.loads(
            response['Parameter']['Value']
        )
        self.init_lambda_arn = self.netconf_params['INIT_LAMBDA_ARN']
        self.identifiers['cb_name'] = names.control_bucket(
            self.netconf_params['BUCKET_STEM'],
            self.identifiers['collection'],
            self.identifiers['provenance'],
            self.identifiers['delivery_id'],
            self.netconf_params['AVAILABILITY_ZONE_ID'],
        )
        self.identifiers['tb_name'] = names.transfer_bucket(
            self.netconf_params['BUCKET_STEM'],
            self.identifiers['collection'],
            self.identifiers['provenance'],
            self.identifiers['delivery_id'],
            self.identifiers['transfer_type']
        )

    def _connect(self) -> None:
        """
        Initialize networked functionality and check online resource state
        validity. Should only be called from UploadClient.connect().
        """
        if self.session is None:
            self.cognito_authenticate()
        self._populate_config()
        self.control_bucket = Bucket(
            self.identifiers["cb_name"], session=self.session
        )
        self.transfer_bucket = Bucket(
            self.identifiers["tb_name"], session=self.session
        )
        if self.source_bucket_name is not None:
            self.dataroot = DataRoot(
                Bucket(self.source_bucket_name, session=self.session)
            )
        try:
            # we don't need to acquire a lock for a read-only operation.
            val_log = self.control_bucket.get(
                names.log_key(
                    self.identifiers["transfer_type"], 'validator'
                )
            )
            val_df = pd.read_csv(
                val_log,
                sep="\t",
                header=None,
                names=[f["name"] for f in LOG_FIELD_SPEC]
            )
            val_rows = val_df.loc[val_df['category'] == 'validation']
            val_groups = val_rows.groupby('ref')
            if len(val_groups) > 0:
                # could be enormous, don't want to make the call frivolously.
                # if it _is_ 0 here, that suggests that the bucket got dumped
                # after a previous upload -- possibly because provider
                # determined files were invalid and requested it, etc.
                tr_df = self.transfer_bucket.df()
                if len(tr_df) > 0:
                    # note: mutates elements of self.file_list inplace
                    self._check_for_validated_files(tr_df, val_groups)
        except ClientError as ce:
            # if the log simply isn't there, it's fine -- this may _not_ be
            # a restarted or corrected upload. anything else indicates a
            # problem, probably one related to accessing the control bucket.
            # time to bug out.
            if "Not Found" not in str(ce) and "does not exist" not in str(ce):
                raise ce
        try:
            self.acquire_lock()
        except BaseException as exc:
            self.cmessage("unable to acquire lock", "error")
            raise exc

        self.logger = S3TSVWriter(
            self.control_bucket,
            names.log_key(self.identifiers["transfer_type"], "client"),
            fixed={"agent_id": self.own_agent_id},
            fields=LOG_FIELD_SPEC,
        )
        self.transfer_exc = ThreadPoolExecutor(self.n_threads)
        self.reader = S3TSVReader(
            bucket=self.control_bucket,
            key=names.log_key(self.identifiers["transfer_type"], "validator"),
            fields=LOG_FIELD_SPEC,
        )
        self.reader.start()
        self.state = "ready"

    def _check_for_validated_files(
        self,
        tr_df: pd.DataFrame,
        val_groups: pd.core.groupby.DataFrameGroupBy
    ) -> None:
        """
        Check for already-validated files by looking at existing logs
        (if any) in order to help gracefully resume interrupted transfers.
        This function is strictly a helper for _connect() and should not be
        called directly. It mutates elements of self.file_list() to mark
        them validated and also informs the user about already-validated file
        status (if any).
        """
        relpath_map = {f.relpath: f for f in self.file_list}
        n_valid, n_invalid, n_missing = 0, 0, 0
        for relpath, group in val_groups:
            if (file := relpath_map.get(relpath)) is None:
                continue
            last_status = group.iloc[-1]['status']
            # noinspection PyUnboundLocalVariable
            if last_status == 'ok' and (relpath == tr_df['Key']).any():
                file.state = 'valid'
                n_valid += 1
            elif last_status == "ok":
                n_missing += 1
            else:
                # we do _not_ want to mark the file as invalid. the
                # ideal case here is that this is a reupload of a
                # corrected file. Reuploading validated-but-incorrect
                # files should be done in conversation with the archive.
                n_invalid += 1
        if n_valid == len(self.file_list):
            self.cmessage("all files valid; nothing to do", "success")
            raise NothingToDoError()
        if n_valid > 0:
            self.cmessage(
                f"{n_valid} files in file list have been previously "
                f"marked as valid and are present in bucket. Not "
                f"reuploading those files.",
                "info"
            )
        if n_invalid > 0:
            self.cmessage(
                f"{n_invalid} files in file list have been previously "
                f"marked as invalid. Reuploading those files.",
                "info"
            )
        if n_missing > 0:
            self.cmessage(
                f"{n_missing} files in file list have been previously "
                f"marked as valid but are not present in bucket. Reuploading "
                f"those files.",
                "info"
            )

    def connect(self) -> None:
        if self.state != "unconnected":
            self.cmessage(
                f"refusing to connect from client state {self.state}",
                "warning"
            )
            return None
        self.cmessage("initializing AWS connection", "info")
        try:
            self._connect()
        except NothingToDoError:
            return self.quit(None)
        except Exception as ex:
            return self.crash(ex)
        self.cmessage(
            "AWS connection established, ready to initiate transfer", "success"
        )
        return None

    def crash(self, exception: BaseException) -> None:
        """
        Call if client has entered an invalid state ('crashed'). Logs the
        crash to S3 if possible, informs the user about the crash, and quits.
        """
        if self.state in ("crashed", "quit"):
            return
        self.state = "crashed"
        if hasattr(self, "logger"):
            self._log_crash(exception)
        self.cmessage(
            f"crashing with fatal error {self.excformat(exception)}", "error"
        )
        self.quit()

    def quit(self, _exception: BaseException | None = None) -> None:
        """
        Call if client has entered a stopped state, finished or not.
        Logs the exit to S3 (if possible), stops all polling resources,
        releases lock if relevant, and marks state as quit. Note that the
        object should not be restarted. Construct a new UploadClient
        if you wish to fully 'reboot' within a single interpreter session.
        """
        if self.state == "quit":
            return
        self.state = "quit"
        self._log_quit()
        if hasattr(self, "logger"):
            time.sleep(1)
            self.logger.stop()
        self.dump_state()
        if hasattr(self, "reader"):
            self.reader.stop()
        if hasattr(self, "logger"):
            pass
        if hasattr(self, "transfer_exc"):
            self.transfer_exc.shutdown(cancel_futures=True)
        self.release_lock()

    def dump_state(self) -> None:
        """
        Dump transfer/validation state to working directory as a CSV file.
        """
        tstr = re.sub("[:-]", "_", dt.datetime.now(dt.UTC).isoformat()[:19])
        recs = [
            {'file': f.abspath, 'state': f.state, 'message': f.message}
            for f in self.file_list
        ]
        pd.DataFrame(recs).to_csv(f"fast_client_log_{tstr}.csv")

    def acquire_lock(self, *, refresh: bool = False) -> bool:
        """
        Attempt to acquire transfer lock ('owned' object in control bucket).
        Raises an exception if lock cannot be acquired. If refresh is True,
        write the lock file even if already held.
        """
        from mast_transfer_tools.utilz.locks import check_lock, LockStatus

        lock_status = check_lock(
            self.control_bucket,
            "client",
            self.own_agent_id,
            staleness_threshold=self.netconf_params.get(
                 "LOCK_STALENESS_THRESHOLD", 3600
            )
        )
        if lock_status in (
            LockStatus.UNLOCKED, LockStatus.INVALID, LockStatus.STALE
        ):
            do_write = True
        elif lock_status == LockStatus.HELD and refresh is True:
            do_write = True
        elif lock_status == LockStatus.HELD:
            do_write = False
        else:
            raise BucketLockedError(f"status {lock_status}")
        if do_write:
            self.control_bucket.put(
                self.own_agent_id, self.lock_key, literal_str=True
            )
            self.last_lock_timestamp = time.time()
        self.locked = True
        return True

    def release_lock(self) -> None:
        """
        Release the transfer lock. Should generally only be called from
        self.quit(); a running client should always hold the lock.
        """
        if self.control_bucket is None:
            return

        from mast_transfer_tools.utilz.locks import check_lock, LockStatus

        lock_status = check_lock(self.control_bucket, "client", self.own_agent_id,
                                 staleness_threshold=self.netconf_params.get(
                                     "LOCK_STALENESS_THRESHOLD", 3600
                                 ))
        if lock_status == LockStatus.HELD:
            self.control_bucket.rm(self.lock_key)
        self.locked = False

    def write_index(self) -> None:
        if self.state != "ready":
            self.cmessage(
                f"can only write index from ready state (current state is "
                f"{self.state})",
                "error"
            )
            return
        self.cmessage("writing file index", "info")
        index = self.file_index.copy()
        index["will_transfer"] = [f.can_transfer for f in self.file_list]
        buf = BytesIO()
        index.to_csv(buf, mode="wb", index=None)
        buf.seek(0)
        self.control_bucket.put(buf, self.index_key)
        self.cmessage("wrote file index", "success")

    def _log_crash(self, exception: BaseException) -> None:
        """
        Log the fact that we are about to quit due to an unhandled exception
        or otherwise entering an invalid state
        """
        # guard present because we may reasonably call this during failed
        # initialization
        if hasattr(self, "logger"):
            self.logger.write(
                category="client",
                ref="self",
                status="error",
                message=str(exc_report(exception)).replace("\n", " ; "),
            )

    def _abspath(self, key: str | Path) -> str:
        """Absolute path of a file/key relative to data root"""
        if self.dataroot is None:
            return str(key)
        if not self.dataroot.is_local:
            return str(key)
        return str(self.dataroot.source / Path(key))

    def _log_transfer(self, file: ClientFile) -> None:
        """Log the fact (to user and S3) that we have transferred a file."""
        self.cmessage(f"successfully transferred {file.abspath}", "complete")
        self.logger.write(
            category="transfer", ref=file.relpath, status="ok"
        )

    def _log_quit(self) -> None:
        """
        Inform the user that we are quitting, and, if possible, log this
        fact to S3.
        """
        if self.validation_complete:
            if not all(f.state == "valid" for f in self.file_list):
                n_valid = len(self.file_list) - len(
                    [f for f in self.file_list if f.state != "valid"]
                )
                self.cmessage(
                    f"validation run complete; quitting. Only "
                    f"{n_valid}/{len(self.file_list)} files were found valid. "
                    f"Check logs for details",
                    "error"
                )
            else:
                self.cmessage(
                    "validation run complete; quitting. All files "
                    "successfully validated.",
                    "success"
                )
        elif self.transfer_complete:
            self.cmessage(
                "All files transferred, but quitting before validation "
                "complete.",
                "warning"
            )
        else:
            self.cmessage("Quitting prior to transfer completion.", "warning")
        if hasattr(self, "logger"):
            self.logger.write(category="stop", ref="self", status="ok")

    def _log_failed_transfer(
        self, key: ClientFile, exc: BaseException | None = None
    ) -> None:
        """
        Log the fact (to user and S3) that we have failed to transfer a file.
        """
        if exc is not None:
            report = exc_report(exc)
            summary = str(report["exception"])
            exc = str(report)
        else:
            exc = ""
            summary = ""
        self.cmessage(f"failed to transfer {key.abspath}: {summary}", "warning")
        self.logger.write(
            category="transfer",
            ref=key.relpath,
            status="error",
            message=exc,
        )

    def _transfer_file(self, file: ClientFile) -> None:
        """
        Initiate a file transfer. In normal operation, should only be
        submitted for asynchronous execution by transfer_next_file().
        """
        file.state = "transferring"
        self.cmessage(f"transferring {file.abspath}", "info")
        try:
            self.dataroot.cp(file, self.transfer_bucket)
            # this race condition is extremely unlikely but why risk it
            if file.state not in ("valid", "invalid"):
                file.state = "done"
            if not self.logger.stopped:
                # if it's stopped, we quit in the middle of a transfer, or
                # the logger's _just_ entered an invalid state. it's
                # fine that we completed the transfer to clean up unfinished
                # multipart uploads, etc., but it's not "really" successful in
                # the sense that it will not be validated or we won't know if
                # it is. this will only happen if something else has gone
                # wrong.
                self._log_transfer(file)
        except Exception as e:
            file.state = "failed"
            file.retries += 1
            self._log_failed_transfer(file, e)

    @property
    def next_file(self) -> ClientFile | None:
        """Next file available for transfer."""
        for f in self.file_list:
            if f.can_transfer:
                return f
        return None

    def transfer_next_file(self) -> None | ClientFile:
        """
        If possible, queue the next file for transfer. If there are no more
        files to transfer, print a warning and do nothing. If the client has
        crashed or is not yet ready/connected, print an error and raise an
        exception.
        """
        if self.transfer_complete:
            self.cmessage("no more files to transfer", "warning")
            return None
        if self.state == "crashed":
            self.cmessage("cannot transfer files when crashed", "error")
            raise ValueError("cannot transfer files when crashed")
        if self.next_file is None:
            return None
        if self.state not in ("ready", "running"):
            self.cmessage(
                "attempting to transfer file before validation app launch",
                "error",
            )
            raise ValueError(
                "Must launch validation app before transferring files"
            )
        if self.state == "ready":
            self.logger.write(
                category="init",
                ref="self",
                status="ok",
                message="transfer application started",
            )
            self.state = "running"
        file = self.next_file
        file.state = "pending"
        f = self.transfer_exc.submit(self._transfer_file, file)
        self.futures.append(f)
        return file

    @property
    def transfer_complete(self) -> bool:
        """
        Are we totally done with our transfer process (but possibly still
        awaiting validation)?
        """
        return self.next_file is None and all(f.done() for f in self.futures)

    @property
    def validation_complete(self) -> bool:
        """
        Do we have nothing left to do at all -- i.e., all files have either
        successfully transferred and undergone validation or failed to
        transfer after max retries?
        """
        return all(f.is_complete for f in self.file_list)

    @property
    def done(self) -> bool:
        """
        Have we either finished our work or (possibly prematurely)
        otherwise entered a terminal state?
        """
        return self.validation_complete or self.state in ("crashed", "quit")

    @property
    def n_complete(self) -> int:
        """
        How many files have either been transferred and undergone validation
        or entirely failed to transfer after max retries?
        """
        return len([f for f in self.file_list if f.is_complete])

    # TODO: prevent duplicate calls
    def initiate_transfer(self) -> None:
        """Attempt to start the validation pipeline."""
        lambda_parameters = {
            "collection": self.identifiers["collection"],
            "provenance": self.identifiers["provenance"],
            "delivery_id": self.identifiers["delivery_id"],
            "transfer_type": self.identifiers["transfer_type"],
            "agent_id": self.own_agent_id,
            "idempotency_key": random.randint(10000, 99999),
        }
        try:
            # noinspection PyUnresolvedReferences
            lamb = self.session.client(
                "lambda", config=self.lambda_client_config
            )
            self.cmessage(
                "attempting to start validation pipeline (this may take a few "
                "minutes)",
                "info"
            )
            result = lamb.invoke(
                FunctionName=self.init_lambda_arn,
                Payload=json.dumps(lambda_parameters).encode("utf-8"),
            )
        except BaseException as exc:
            return self.crash(exc)
        response = result['Payload'].read().decode('utf-8')
        if 'success' not in response:
            self.reader.update()
            errors = self.last_log.loc[self.last_log['status'] == 'failure']
            if len(errors) > 0:
                self.cmessage("Startup errors reported by pipeline:", "error")
                for _, e in errors.iterrows():
                    self.cmessage(f"    {e['message']}", "error")
            else:
                self.cmessage(
                    "Pipeline never initialized log, or failed to "
                    "specify errors", "error"
                )
            return self.crash(
                OSError(f"validation pipeline failed to start: {response}")
            )
        self.state = "ready"
        self.cmessage(
            "validation pipeline successfully started, ready to transfer",
            "success"
        )
        return None

    def excformat(self, exc: BaseException) -> str:
        """Format an exception for printing / logging."""
        if self.debug is True:
            return exc_report(exc)
        return f"{type(exc)}: {exc}"

    def _colorprint(self, text: str, color: str) -> None:
        """Print a message in a particular color."""
        return self.cprint(f"[{color}]{text}[/{color}]")

    def cmessage(
        self,
        text: str,
        mtype: Literal["warning", "error", "info", "complete", "success"],
    ) -> None:
        """Print a message in a predefined message-category color."""
        return self._colorprint(text, CCOLORS[mtype])

    def cprint(self, renderable: str, *, padded: bool = True) -> None:
        """Print a message to our virtual console."""
        if padded:
            renderable = Padding(renderable)
        return self.console.print(renderable)

    def _update_files(self) -> None:
        """
        Check for messages from the validator retrieved from the S3 log
        object; update file statuses and inform user of results as relevant.
        Intended only as subroutine of update().
        """
        for _, m in self.last_log.loc[
            self.last_log['category'] == 'validation'
        ].iterrows():
            ref, status, message = m['ref'], m['status'], m["message"]
            found = False
            for f in self.file_list:
                if f.relpath != ref:
                    continue
                found = True
                if f.state in ("valid", "invalid"):
                    f.state = "invalid"
                    f.message = "improper double validation"
                    self.cmessage(
                        f"improper double validation on {f.abspath}", "error"
                    )
                    self.n_failures += 1
                    continue
                if f.state != "done":
                    f.state = "invalid"
                    f.message = (
                        "validator reported success before transfer complete"
                    )
                    self.cmessage(
                        f"validator reported success before transfer complete "
                        f"for {f.abspath}",
                        "error"
                    )
                    self.n_failures += 1
                    continue
                if status == "ok":
                    f.state = "valid"
                    self.cmessage(
                        f"{f.abspath} succcessfully validated", "success"
                    )
                    continue
                f.state = "invalid"
                self.n_failures += 1
                f.message = message
                self.cmessage(
                    f"{f.abspath} failed validation: {message}", "error"
                )
            if found is False:
                self.cmessage(
                    f"Validation pipeline returned information for {ref}, "
                    f"which is not in file list",
                    "warning"
                )

    def update(self) -> bool:
        """
        Update the state of the client. Returns True if the update itself
        indicates that the client should stop, False otherwise.

        Prints a warning and does nothing if the client is not running or
        has crashed.

        Otherwise, calls self.reader.update() and acts on any new information
        found in the log. If self.reader.update() fails, crashes the client.
        Updates any files marked valid or invalid by the validator. If the
        validator has prematurely stopped or validation is complete, initiates
        quit sequence.
        """
        if self.state == "crashed":
            self.cmessage("cannot update while crashed", "warning")
            return False
        if not self.reader.running:
            self.cmessage("reader not running, cannot update", "warning")
            return False
        if (
            time.time() - self.last_lock_timestamp
            > self.netconf_params.get("LOCK_STALENESS_THRESHOLD", 3600) / 4
        ):
            self.acquire_lock(refresh=True)
        try:
            if not self.reader.update():
                return False
        except Exception as ex:
            self.cmessage("fatal error in log reader, terminating", "error")
            self.crash(ex)
            return False
        try:
            self._update_files()
        except Exception as ex:
            self.cmessage("fatal error in file update, terminating", "error")
            self.crash(ex)
        if self.transfer_complete and self.state == "running":
            self.cmessage(
                "all files transferred, awaiting validation", "complete"
            )
            self.state = "pending_validation"
        elif self.state == "pending_validation" and self.validation_complete:
            self.cmessage("validation complete", "complete")
            self.state = "complete"
            self.release_lock()
        stop = self.last_log.loc[self.last_log['category'] == 'stop']
        shutdown = self.last_log.loc[self.last_log['category'] == 'shutdown']

        if (
            (len(shutdown) + len(stop) == 0)
            and (self.n_failures <= conf.MAX_TRANSFER_FAILURES)
        ):
            return False
        if self.n_failures > conf.MAX_TRANSFER_FAILURES:
            self.cmessage(
                f"Quitting because more than configured limit of "
                f"{conf.MAX_TRANSFER_FAILURES} files have failed validation",
                "error"
            )
            self.quit()
            return True
        if len(shutdown) > 0:
            shut_message = shutdown.iloc[0]["message"]
        else:
            shut_message = "no reason given"
        if not self.validation_complete:
            self.cmessage(
                f"unexpected validation pipeline exit: {shut_message}. "
                f"terminating",
                "error"
            )
            self.quit()
        else:
            self.cmessage("Validation process complete", "complete")
            self.quit()
        return True

    @property
    def log(self) -> pd.DataFrame:
        return self.reader.log

    @property
    def last_log(self) -> pd.DataFrame:
        return self.reader.last_log

    control_bucket: Bucket | None
    transfer_bucket: Bucket | None
    logger: S3TSVWriter
    transfer_exc: ThreadPoolExecutor
    reader: S3TSVReader
    last_lock_timestamp: float | None = None
