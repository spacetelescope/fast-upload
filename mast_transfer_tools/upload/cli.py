"""Upload data sets to MAST, the Mikulski Archive for Space Telescopes.

This module defines a command-line interface to upload operations.
Its functions cannot usefully be used as part of a larger program.
The other modules in this subpackage define a Pythonic interface that _can_
be used by other programs.
"""

import sys
import warnings
from collections import defaultdict
from copy import deepcopy

from pathlib import Path
from typing import Any, Callable

import pandas as pd
import click
from rich import print as rprint
from yaml import YAMLError

from hostess.aws.s3 import Bucket
from mast_transfer_tools.describe import SUPPORTED_STANDARDS, FileDescription, describe_file
from mast_transfer_tools.utilz.shims import path_walk
from mast_transfer_tools.utilz.cli import (
    fatal_oserror,
    fatal_yaml_error,
    parse_src_url_arg,
    require_directory,
    require_no_label_errors,
    require_bucket,
    require_index,
    validate_chatty,
    require_valid_targets,
    calc_checksums_with_progress,
)
from mast_transfer_tools.utilz.numeric import parse_bytes_with_scale
from mast_transfer_tools.labels import Label, DataObject


class BytesWithScaleParamType(click.ParamType):
    """Shim between click and utilz.numeric.parse_bytes_with_scale."""

    name = "size"

    def convert(self, value: str | int, param: Any, ctx: Any) -> int:
        if isinstance(value, int):
            return value

        try:
            return parse_bytes_with_scale(value)
        except ValueError as e:
            self.fail(str(e), param, ctx)


BytesWithScale = BytesWithScaleParamType()


# Many of the commands take a similar set of options; they are listed here
# so we don't have to repeat the help text and whatnot.
optional_label_argument = click.argument(
    "label",
    metavar="[<label.yml>]",
    required=False,
    type=Path,
)

directory_argument = click.argument(
    "directory",
    metavar="<directory>",
    type=Path,
)

directory_or_label_argument = click.argument(
    "directory_or_label",
    metavar="<directory | label.yml>",
    type=Path,
)


def object_check_hook_option(fn: Callable[..., None]) -> Callable[..., None]:
    return click.option(
        "--object-check-hook/--no-object-check-hook",
        default=True,
        show_default=True,
        help=(
            "Run custom validation_options.object_check_hook modules defined "
            "in the label."
        ),
    )(fn)


# Does click have a way to not hardcode the command name here?
# argparse lets you do %(prog)s but that doesn't work in click.
@click.group(
    epilog="Use 'mast-upload <command> --help' for help on a specific command."
)
@click.version_option(package_name="mast_transfer_tools")
def main() -> None:
    """Upload data sets to MAST, the Mikulski Archive for Space Telescopes."""
    pass


@main.command()
@click.argument("source", metavar="<source>", type=str)
@click.argument("label", metavar="<label>", type=Path)
@click.option(
    "-o",
    "--output",
    metavar="<output>",
    help="label file to create instead of {dataset}-{delivery_id}-populated.yml.",
    type=Path,
    default=None,
)
@click.option(
    "-f",
    "--filetype_names",
    metavar="<filetype_names>",
    help="Populate only members of this comma-separated list of filetypes.",
    type=str,
    default=None,
)
def populate_label(
    *, source: str, label: Path, output: Path, filetype_names: str | None
) -> None:
    """
    Populate (or repopulate) the filetypes section of a label by analyzing
    files in <source>, where `<source>` is either the root directory of a tree
    under which your files are located or a prefix on an S3 bucket under which
    your files are located. S3 addresses must be preceded with 's3://'.
    Filename patterns and standards must be defined for
    each filetype, or the application will not be able to identify which
    files should go with which filetype and, if relevant, what data-level
    analysis to perform on those files. <source> may contain either your full
    data set or a representative subset of files for quicker analysis. If
    some filetypes are missing representatives, `populate_label()` will
    populate everything it can. Some highly complex or variable filetypes may
    not be describable by populate_label(); such filetypes must be defined
    manually if data-level validation past standard conformance is desired.
    In any case, it is best to manually review the generated file
    specifications and to check them against local validation commands.
    """
    try:
        parsed_label = Label.from_file(label)
        require_no_label_errors(parsed_label, label)
    except YAMLError as e:
        fatal_yaml_error(e, label)
    except OSError as e:
        fatal_oserror(e)

    if filetype_names is not None:
        filetype_names = filetype_names.split(",")
        if len(filetype_names) == 0:
            sys.stderr.write("Empty filetype list, nothing to do.\n")
            sys.exit(1)
        not_in_label = [
            ft
            for ft in filetype_names
            if ft not in parsed_label.filetypes.keys()
        ]
        if len(not_in_label) > 0:
            sys.stderr.write(
                f"Requested filetypes missing from label: {not_in_label}\n"
            )
            sys.exit(1)
        filetypes = {ft: parsed_label.filetypes[ft] for ft in filetype_names}
    else:
        filetypes = parsed_label.filetypes
        if len(filetypes) == 0:
            sys.stderr.write("No filetypes defined in label, nothing to do.")
            sys.exit(1)

    targets = defaultdict(list)
    unmatched = []
    overmatched = {}

    bucket, prefix = parse_src_url_arg(source)
    if bucket is None:
        covered_files_iter = parsed_label.covered_files_local(prefix)
    else:
        covered_files_iter = parsed_label.covered_files_s3(bucket, prefix)
    for fpath, ftypes in covered_files_iter:
        if bucket is None:
            fpath = source / fpath
        match len(ftypes):
            case 0:
                unmatched.append(fpath)
            case 1:
                ft_name = ftypes[0].lpath
                ft_name = ft_name[ft_name.rfind("/") + 1 :]
                if ft_name in filetypes.keys():
                    targets[ft_name].append(fpath)
            case _:
                ft_names = []
                for ft in ftypes:
                    ft_name = ft.lpath
                    ft_names.append(ft_name[ft_name.rfind("/") + 1 :])
                overmatched[fpath] = ", ".join(sorted(ftypes))

    fatal_errors = False
    if len(targets) == 0:
        fatal_errors = True
        sys.stderr.write(
            f"error: no files under {source} are described by filetypes "
            f"{sorted(filetypes.keys())}"
        )
    if len(overmatched) > 0:
        fatal_errors = True
        sys.stderr.write(
            "\nerror: some filetypes have overlapping name definitions. "
            "Overlapping files:\n\n"
        )
        for k, v in sorted(overmatched.items()):
            sys.stderr.write(f"  {k}: {v}\n")
    if len(unmatched) > 0:
        fatal_errors = True
        sys.stderr.write(
            f"error: some files under {source} are not described by any"
            f" filetypes.\n"
            f"note: If there are files that should be excluded from upload,"
            f" match them with a filetype tagged `ignore: true`.\n"
            f"Unmatched files:\n\n"
        )
        for p in sorted(unmatched):
            sys.stderr.write(f"  {p}\n")
    if fatal_errors:
        sys.exit(1)

    new_label = deepcopy(parsed_label)
    for ft_name, ft in filetypes.items():
        matching_files = targets[ft_name]
        if len(matching_files) == 0:
            rprint(
                f"[orange]Warning: no files matching "
                f"{[fn.pattern.pattern for fn in ft.filename]} are included "
                f"in {source}, not populating {ft_name}"
            )
            continue
        if ft.standard not in SUPPORTED_STANDARDS.keys():
            rprint(
                f"[cyan]Note: {ft.standard} does not support data-level "
                f"validation, not populating {ft_name}"
            )
            continue
        from rich.progress import Progress

        descriptions = []
        with Progress() as progress:
            task = progress.add_task(
                f"examining {ft_name} files....", total=len(matching_files)
            )
            for f in matching_files:

                with warnings.catch_warnings(record=True) as warning_trap:
                    desc = FileDescription(fn=f)
                    try:
                        desc = describe_file(f)
                    except Exception as ex:
                        # for the exceptions that wind up here, the exception
                        # type doesn't add to the error message
                        desc.errors.append(str(ex))
                    if len(warning_trap) > 0:
                        desc.warnings.extend(warning_trap)
                descriptions.append(desc)
                progress.update(task, advance=1)
            progress.remove_task(task)
        warned_descs = [d for d in descriptions if len(d.warnings) > 0]
        if len(warned_descs) > 0:
            rprint(
                f"[yellow]warning:[/yellow] while processing [blue]{ft_name}[/blue],"
                f" encountered problems: "
            )
            for f in warned_descs:
                for w in f.warnings:
                    if hasattr(w, "message"):
                        w_message = str(w.message)
                    else:
                        w_message = str(w)
                    rprint(
                        f"  [yellow]warning:[/yellow] [blue]{f.fn}[/blue]: {w_message}"
                    )
        failed_descs = [d for d in descriptions if len(d.errors) > 0]
        if len(failed_descs) > 0:
            rprint(
                f"[red]error:[/red] Unable to populate [blue]{ft_name}[/blue], because: "
            )
            for f in failed_descs:
                for e in f.errors:
                    rprint(f"  [red]error:[/red] [blue]{f.fn}[/blue]: {e}")
            if len(warned_descs) > 0:
                rprint(
                    "Warnings above may explain the problem in more detail."
                )
            continue
        description_module = SUPPORTED_STANDARDS[ft.standard]
        objs, unification_failure = description_module.unify_descriptions(
            descriptions
        )
        if unification_failure is not None:
            rprint(
                f"[red]Unable to accurately describe {ft_name}: "
                f"{unification_failure}"
            )
            continue
        new_label.filetypes[ft_name].objects = [DataObject(**o) for o in objs]
        rprint(
            f"[green]Populated label section for {ft_name} from "
            f"{len(matching_files)} files"
        )
    if output is None:
        output = Path(
            f"{new_label.dataset}-{new_label.delivery_id}-populated.yml"
        )
    with open(output, "w") as stream:
        new_label.serialize_to_file(stream)


@main.command()
@click.argument("source", metavar="<source>", type=str)
@click.argument("label", metavar="<label>", type=Path)
@click.option(
    "-o",
    "--output",
    metavar="<output>",
    help="file to write instead of filetypes.csv.",
    type=Path,
    default=Path("filetypes.csv"),
)
def report_filetypes(
    *, source: str, label: Path, output: Path = Path("filetypes.csv")
) -> None:
    """
    Write a CSV file containing matching filetypes as described in <label>
    for all files under <source>. The file has two columns: "path" and
    "filetypes", where "filetypes" is a semicolon-separated list of matching
    filetype names (blank if none match).

    Also prints a warning if any files are matched by more than one filetype
    (this is an invalid condition for transfer / description / validation).
    """
    try:
        parsed_label = Label.from_file(label)
        require_no_label_errors(parsed_label, label)
    except YAMLError as e:
        fatal_yaml_error(e, label)
    except OSError as e:
        fatal_oserror(e)

    targets = []
    filetypes = []
    unmatched = []
    overmatched = {}

    bucket, prefix = parse_src_url_arg(source)
    if bucket is None:
        covered_files_iter = parsed_label.covered_files_local(prefix)
    else:
        covered_files_iter = parsed_label.covered_files_s3(bucket, prefix)
    for fpath, ftypes in covered_files_iter:
        match len(ftypes):
            case 0:
                unmatched.append(fpath)
            case 1:
                ft_name = ftypes[0].lpath
                ft_name = ft_name[ft_name.rfind("/") + 1 :]
                targets.append(fpath)
                filetypes.append(ft_name)
            case _:
                ft_names = []
                for ft in ftypes:
                    ft_name = ft.lpath
                    ft_names.append(ft_name[ft_name.rfind("/") + 1 :])
                overmatched[fpath] = ", ".join(sorted(ftypes))

    fatal_errors = False
    if len(targets) == 0:
        fatal_errors = True
        sys.stderr.write(
            f"error: no files under {source} are matched by this label\n"
        )
    if len(overmatched) > 0:
        fatal_errors = True
        sys.stderr.write(
            "\nerror: some filetypes have overlapping name definitions. "
            "Overlapping files:\n\n"
        )
        for k, v in sorted(overmatched.items()):
            sys.stderr.write(f"  {k}: {v}\n")
    if len(unmatched) > 0:
        fatal_errors = True
        sys.stderr.write(
            f"error: some files under {source} are not described by any"
            f" filetypes.\n"
            f"note: If there are files that should be excluded from upload,"
            f" match them with a filetype tagged `ignore: true`.\n"
            f"Unmatched files:\n\n"
        )
        for p in sorted(unmatched):
            sys.stderr.write(f"  {p}\n")
    if fatal_errors:
        sys.exit(1)

    pd.DataFrame({"path": targets, "filetypes": filetypes}).to_csv(
        output, index=False
    )


@main.command()
@click.argument("source", metavar="<source>", type=str)
@click.argument("label", metavar="<label>", type=Path)
@click.argument("index_file", metavar="<index_file>", type=Path)
@click.option(
    "--sample",
    metavar="<sample>",
    is_flag=True,
    help="Is this a sample rather than a staging transfer?",
    type=bool,
)
def transfer(
    *,
    source: str,
    label: Path,
    index_file: Path,
    sample: bool,
) -> None:
    """
    Upload a data set to MAST.

    If the upload is interrupted or stops due to validation errors, you can
    simply repeat the command to resume from where it left off, after fixing
    any validation problems.
    """
    from .upload import upload

    if source.startswith("s3://"):
        from hostess.aws.s3 import Bucket

        bucket_name = source.replace("s3://", "")
        if "/" in bucket_name:
            sys.stderr.write(
                "Do not pass a bucket name + prefix. All keys in the index "
                "file for S3 objects must be given relative to bucket root."
            )
            sys.exit(1)
        source = Bucket(bucket_name)
        require_bucket(source)

    else:
        source = Path(source)
        require_directory(source)

    try:
        parsed_label = Label.from_file(label)
        require_no_label_errors(parsed_label, label)
    except YAMLError as e:
        fatal_yaml_error(e, label)
    except OSError as e:
        fatal_oserror(e)

    file_index = pd.read_csv(index_file)

    transfer_type = "staging" if not sample else "sample"

    upload(parsed_label, transfer_type, file_index, source)


@main.command()
@click.argument("file", metavar="<file>", type=str)
@click.argument("label", metavar="<label>", type=Path)
@object_check_hook_option
def validate(*, file: str, label: Path, object_check_hook: bool) -> None:
    """
    Validate an individual file against a label. This command confirms that
    the label itself is valid and that the filename matches exactly one
    filetype defined in the label. If the file standard supports data
    validation (FITS, ASDF, or Parquet), the command will furthermore confirm
    that it conforms to the file standard and, if data objects are defined
    in the label, that it conforms to that specification.
    """
    if file.startswith("s3://"):
        file = file.strip("s3://")
        bucket_name, file = file.split("/", maxsplit=1)
        require_bucket(Bucket(bucket_name))
    else:
        if not Path(file).exists():
            sys.stderr.write(f"{file} does not exist")
            sys.exit(1)
        bucket_name = None
    parsed_label = Label.from_file(label)
    require_no_label_errors(parsed_label, label)
    msg, success = validate_chatty(
        file, parsed_label, bucket_name, object_check_hook=object_check_hook
    )
    if success:
        rprint("[green]Successfully validated.")
    else:
        rprint(f"[red]Failed validation:\n{msg}")


@main.command()
@click.argument("source", metavar="<source>", type=str)
@click.argument("label", metavar="<label>", type=Path)
@object_check_hook_option
@click.option(
    "-i",
    "--index-file",
    metavar="<index_file>",
    help="validate only files listed in this index",
    type=Path,
    default=None,
)
def validate_all(
    *,
    source: str,
    label: Path,
    index_file: Path | None = None,
    object_check_hook: bool = True,
) -> None:
    """
    Validate all files under <source> against <label>.

    If <index_file> is provided, instead only validate files listed in that
    index. <index_file> should be a CSV file whose first column is 'path' and
    whose second column, if present, is 'checksum'. A file of this format may
    be automatically produced using the `index` command.

    <source> may be either a local directory or a prefix on an s3 bucket; if
    an s3 bucket/prefix, its name should be preceded by 's3://'.

    Paths in <index_file> will be treated as relative to <source>.
    (If an index file is provided, a specific prefix on an S3 bucket may
    not be used as <source>; paths must be relative to the root prefix.)

    This command confirms that the label itself is valid and that each
    filename matches exactly one filetype defined in the label.
    If a file standard supports data validation (FITS, ASDF, or Parquet),
    the command will furthermore confirm that it conforms to the file
    standard and, if data objects are defined in the label, that it conforms
    to that definition.
    """
    try:
        parsed_label = Label.from_file(label)
        require_no_label_errors(parsed_label, label)
    except YAMLError as e:
        fatal_yaml_error(e, label)

    except OSError as e:
        fatal_oserror(e)

    from hostess.aws.s3 import Bucket

    if source.startswith("s3://"):
        bucket_name = source.replace("s3://", "")
        if "/" in bucket_name and index_file is not None:
            sys.stderr.write(
                "Do not pass a bucket name + prefix. All keys in the index "
                "file for S3 objects must be given relative to bucket root."
            )
            sys.exit(1)
        if "/" in bucket_name:
            prefix = bucket_name.split("/", maxsplit=1)[1].strip("/") + "/"
        else:
            prefix = ""
        source_obj = Bucket(bucket_name)
        require_bucket(source_obj)
    else:
        source_obj = Path(source)
        require_directory(source_obj)
        bucket_name = None
        prefix = None
    if index_file is not None:
        table = require_index(index_file)
        targets = require_valid_targets(source_obj, table)
    elif isinstance(source_obj, Bucket):
        directory = source_obj.ls(prefix, recursive=True, formatting="df")
        if len(directory) == 0:
            sys.stderr.write(f"No objects under s3://{bucket_name}/{prefix}")
            sys.exit(1)
        targets = directory["Key"]
    else:
        targets = [
            source_obj / entry.path
            for entry in path_walk(source_obj)
            if entry.is_file(follow_symlinks=False)
        ]
        if len(targets) == 0:
            sys.stderr.write(f"No files under {source}")
            sys.exit(1)

    results = []

    from rich.progress import Progress

    with Progress() as progress:
        task = progress.add_task("Validating files...", total=len(targets))
        for t in targets:
            results.append(
                validate_chatty(
                    t,
                    parsed_label,
                    bucket_name,
                    object_check_hook=object_check_hook,
                )
            )
            progress.update(task, advance=1)

    failed_files = {
        file: msg
        for file, (msg, success) in zip(targets, results)
        if not success
    }
    if len(failed_files) == 0:
        rprint("[green]All files successfully validated")
        sys.exit(0)
    rprint(f"[red]{len(failed_files)}/{len(targets)} failed validation.")
    print("\n\n----\n\n")
    for fn, msg in failed_files.items():
        print(f"{fn}:\n{msg}\n\n----\n\n")


@main.command()
@click.argument("label", metavar="<label>", type=Path)
def check_label(label: Path) -> None:
    """Checks a label for syntactic validity."""
    parsed_label = Label.from_file(label)
    require_no_label_errors(parsed_label, label)
    rprint("[green]label ok")


@main.command()
@click.argument("source", metavar="<source>", type=str)
@click.argument("index_file", metavar="<index_file>", type=Path)
@click.option(
    "-o",
    "--output",
    metavar="<index.csv>",
    help="index file to create instead of index.csv.",
    type=Path,
    default=Path("index.csv"),
)
def checksum(
    *, source: str, index_file: Path, output: Path = Path("index.csv")
) -> None:
    """
    Add CRC32 checksums to an existing index file.
    """
    if source.startswith("s3://"):
        sys.stderr.write("Checksum creation from S3 is not supported.")
        sys.exit(1)
    source = Path(source)
    require_directory(source)

    if output == index_file:
        sys.stderr.write("Refusing to overwrite source index file.")
        sys.exit(1)

    table = require_index(index_file)

    try:
        writer = open(output, "wb")  # noqa: SIM115
        writer.close()
    except Exception as ex:
        sys.stderr.write(f"Can't write to {output}: {ex}.")
        sys.exit(1)

    targets = require_valid_targets(source, table)

    checksums = calc_checksums_with_progress(targets)

    table["checksum"] = checksums
    table.to_csv(output, index=False)


@main.command()
@click.argument("source", metavar="<source>", type=str)
@click.option(
    "-o",
    "--output",
    metavar="<index.csv>",
    help="index file to create instead of index.csv.",
    type=Path,
    default=Path("index.csv"),
)
@click.option(
    "-c",
    "--make-checksums",
    metavar="<checksum>",
    is_flag=True,
    help="calculate checksums for files?",
    type=bool,
)
@click.option(
    "-l",
    "--label",
    metavar="<label>",
    help="index only files described by this label",
    type=Path,
    default=None,
)
def index(
    *,
    source: str,
    output: Path = Path("index.csv"),
    make_checksums: bool = False,
    label: Path | None = None,
) -> None:
    """
    Create an index file suitable for use with other mast-upload commands. If
    -c / --make-checksums is passed, also calculate CRC32 checksums for each
    indexed file. (Checksum creation is not supported for S3 objects.)

    If <source> is a local directory, paths in the index file will be given
    relative to <source>, which must also be used as <source> for upload and
    validation commands that make use of that index file. If <source> is an
    S3 bucket + prefix, paths in the index file will be given relative to the
    bucket root prefix, and s3://bucket-name should be used as <source> for
    subsequent upload and validation commands .

    By default, this command indexes all standard files/objects under <source>.
    If you also provide <label>, it will index only files matching a defined
    filetype in that label. If you wish to restrict indexing in a way that
    cannot be defined by filetypes + top-level directory/prefix (for instance,
    you intend to deliver the contents of three sibling directories), it is
    preferable to construct the index manually and, if checksums are desired,
    subsequently use `checksum` to populate checksums.
    """
    if label is not None:
        parsed_label = Label.from_file(label)
        require_no_label_errors(parsed_label, label)
    else:
        parsed_label = None

    if source.startswith("s3://") and make_checksums:
        sys.stderr.write("Checksum creation from S3 is not supported.")
        sys.exit(1)

    try:
        writer = open(output, "wb")  # noqa: SIM115
        writer.close()
    except Exception as ex:
        sys.stderr.write(f"Can't write to {output}: {ex}.")
        sys.exit(1)

    if source.startswith("s3://"):
        from hostess.aws.s3 import Bucket

        bucket_name = source.replace("s3://", "")
        if "/" in bucket_name:
            prefix = bucket_name.split("/", maxsplit=1)[1].strip("/") + "/"
        else:
            prefix = ""
        bucket = Bucket(bucket_name)
        require_bucket(bucket)
        directory = bucket.ls(prefix, recursive=True, formatting="df")
        if len(directory) == 0:
            sys.stderr.write(f"No objects under s3://{bucket_name}/{prefix}")
            sys.exit(1)
        targets = directory["Key"]
        if parsed_label is not None:
            targets = [t for t in targets if parsed_label.covers_file(t)]
            if len(targets) == 0:
                sys.stderr.write(
                    f"No objects under s3://{bucket_name}/{prefix} are "
                    f"described by {label}"
                )
                sys.exit(1)
    else:
        source = Path(source)
        require_directory(source)
        if parsed_label is None:
            targets = [
                entry.path
                for entry in path_walk(source)
                if entry.is_file(follow_symlinks=False)
            ]
            if len(targets) == 0:
                sys.stderr.write(f"No files under {source}")
                sys.exit(1)
        else:
            targets = [
                t
                for t, ft in parsed_label.covered_files_local(source)
                if len(ft) != 0
            ]

            if len(targets) == 0:
                sys.stderr.write(
                    f"No files under {source} are described by {label}"
                )
                sys.exit(1)

    if not make_checksums:
        df = pd.DataFrame({"path": targets})
        df.to_csv(output, index=False)
        return

    checksums = calc_checksums_with_progress([source / t for t in targets])
    df = pd.DataFrame(
        {"path": targets, "checksum": checksums}
    )
    df.to_csv(output, index=False)


if __name__ == "__main__":
    sys.exit(main())
