"""StorageFileImport body validation."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from kragen.api.schemas import StorageFileImport


def test_path_mode() -> None:
    m = StorageFileImport(
        url="https://example.com/f.pdf",
        workspace_id=uuid.uuid4(),
        dest_folder_path="/library/pq",
        filename="doc.pdf",
    )
    assert m.parent_id is None
    assert m.file_name is None


def test_parent_mode() -> None:
    pid = uuid.uuid4()
    m = StorageFileImport(
        url="https://example.com/f.pdf",
        workspace_id=uuid.uuid4(),
        parent_id=pid,
        file_name="doc.pdf",
    )
    assert m.dest_folder_path is None


def test_rejects_both_path_and_parent() -> None:
    with pytest.raises(ValidationError):
        StorageFileImport(
            url="https://a/b",
            workspace_id=uuid.uuid4(),
            dest_folder_path="/a",
            parent_id=uuid.uuid4(),
            file_name="x.pdf",
        )
