import pytest
from pathlib import Path
from textwrap import dedent


LABEL_TEXT = dedent("""\
    dataset: cli-smoke
    delivery_id: 1
    filetypes:
      text:
        filename: .*\\.txt
        standard: text
    time:
      delivery_start_date: 2026-01-01
    delivery_meta:
      schema_version: 0.1.0
""")


@pytest.fixture
def label_file(tmp_path: Path) -> Path:
    label = tmp_path / "label.yml"
    label.write_text(LABEL_TEXT, encoding="utf-8")
    return label


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (root / "a.txt").write_text("alpha\n", encoding="utf-8")
    (nested / "b.txt").write_text("beta\n", encoding="utf-8")
    return root

