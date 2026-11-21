"""The demo tool only opens an explicitly supplied, authorized PDF."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pymupdf
import pytest

from storybook.pipeline import assert_allowed_input

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "make_demo_book.py"


def test_importing_demo_does_not_discover_datasets(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Demo import must not enumerate or inspect candidate data directories")
    monkeypatch.setattr(Path, "is_dir", forbidden)
    spec = importlib.util.spec_from_file_location("demo_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)


@pytest.mark.parametrize("path", ["F:\\AI+\\测试集\\book.pdf", "F:/AI+/hidden_tests/book.pdf", "/safe/TEST_SET/book.pdf"])
def test_protected_paths_rejected_before_resolution(monkeypatch, path):
    def forbidden(*args, **kwargs):
        pytest.fail("Forbidden lexical path reached the filesystem")
    monkeypatch.setattr(Path, "resolve", forbidden)
    with pytest.raises(ValueError, match="隔离测试集"):
        assert_allowed_input(Path(path))


def test_demo_cli_rejects_missing_input_without_discovery(tmp_path):
    result = subprocess.run([sys.executable, str(SCRIPT)], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode != 0
    assert "--pdf" in result.stderr


def test_demo_uses_new_pdf_facts_without_sample_book_injection(tmp_path):
    pdf = tmp_path / "original-moon.pdf"
    with pymupdf.open() as document:
        page = document.new_page(width=240, height=320)
        page.insert_text((20, 50), "A silver moon shines.")
        document.save(pdf)
    output = tmp_path / "demo-data"
    result = subprocess.run([sys.executable, str(SCRIPT), "--pdf", str(pdf), "--data-dir", str(output), "--book-id", "new-book"],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    book = json.loads((output / "new-book" / "book.json").read_text())
    assert book["mode"] == "local"
    assert book["pages"][0]["native_text"] == "A silver moon shines."
    assert book["pages"][0]["description"] == ""
    assert book["scenes"][0]["narration"] == "A silver moon shines."
    assert "番茄" not in json.dumps(book, ensure_ascii=False)
    assert book["provenance"]["generative_video_used"] is False
    assert (output / "new-book" / "book.mp4").stat().st_size > 0
