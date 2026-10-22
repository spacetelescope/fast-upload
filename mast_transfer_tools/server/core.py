"""
Core objects for the FAST validation server. In normal operation, these expect
to be running in an isolated remote environment, communicating asynchronously
via "log" objects in an S3 directory bucket. They are not intended for local
use. See the validation commands in upload.cli, or their Pythonic interfaces
in validation, for local alternatives.
"""

import concurrent.futures
import datetime as dt
import difflib
import json
from concurrent.futures import ThreadPoolExecutor
from io import IOBase, StringIO
from pathlib import Path
import random
from string import ascii_lowercase
import time
from types import MappingProxyType as MPt
from typing import Any, Sequence, Callable

from asdf import AsdfFile
from astropy.io.fits import HDUList
from botocore.exceptions import ClientError
from dustgoggles.dynamic import exc_report
import numpy as np
from pyarrow.parquet import ParquetFile

from hostess.aws.s3 import Bucket
from hostess.aws.utilities import make_boto_client
from hostess.utilities import StoppableFuture
import pandas as pd

import mast_transfer_tools.config as conf
from mast_transfer_tools.labels import (
    Label, Filetype, STANDARDS_SUPPORTING_DATA_VALIDATION
)
from mast_transfer_tools.utilz.locks import check_lock, LockStatus
from mast_transfer_tools.s3log.s3tsvreader import S3TSVReader
from mast_transfer_tools.s3log.s3tsvwriter import S3TSVWriter
from mast_transfer_tools.s3log.helpers import LOG_FIELD_SPEC
import mast_transfer_tools.utilz.name_reference as names
from mast_transfer_tools.types import (
    TransferType,
    ValPipeSettings,
    ValIdent,
    PipelineNetworkConfig, ValidationSQSReport,
)
from mast_transfer_tools.errors import (
    InvalidFileIndexError,
    InvalidLabelError,
    BucketLockedError
)
from mast_transfer_tools.io import loader_for
from mast_transfer_tools.utilz.futures import is_crashed, is_running
from mast_transfer_tools.server.messages import (
    file_load_error_msg,
    success_msg,
    validation_error_msg,
    validation_failure_message
)
from mast_transfer_tools.server.state import (
    ValidationState, FileIndex, TERMINAL_CLIENT_STATES
)
from mast_transfer_tools.validation import check_data


def add_filetype_classification(
    typename: str,
    filetype: Filetype,
    index: pd.DataFrame,
    *,
    missing_filetypes_ok: bool
) -> pd.DataFrame:
    """
    Helper function for parse_index_file(). Return a copy of index with its
    'type' column populated with `typename` where a file matches the naming
     pattern defined in `filetype`. Raise an exception if the filetype is
     entirely absent in the index, or if its naming pattern collides with an
     already-populated filetype's.
    """
    index = index.copy()
    match_series = pd.Series(np.full_like(index.index, False))  # noqa: FBT003
    inclusions = [p.pattern for p in filetype.filename if p.include]
    exclusions = [p.pattern for p in filetype.filename if not p.include]
    for i in inclusions:
        match_series = match_series | index['filename'].str.match(i)
    for e in exclusions:
        match_series = match_series & ~index['filename'].str.match(e)
    if missing_filetypes_ok is False and not match_series.any():
        raise InvalidFileIndexError(
            f"No examples of filetype {filetype} in index"
        )
    collisions = set(
        index.loc[match_series, 'type'].unique()
    ).difference({None, filetype})
    if len(collisions) != 0:
        raise InvalidFileIndexError(
            f"Filename definition for {filetype} collides with "
            f"{', '.join(collisions)}"
        )
    index.loc[match_series, 'type'] = typename
    return index


def _maybe_raise(
    exc: Exception, formatter: Callable[[Exception], dict], *, do_raise: bool
) -> dict:
    """Debug wrapper for "optional" behavior"""
    if do_raise:
        raise exc
    return formatter(exc)


def load_data(
    filetype_spec: Filetype,
    key: str,
    bucket: str | Bucket | None = None,
    *,
    debug: bool = False
) -> tuple[ParquetFile | AsdfFile | HDUList | None, dict | None]:
    """
    Load an object into memory using the method defined for its standard.

    Returns:
        data: ParquetFile, AsdfFile, or HDUList as appropriate. Or, if
            loading fails, None.
        exception_report: None if loading succeeds. Information on exception
            if loading fails.
    """
    standard = filetype_spec.standard
    loader = loader_for(standard)
    try:
        data = loader(key, bucket)
    except Exception as exc:
        return None, _maybe_raise(exc, file_load_error_msg, do_raise=debug)
    return data, None


def _maybe_check_objs(
    spec: Filetype,
    data: ParquetFile | AsdfFile | HDUList,
    *,
    debug: bool = False
) -> tuple[dict, dict | None]:
    """
    Check data against objects defined in filetype, if any are defined, plus
    an optional custom object-check hook.

    Returns:
        failures: dict like {check_name: failures}. If empty, validation
            succeeded.
        exception_report: Formatted report on encountered exception if some
            validation step crashed. None otherwise.
    """
    hook_module = getattr(spec.validation_options, "object_check_hook", None)

    if len(spec.objects) == 0 and hook_module is None:
        return {}, None

    try:
        failures = check_data(data, spec)

    except Exception as exc:
        # this case is an unhandled exception in the checker, not a
        # "validation failure"
        return {}, _maybe_raise(exc, validation_error_msg, do_raise=debug)
    return failures, None


def validate_file_content(
    bucket: str | Bucket | None,
    key: str,
    *,
    spec: Filetype,
    debug: bool = False
) -> dict:
    """
    Perform data-level validation as specified by filetype. Intended
    primarily to be called by validate_file().

    NOTE: this function should only be called if we are performing
        some task that requires actually loading some of the contents of a
        file, even if it's just validating the basic file format (e.g. 'is
        this valid FITS?')

    Returns:
        dict like {'status': status, 'message': message}.
    """
    try:
        data, load_err = load_data(spec, key, bucket, debug=debug)
    except Exception as ex:
        return validation_error_msg(ex)
    if load_err is not None:
        return load_err
    failures, check_err = _maybe_check_objs(spec, data)
    if check_err is not None:
        result = check_err
    elif len(failures) != 0:
        result = validation_failure_message(failures)
    else:
        result = success_msg()
    if hasattr(data, "close"):
        data.close()
    return result


OBJECT_LEVEL_VALIDATION_ATTRIBUTES = (
    "schema", "dtype", "objtype", "metadata", "ndim", "value", "name"
)


def _spec_has_data_validation(spec: Filetype | None) -> bool:
    """Does this filetype imply performance of data-level validation?"""
    if (
        spec is None
        or spec.standard not in STANDARDS_SUPPORTING_DATA_VALIDATION
        or "all" in spec.validation_options.skip
    ):
        return False

    if (
        getattr(spec.validation_options, "object_check_hook", None) is not None
        and "hook" not in spec.validation_options.skip
    ):
        return True

    if "standard" not in spec.validation_options.skip:
        return True
    for obj in spec.objects:
        for checkname in OBJECT_LEVEL_VALIDATION_ATTRIBUTES:
            if checkname in spec.validation_options.skip:
                continue
            attr = getattr(obj, checkname)
            # DataObject.metadata and DataObject.schema always exist, but
            # are empty when irrelevant. Others don't exist when irrelevant.
            # Note that a strict falsiness check isn't good here because 0,
            # False, etc. can be semantically meaningful for some attributes.
            if attr is None:
                continue
            if hasattr(attr, "__len__") and len(attr) == 0:
                continue
            return True
    return False


def validate_file(
    key: str, bucket: Bucket, *, checksum: str | None, spec: Filetype | None
) -> dict[str, str | dict | None]:
    """
    Handler function for individual file validation. Checks that the object is
    actually present; if it is and the defined filetype implies that data-level
    validation should be performed, perform that validation. Return a report
    properly formatted for use by ValidationManager, whether the validation
    succeeded, failed, or crashed. In normal operation, this is intended to be
    submitted by ValidationSession to its thread manager for aysnchronous
    execution.
    """
    # even if we don't need to actually look at the object, we should
    # check to see if it is actually present and that the checksum, if any,
    # matches
    try:
        head = bucket.head(key)
    except ClientError:
        return {"status": "failure", "message": "file not found"}
    if checksum is not None and (cks := head.get('ChecksumCRC32')) != checksum:
        return {
            "status": "failure",
            "message": f"incorrect crc32 checksum: expected "
                       f"{checksum}, got {cks}"
        }
    # we do not want to open the file _at all_ if no data-level validation is
    # required, which is why we don't just check for skips inline of the
    # validation function.
    if spec is not None:
        return validate_file_content(bucket, key, spec=spec)

    return success_msg()


def parse_index_file(
    file: str | Path | IOBase, label: Label,
) -> FileIndex:
    """
    Validate and preprocess a client-uploaded index file, adding
    filetypes from a label. Primarily intended as a helper function
    for ValidationSession._init_launch_objs().
    """
    index = pd.read_csv(file)
    if index.columns[0] != "path":
        raise InvalidFileIndexError(
            "Header of first index column must be 'path'"
        )
    if "will_transfer" not in index.columns:
        raise InvalidFileIndexError(
            "File index must contain a 'will_transfer' column"
        )
    if len(index.columns) > 3:
        raise InvalidFileIndexError(
            "File index must have exactly two or three columns"
        )
    if len(index.columns) == 3 and "checksum" not in index.columns:
        raise InvalidFileIndexError(
            "Third column of file index, if present, must be 'checksum"
        )
    index['filename'] = index['path'].map(lambda p: Path(p).name)
    index['type'] = None
    global_opts = label.delivery_meta.global_validation_options
    for typename, filetype in label.filetypes.items():
        index = add_filetype_classification(
            typename,
            filetype,
            index,
            missing_filetypes_ok=global_opts.missing_filetypes_ok
        )
    if not global_opts.no_assigned_filetype_ok:
        n_missing = index['type'].isna().sum()
        if n_missing > 0:
            raise InvalidFileIndexError(
                f"Unable to assign types to {n_missing}/{len(index)} files"
            )
    index["status"] = "waiting"
    index['n_fail'] = 0
    index.loc[~index['will_transfer'], 'status'] = "done"
    return index


class ValidationManager:
    """
    Helper class for managing validation worker threads. Should typically only
    be used by `ValidationSession`.
    """
    def __init__(
        self,
        bucket: Bucket,
        index: FileIndex,
        label: Label,
        *,
        n_threads: int = 1
    ) -> None:
        self.index, self.label, self.bucket = index, label, bucket
        self.exc = ThreadPoolExecutor(n_threads)
        self.futures: dict[str, concurrent.futures.Future] = {}
        self._finished: dict[str, concurrent.futures.Future] = {}
        # precomputing this to avoid checking whether we actually need
        # to open a particular filetype hundreds of thousands of times
        self.filetypes_requiring_data = {}
        for typename, filetype in label.filetypes.items():
            filetype.validation_options.skip += (
                label.delivery_meta.global_validation_options.skip
            )
            if _spec_has_data_validation(filetype):
                self.filetypes_requiring_data[typename] = filetype

    def queue_validation(self, keys: Sequence[str]) -> None:
        """
        Check whether a set of uploaded keys are valid targets for validation,
        and, if so, submit them to the thread pool for validation.
        Should typically only be called from
        ValidationSession._pipe_loop_inner() in response to a client message
        that it has successfully uploaded more files.
        """
        fslice = self.index.loc[self.index["path"].isin(keys)]
        if (fslice["status"] != "waiting").any():
            # NOTE: it's possible that this one shouldn't be a hard
            #  error. argument is that it probably means the client just
            #  messed up; we should log it and add it to our failure count.
            #  on the other hand, maybe we _legitimately_ messed up and the
            #  validation sequence is now in an undefined state.
            raise ValueError("Some files already validated")
        if len(fslice) != len(keys):
            # we have already checked for this like six other places; if it
            # happens here, it's a hard error
            raise ValueError(
                "Duplicate or non-indexed files queued for validation"
            )
        if set(self.futures.keys()).intersection(keys):
            # same argument
            raise ValueError("Some files already queued for validation")
        for rec in fslice.to_dict('records'):
            path = rec['path']
            type_ = rec["type"]
            self.futures[path] = self.exc.submit(
                validate_file,
                path,
                self.bucket,
                # if this is None, we only check existence / checksum
                spec=self.filetypes_requiring_data.get(type_),
                checksum=rec.get("checksum")
            )

    def update_validation_results(self) -> dict[str, Any]:
        """
        Check our validation tasks to see if any have completed. Return
        a dict like {object_key: validation_result} for each completed
        task. Also remove any completed tasks from self.futures.
        """
        self._finished = {
            k: self.futures.pop(k)
            for k, f in tuple(self.futures.items())
            if f.done() is True
        }
        results = {}
        for k, f in self._finished.items():
            ix = self.index.loc[self.index["path"] == k].index[0]
            try:
                content = f.result()
                # note that "status" here can _also_ be "error".
                # we should only hit the except clause below in pretty unusual
                # situations.
                status = content["status"]
            except BaseException as ex:
                content, status = {"message": exc_report(ex)}, "error"
            self.index.loc[ix, "status"] = status
            results[k] = {"status": status, "message": content.get("message")}
        return results


class ValidationSession:
    """Top-level handler object for the validation pipeline."""

    def __init__(
        self,
        vstate: ValidationState,
        manager: ValidationManager,
        logger: S3TSVWriter,
        settings: ValPipeSettings,
        identifiers: ValIdent,
        index: FileIndex,
        label: Label
    ) -> None:
        """
        Caution:
            ValidationSession.from_launch_parameters() should typically be
            preferred to ValidationSession.__init__(). Call __init___()
            directly only if you have a special reason to manually construct
            one or more of the support objects it accepts as arguments.
        """
        self.vstate, self.manager, self.logger = vstate, manager, logger
        self.index, self.label = index, label
        self.exception = None
        for k, v in (identifiers | settings).items():
            setattr(self, k, v)
        self.identifiers, self.settings = identifiers, settings
        self.pipe_future: StoppableFuture | None = None
        self._pipe_exec = ThreadPoolExecutor(1)
        self.errors = {}

    @property
    def running(self) -> bool:
        """Have we actually launched the pipeline, and is it still going?"""
        return is_running(self.pipe_future)

    @property
    def crashed(self) -> bool:
        """
        Does the pipeline appear to have encountered an unhandled exception?
        """
        return is_crashed(self.pipe_future)

    @classmethod
    def _init_launch_objs(
        cls,
        bucket_name_stem: str,
        confb_name: str,
        az_id: str,
        cb_name: str,
        dataset: str,
        delivery_id: str,
        settings: ValPipeSettings,
        tb_name: str,
        transfer_type: TransferType,
        kw: dict | None = None
    ) -> tuple[dict, dict, BaseException | None]:
        """
        Intended primarily as a subroutine of from_launch_parameters();
        can also be called directly if ad-hoc use of partially-initialized
        helper objects is desired
        """
        kw = {} if kw is None else kw
        assert len(kw) == 0, "do not pass a populated mapping as 'kw'"
        kw["settings"] = settings
        own_agent_id = "".join(random.choices(ascii_lowercase, k=11))
        if cb_name is None:
            cb_name = names.control_bucket(
                bucket_name_stem, dataset, delivery_id, az_id
            )
        if tb_name is None:
            tb_name = names.transfer_bucket(
                bucket_name_stem, dataset, delivery_id, transfer_type
            )
        identifiers: ValIdent = {
            "confb_name": confb_name,
            "cb_name": cb_name,
            "tb_name": tb_name,
            "dataset": dataset,
            "delivery_id": delivery_id,
            "transfer_type": transfer_type,
            "agent_id": own_agent_id,
        }
        kw["identifiers"] = identifiers

        client = make_boto_client('s3', verify=False)
        control_bucket = Bucket(cb_name, client=client)
        transfer_bucket = Bucket(tb_name, client=client)
        kw["logger"] = S3TSVWriter(
            control_bucket,
            names.log_key(transfer_type, "validator"),
            fixed={"agent_id": own_agent_id},
            fields=LOG_FIELD_SPEC,
        )
        try:
            submitted_label_text = control_bucket.read(
                names.label_key(dataset, delivery_id)
            )
            label_submitted = Label.from_text(submitted_label_text)
            if not label_submitted.valid:
                raise InvalidLabelError(str(label_submitted.errors))
        except BaseException as ex:
            return kw, {
                "ref": names.label_key(dataset, delivery_id),
                "message": {"summary": "problem with user-submitted label"},
            }, ex
        config_bucket = Bucket(confb_name)
        try:
            accepted_label_text = config_bucket.read(
                f"{conf.LABEL_PREFIX}/{names.label_key(dataset, delivery_id)}"
            )
            label_accepted = Label.from_text(accepted_label_text)
            if not label_accepted.valid:
                raise InvalidLabelError(str(label_accepted.errors))
        except BaseException as ex:
            return kw, {
                "ref": names.label_key(dataset, delivery_id),
                "message": {"summary": "problem with accepted label"}
            }, ex
        sbuf, abuf = StringIO(), StringIO()
        label_submitted.serialize_to_file(sbuf)
        label_accepted.serialize_to_file(abuf)
        label_submitted_canonicalized = sbuf.read()
        label_accepted_canonicalized = abuf.read()
        if label_submitted_canonicalized != label_accepted_canonicalized:
            diff = "; ".join(difflib.ndiff(
                label_accepted_canonicalized.splitlines(),
                label_submitted_canonicalized.splitlines()))
            return kw, {
                "ref": names.label_key(dataset, delivery_id),
                "message": {"summary": f"mismatch between user-submitted and "
                                       f"accepted labels: {diff}"}
            }, InvalidLabelError()
        try:
            ibuf = control_bucket.get(
                names.index_key(dataset, delivery_id, transfer_type)
            )
            index = parse_index_file(ibuf, label_accepted)
            kw["index"] = index
        except BaseException as ex:
            return kw, {
                "ref": names.index_key(dataset, delivery_id, transfer_type),
                "message": {"summary": "could not read index"},
            }, ex
        client_log_reader = S3TSVReader(
            bucket=control_bucket,
            key=names.log_key(transfer_type, "client"),
            fields=LOG_FIELD_SPEC
        )
        kw["vstate"] = ValidationState(
            index=index,
            label=label_accepted,
            transfer_timeout=settings["transfer_timeout"],
            missing_timeout=settings["missing_timeout"],
            reader=client_log_reader
        )
        kw["manager"] = ValidationManager(
            transfer_bucket,
            index,
            label_accepted,
            n_threads=settings["n_val_threads"]
        )
        kw["label"] = label_accepted
        return kw, {}, None

    @classmethod
    def from_launch_parameters(
        cls,
        dataset: str,
        delivery_id: str,
        transfer_type: TransferType,
        settings: ValPipeSettings = MPt(conf.VAL_PIPE_SETTINGS),
        cb_name: str | None = None,
        tb_name: str | None = None,
    ) -> "ValidationSession":
        """
        Constructor for ValidationSession. Accepts simple string arguments
        that, in normal system operation, will be known to and can easily be
        remotely passed by the pipeline launch script. Also performs some
        basic validation of system state (e.g. label match between accepted
        and user-submitted labels).

        Should typically be preferred to directly calling
        ValidationSession.__init__().

        Args:
            dataset: dataset from the label
            delivery_id: delivery_id from the label
            transfer_type: "sample" or "staging" as appropriate
            settings: ValPipeSettings
            cb_name: Name of the control bucket, or None (default). If None,
                uses standard name construction rules.
            tb_name: Name of the transfer bucket, or None (default). If None,
                uses standard name construction rules.

        Returns:
            A ValidationSession initialized from passed parameters

        """
        print("constructing ValidationSession from passed parameters\n")
        exc, err_logdict, kw, obj = None, None, {}, None
        try:
            ssm = make_boto_client("ssm")
            response = ssm.get_parameter(
                Name=conf.NETWORK_CONFIG_PARAMETER,
                WithDecryption=True
            )
            netconf_params: PipelineNetworkConfig = json.loads(
                response['Parameter']['Value']
            )
            kw, errdict, exc = cls._init_launch_objs(
                netconf_params['BUCKET_STEM'],
                netconf_params['CONFIG_BUCKET'],
                netconf_params['AVAILABILITY_ZONE_ID'],
                cb_name,
                dataset,
                delivery_id,
                settings,
                tb_name,
                transfer_type,
                kw
            )
            if len(errdict) > 0:
                err_logdict = {"category": "init", "status": "error"} | errdict
                print(err_logdict, flush=True)
            else:
                obj = cls(**kw)
                if 'LOCK_STALENESS_THRESHOLD' in netconf_params:
                    obj.lock_staleness_threshold = (
                        netconf_params['LOCK_STALENESS_THRESHOLD']
                    )
                print("constructed session\n")
        except BaseException as unclassified_exc:
            exc = unclassified_exc
            err_logdict = {
                "category": "init",
                "status": "error",
                "ref": "self",
                "message": {"summary": "unhandled error in pipeline init"}
            }
            print(f"{err_logdict}\n")
        if obj is None and exc is None:
            exc = TypeError("Sequencing error in pipeline init")
        if exc is not None:
            if "logger" in kw.keys():
                cls._log_init_crash(err_logdict, exc, kw["logger"])
                kw["logger"].stop()
            if "vstate" in kw.keys():
                kw["vstate"].stop()
            report = str(exc_report(exc)).replace("\n", " ; ")
            print(f"{report}\n", flush=True)
            raise exc
        return obj

    @classmethod
    def _log_init_crash(
        cls,
        err_logdict: dict[str, str | dict],
        exc: Exception,
        logger: S3TSVWriter
    ) -> None:
        """
        Attempt to write fact of unhandled exception during init to S3
        log. If the logging attempt itself fails (for instance, because of
        a permissions issue with the S3 bucket), write that fact to stdout
        (which in normal operation also means Cloudwatch).
        """
        try:
            err_logdict["message"]["exception"] = f"{exc}: {type(exc)}"
            logger.write(**err_logdict)
            logger.write(
                category="shutdown",
                ref="self",
                status="error",
                message="shutting down due to initialization failure"
            )
            logger.write(category="stop", ref="self", status="ok")
            logger.stop()
        except Exception as log_exc:
            log_report = (
                "Encountered exception while attempting to write init crash "
                "to log: "
                + str(exc_report(log_exc)).replace("\n", " ; ")
            )
            print(f"{log_report}\n", flush=True)

            print("Unwritten init exception: \n", flush=True)

    def acquire_lock(
        self, *, refresh: bool = False, category: str = "init"
    ) -> tuple[bool | dict, Exception | None]:
        """
        Attempt to acquire the lock object in order to make sure another
        instance of the pipeline isn't running for this dataset and delivery.
        If refresh is True, write the lock file even if already held.

        Returns:
            result: True if successful, dict giving reason for failure if
                not, formatted for logging
            exception: None if successful, encountered exception if not
        """
        client = make_boto_client("s3", verify=False)
        control_bucket = Bucket(self.cb_name, client=client)
        lock_status = check_lock(
            control_bucket,
            "validator",
            self.agent_id,
            staleness_threshold=self.lock_staleness_threshold
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
            return {
                "category": category,
                "ref": "lock",
                "status": "failure",
                "message": {"summary": "could not acquire lock"},
            }, BucketLockedError(f"status {lock_status}")
        if do_write:
            control_bucket.put(
                self.agent_id, names.lock_key("validator"), literal_str=True
            )
            self.last_lock_timestamp = time.time()
        return True, None

    def release_lock(self) -> None:
        """
        Release the lock object if held. Does nothing if lock is not held.
        """
        client = make_boto_client("s3", verify=False)
        control_bucket = Bucket(self.cb_name, client=client)
        lock_status = check_lock(control_bucket, "validator", self.agent_id)
        if lock_status == LockStatus.HELD:
            control_bucket.rm(names.lock_key("validator"))

    def start(self, *, restart: bool = False) -> None:
        print("starting validation session", flush=True)
        if self.running and restart is False:
            raise ValueError(
                "Already running. Pass 'restart=True' to restart."
            )
        if self.crashed is True and restart is False:
            raise ValueError(
                "Pipeline crashed. Pass 'restart=True' to restart."
            )
        if self.running:
            self.pipe_future.stop()
        if self.running or self.crashed:
            self.logger.write(
                category="init",
                ref="self",
                status="ok",
                message="restarting validation pipeline"
            )
        try:
            acquired, failure = self.acquire_lock()
        except Exception as e:
            acquired = {
                "category": "init",
                "ref": "lock",
                "status": "error",
                "message": {"summary": "lock check error"},
            }
            failure = e
        if acquired is not True:
            print(f"Unable to acquire lock: {acquired}\n")
            self.logger.write(**acquired)
            self._log_stop()
            self.vstate.stop()
            self.logger.stop()
            raise failure
        if not self.vstate.reader.running:
            self.vstate.reader.start()
        self.pipe_future = StoppableFuture.launch_into(
            self._pipe_exec, self._pipe_loop
        )

    def _pipe_loop_inner(self, _sigdict: dict) -> bool:
        """
        Subroutine of `_pipe_loop()` split to more nicely live inside a
        try-except statement. Under no circumstances should you invoke this
        directly.
        """
        self.logger.write(
            category="init",
            ref="self",
            status="ok",
            message="validation pipeline started"
        )
        while (
            self.vstate.client_status not in TERMINAL_CLIENT_STATES
            or len(self.manager.futures) > 0
        ):
            if self.vstate.reader.running is False:
                raise ValueError("Log reader crashed / prematurely stopped.")
            are_updates, did_stop, are_issues, transfers = self.vstate.update()
            if are_updates:
                self.manager.queue_validation(transfers)
            for k, r in self.manager.update_validation_results().items():
                fields = {
                    "category": "validation", "ref": k, "status": r["status"],
                }
                fields["message"] = r.get("message")
                self.logger.write(**fields)
                self.vstate.n_completed += 1
                if r["status"] != "ok":
                    self.vstate.n_failures += 1
                    self.errors[k] = fields["message"]
            if self.logger.elapsed() > self.keepalive_threshold:
                self.logger.write(
                    category="keepalive",
                    ref="self",
                    message=f"{self.vstate.n_completed} / {self.vstate.n_expected_files}",
                    status="ok"
                )
            if _sigdict.get(0) is not None:
                return True
            if (
                time.time() - self.last_lock_timestamp
                > self.lock_staleness_threshold / 4
            ):
                self.acquire_lock(refresh=True, category="lock_refresh")
            time.sleep(self.loop_rate)
        return False

    def _log_exit(self) -> None:
        """
        Log the fact that we are in the process of gracefully shutting down
        in response to 'normal' pipeline events -- changes
        in client-reported status, client disappearance, validation
        completion, etc. -- and why.
        """
        self.logger.write(
            category="shutdown",
            status="ok",
            ref="self",
            message={
                "summary": "exiting at terminal state",
                "client status": self.vstate.client_status
            }
        )

    @staticmethod
    def _send_sqs_report(report: ValidationSQSReport) -> None:
        """
        Send a message to conf.VAL_PIPE_SQS_QUEUE_URL on pipeline exit.
        """
        sqs = make_boto_client('sqs', verify=False)
        sqs.send_message(
            QueueUrl=conf.VAL_PIPE_SQS_QUEUE_URL,
            MessageBody=json.dumps(report)
        )

    def _format_sqs_report(self) -> ValidationSQSReport:
        """Format a pipeline exit status message for SQS"""
        msg = {}
        msg["dataset"] = self.identifiers["dataset"]
        msg["delivery_id"] = self.identifiers["delivery_id"]
        msg["completed_at"] = dt.datetime.now(tz=dt.timezone.utc).isoformat()
        msg["transfer_type"] = self.identifiers["transfer_type"]
        msg["label_path"] = (
            f"s3://{self.identifiers['confb_name']}/"
            f"{names.label_key(self.identifiers['dataset'], self.identifiers['delivery_id'])}"
        )
        msg["details"] = {
            "files_validated": self.vstate.n_completed,
            "files_expected": self.vstate.n_expected_files,
            "total_failures": self.vstate.n_failures,
            "errors": self.errors
        }
        if self.exception is not None:
            msg["pipeline_exception"] = str(exc_report(self.exception))
        else:
            msg["pipeline_exception"] = "None"
        if not self.vstate.done:
            msg["validation_result"] = "incomplete"
        elif len(self.errors) > 0:
            msg["validation_result"] = "failure"
        else:
            msg["validation_result"] = "success"
        return msg

    def _log_termination(self) -> None:
        """
        Log the fact that we have exited due to commanded serverside
        termination. In normal operation, this will never happen,
        but may occur in some diagnostic or special-purpose modes.
        """
        self.logger.write(
            category="shutdown",
            status="failure",
            ref="self",
            message={
                "summary": "exiting due to commanded termination",
                "client status": self.vstate.client_status
            }
        )

    def _log_crash(self, ex: BaseException) -> None:
        """
        Log the fact that we are exiting due to an unhandled exception. This
        should not be called in response to simple validation failures (even
        if those failures themselves occurred due to unhandled exceptions),
        client-reported crashes, etc. It should only be called when the
        pipeline itself has entered an unrecoverable / undefined state and
        is about to quit.
        """
        self.logger.write(
            category="shutdown",
            status="error",
            ref="self",
            message={
                "summary": "exiting due to unhandled exception in pipeline",
                "client status": self.vstate.client_status,
                "error": exc_report(ex)
            }
        )

    def _cleanup(self) -> None:
        """
        Cleans up temp resources. Currently just releases the lock. Can be
        used as an extension point if the pipeline ever ends up using
        external scratch space, needs to terminate running processes, etc.
        """
        self.release_lock()

    def _log_stop(self) -> None:
        """
        Log the fact that we have stopped, regardless of reason. _Rationale_
        for stopping should be separately logged. This should be the last log
        call a server ever makes, and should always be called before exiting
        if the server ever got around to logging anything else at all.
        """
        self.logger.write(category="stop", ref="self", status="ok")

    def _pipe_loop(self, _sigdict: dict, _id: int = 0) -> None:
        """
        Main loop for validation pipeline. Should only be invoked via
        `ValidationSession.start()`; may otherwise exhibit a variety of
        unpleasant behaviors.
        """
        print("entering core pipe loop", flush=True)
        try:
            if self._pipe_loop_inner(_sigdict) is True:
                self._log_termination()
            else:
                self._log_exit()
            sqs_message = self._format_sqs_report()
            self._send_sqs_report(sqs_message)
        except BaseException as ex:
            self._log_crash(ex)
            self.exception = ex
        self._cleanup()
        self._log_stop()
        time.sleep(1)
        self.logger.stop()
        self.vstate.stop()

    # the following attributes are populated from the contents of
    # `settings` and `identifiers`; see
    # mast_transfer_tools.fast_types.ValTypeSettings and ValTypeIdent
    cb_name: str
    """name of associated control bucket"""
    tb_name: str
    """name of associated transfer bucket"""
    dataset: str
    """name of dataset"""
    delivery_id: str
    """id of specific delivery"""
    transfer_type: TransferType
    """is this a staging or sample transfer?"""
    agent_id: str
    """randomly-generated identifier for this particular session"""
    transfer_timeout: float
    """
    how many seconds can elapse between messages from the client before we
    decide they've stopped without telling us?
    """
    missing_timeout: float
    """
    how many seconds can elapse between messages from the client before we
    start getting suspicious?
    """
    loop_rate: float
    """
    How many seconds should we wait between iterations of the main loop
    (including polling validation threads and the client log?)
    """
    n_val_threads: int
    """
    How many threads can we spawn at once for file validations?
    """
    keepalive_threshold: float
    """
    After how many seconds without writing any sort of message (keepalive or
    otherwise) should we write a keepalive message to our log?
    """
    az_id: str
    """
    AWS Availability Zone ID of our control bucket (and ideally also
    whatever we're running on)
    """
    lock_staleness_threshold: int = 3600
    """
    Default staleness threshold for locks (can be overridden by a value in
    fetched netconf_params).
    """
    last_lock_timestamp: float | None = None
    """When was the last time we wrote a lock (if at all)?"""
