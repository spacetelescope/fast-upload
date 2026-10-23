"""Formatters for responses to the upload client."""
from __future__ import annotations

from typing import Callable, Any

import yaml


def err_response(func: Callable) -> Callable:
    """Convenience wrapper: format an error message as a YAML mapping."""
    def dump_err_response(*args: Any, **kwargs: Any) -> str:
        step, details = func(*args, **kwargs)
        msg = {"status": "error", "step": step, "details": details}
        return yaml.dump(msg)

    return dump_err_response


@err_response
def llock_err_response(_ex: Exception) -> tuple[str, str]:
    """Error message: lambda lock already held"""
    return "lambda lock check", "duplicate function execution, terminating"


@err_response
def cbucket_err_response(_ex: Exception) -> tuple[str, str]:
    """Error message: lambda cannot access control bucket"""
    return "control bucket access", "unable to find/access control bucket"


@err_response
def conf_bucket_err_response(_ex: Exception) -> tuple[str, str]:
    """Error message: lambda cannot access config bucket"""
    return "config bucket access", "unable to find/access config bucket"


@err_response
def task_run_err_response(_ex: Exception) -> tuple[str, str]:
    """Error message: cannot launch validation task"""
    return "task launch", "task failed to launch"


@err_response
def task_setup_err_response(_ex: Exception) -> tuple[str, str]:
    """Error message: cannot set up validation task correctly"""
    return "task setup", "task setup failed"


@err_response
def iconfig_err_response(_ex: Exception) -> tuple[str, str]:
    """Error message: cannot read task configuration"""
    return "task config read", "bad task config"


@err_response
def tlock_err_response(_ex: Exception) -> tuple[str, str]:
    """Error message: transfer client lock not held by caller"""
    return "lockfile check", "transfer app lockfile invalid or not present"


@err_response
def vtask_running_err_response() -> tuple[str, str]:
    """Error message: validation task already running"""
    return (
        "validation task check",
        "a validation task is already running for this transfer"
    )


@err_response
def noconfig_err_response() -> tuple[str, str]:
    """Error message: no task configuration found"""
    return (
        "task config read",
        "Neither collection/provenance-specific nor default task configuration "
        "found. This error cannot be addressed by the user. Please "
        "contact system administrators."
    )


@err_response
def lock_write_err_response() -> tuple[str, str]:
    """Error message: unable to write lock"""
    return (
        "lambda lock file write",
        "lambda function could not write lock file. "
        "This error cannot be addressed by the user. "
        "Please contact system administrators."
    )


@err_response
def lambda_main_execution_error(_ex: Exception) -> tuple[str, str]:
    """Error message: lambda crashed for unknown reason"""
    return "lambda execution", "unclassified lambda execution failure"


def pipeline_exec_success_msg() -> str:
    """Lambda succeeded"""
    return yaml.dump({"status": "success"})
