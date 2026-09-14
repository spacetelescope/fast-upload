from pathlib import Path

from click.testing import CliRunner
import pandas as pd
import pytest

import mast_transfer_tools.upload.cli as cli_mod
from mast_transfer_tools.upload.cli import main


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.mark.parametrize(
    "args",
    [
        ["--help"],
        ["populate-label", "--help"],
        ["report-filetypes", "--help"],
        ["transfer", "--help"],
        ["validate", "--help"],
        ["validate-all", "--help"],
        ["check-label", "--help"],
        ["checksum", "--help"],
        ["index", "--help"],
    ],
)
def test_upload_cli_help_smoke(runner: CliRunner, args: list[str]) -> None:
    result = runner.invoke(main, args)

    assert result.exit_code == 0, result.output
    assert "Traceback" not in result.output


def test_check_label_accepts_minimal_valid_label(
    runner: CliRunner,
    label_file: Path,
) -> None:
    result = runner.invoke(main, ["check-label", str(label_file)])

    assert result.exit_code == 0, result.output
    assert "label ok" in result.output


def test_check_label_rejects_bad_label(
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    label = tmp_path / "bad.yml"
    label.write_text("", encoding="utf-8")

    result = runner.invoke(main, ["check-label", str(label)])

    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "Errors found" in result.output


@pytest.mark.parametrize("label_option", [None, "--label"])
def test_index_local_directory_contract(
    runner: CliRunner,
    data_dir: Path,
    label_file: Path,
    tmp_path: Path,
    label_option: str | None,
) -> None:
    output = tmp_path / "index.csv"
    args = ["index", str(data_dir), "-o", str(output)]
    if label_option is not None:
        args.extend([label_option, str(label_file)])

    result = runner.invoke(main, args)

    assert result.exit_code == 0, result.output
    assert output.exists()

    table = pd.read_csv(output)

    # This is the contract documented by validate-all/checksum.
    assert table.columns.tolist() == ["path"]
    assert sorted(table["path"].tolist()) == ["a.txt", "nested/b.txt"]


def test_checksum_accepts_path_index(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    data_dir: Path,
    tmp_path: Path,
) -> None:
    index_file = tmp_path / "index.csv"
    output = tmp_path / "checksummed.csv"

    index_file.write_text("path\na.txt\nnested/b.txt\n", encoding="utf-8")

    def fake_checksums(targets: list[Path]) -> list[str]:
        return [f"checksum-{Path(t).name}" for t in targets]

    monkeypatch.setattr(
        cli_mod,
        "calc_checksums_with_progress",
        fake_checksums,
    )

    result = runner.invoke(
        main,
        ["checksum", str(data_dir), str(index_file), "-o", str(output)],
    )

    assert result.exit_code == 0, result.output

    table = pd.read_csv(output)
    assert table.columns.tolist() == ["path", "checksum"]
    assert table["checksum"].tolist() == [
        "checksum-a.txt",
        "checksum-b.txt",
    ]


def test_checksum_refuses_to_overwrite_source_index(
    runner: CliRunner,
    data_dir: Path,
    tmp_path: Path,
) -> None:
    index_file = tmp_path / "index.csv"
    index_file.write_text("path\na.txt\n", encoding="utf-8")

    result = runner.invoke(
        main,
        ["checksum", str(data_dir), str(index_file), "-o", str(index_file)],
    )

    assert result.exit_code != 0
    assert "Refusing to overwrite source index file" in result.output


def test_validate_no_object_check_hook_is_wired(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    data_dir: Path,
    label_file: Path,
) -> None:
    seen: dict[str, bool] = {}

    def fake_validate_chatty(
        file: str,
        parsed_label: object,
        bucket_name: str | None = None,
        *,
        object_check_hook: bool = True,
    ) -> tuple[str, bool]:
        seen["object_check_hook"] = object_check_hook
        return "", True

    monkeypatch.setattr(cli_mod, "validate_chatty", fake_validate_chatty)

    result = runner.invoke(
        main,
        [
            "validate",
            str(data_dir / "a.txt"),
            str(label_file),
            "--no-object-check-hook",
        ],
    )

    assert result.exit_code == 0, result.output
    assert seen["object_check_hook"] is False
    assert "Successfully validated" in result.output


def test_transfer_sample_flag_is_wired(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    data_dir: Path,
    label_file: Path,
    tmp_path: Path,
) -> None:
    index_file = tmp_path / "index.csv"
    index_file.write_text("path\na.txt\n", encoding="utf-8")

    calls: dict[str, object] = {}

    def fake_upload(
        parsed_label: object,
        transfer_type: str,
        file_index: pd.DataFrame,
        source: Path,
    ) -> None:
        calls["transfer_type"] = transfer_type
        calls["source"] = source
        calls["index_columns"] = list(file_index.columns)

    import mast_transfer_tools.upload.upload as upload_mod

    monkeypatch.setattr(upload_mod, "upload", fake_upload)

    result = runner.invoke(
        main,
        [
            "transfer",
            str(data_dir),
            str(label_file),
            str(index_file),
            "--sample",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls["transfer_type"] == "sample"
    assert calls["source"] == data_dir
    assert calls["index_columns"] == ["path"]


def test_index_refuses_s3_checksums(runner: CliRunner) -> None:
    result = runner.invoke(
        main,
        ["index", "s3://some-bucket/some-prefix", "--make-checksums"],
    )

    assert result.exit_code != 0
    assert "Checksum creation from S3 is not supported" in result.output


def test_validate_reports_failed_validation_as_successful_command(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "bad.fits"
    target.write_text("not really fits", encoding="utf-8")

    label = tmp_path / "label.yml"
    label.write_text("irrelevant because mocked", encoding="utf-8")

    parsed_label = object()

    monkeypatch.setattr(cli_mod.Label, "from_file", lambda _: parsed_label)
    monkeypatch.setattr(
        cli_mod,
        "require_no_label_errors",
        lambda parsed, label_path: None,
    )

    def fake_validate_chatty(
        file: str,
        parsed_label_arg: object,
        bucket_name: str | None = None,
        *,
        object_check_hook: bool = True,
    ) -> tuple[str, bool]:
        assert file == str(target)
        assert parsed_label_arg is parsed_label
        return "the file is bogus", False

    monkeypatch.setattr(cli_mod, "validate_chatty", fake_validate_chatty)

    result = runner.invoke(
        main,
        ["validate", str(target), str(label)],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "Failed validation" in result.output
    assert "the file is bogus" in result.output


def test_validate_all_reports_failed_files_without_failing_command(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "data"
    source.mkdir()
    good = source / "good.fits"
    bad = source / "bad.fits"
    good.write_text("good", encoding="utf-8")
    bad.write_text("bad", encoding="utf-8")

    label = tmp_path / "label.yml"
    label.write_text("irrelevant because mocked", encoding="utf-8")

    parsed_label = object()

    monkeypatch.setattr(cli_mod.Label, "from_file", lambda _: parsed_label)
    monkeypatch.setattr(
        cli_mod,
        "require_no_label_errors",
        lambda parsed, label_path: None,
    )

    def fake_validate_chatty(
        file: Path,
        parsed_label_arg: object,
        bucket_name: str | None = None,
        *,
        object_check_hook: bool = True,
    ) -> tuple[str, bool]:
        if Path(file).name == "bad.fits":
            return "bad file is bad", False
        return "", True

    monkeypatch.setattr(cli_mod, "validate_chatty", fake_validate_chatty)

    result = runner.invoke(
        main,
        ["validate-all", str(source), str(label)],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "1/2 failed validation" in result.output
    assert "bad file is bad" in result.output


def test_validate_missing_file_is_command_failure(
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    label = tmp_path / "label.yml"
    label.write_text("irrelevant", encoding="utf-8")

    result = runner.invoke(
        main,
        ["validate", str(tmp_path / "missing.fits"), str(label)],
    )

    assert result.exit_code != 0
    assert "does not exist" in result.output
