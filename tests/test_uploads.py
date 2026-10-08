"""Pool photo replacement and cleanup of uploaded files on delete."""
from __future__ import annotations

from app.config import get_settings
from app.database import SessionLocal
from app.models import Pool, Reading

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _create_pool(client, name: str) -> int:
    resp = client.post(
        "/pools/new",
        data={"name": name, "volume": "30000", "volume_unit": "litres"},
        follow_redirects=False,
    )
    return int(resp.headers["location"].rstrip("/").split("/")[-1])


def _edit_photo(client, pool_id: int, filename: str, content_type: str):
    return client.post(
        f"/pools/{pool_id}/edit",
        data={"name": "Photo Pool", "volume": "30000"},
        files={"photo": (filename, _PNG, content_type)},
        follow_redirects=False,
    )


def _image_path(pool_id: int) -> str | None:
    with SessionLocal() as db:
        return db.get(Pool, pool_id).image_path


def _exists(filename: str) -> bool:
    return (get_settings().uploads_dir / filename).is_file()


def test_rejected_replacement_keeps_existing_photo(logged_in_client):
    pool_id = _create_pool(logged_in_client, "Photo Pool")
    _edit_photo(logged_in_client, pool_id, "pool.png", "image/png")
    original = _image_path(pool_id)
    assert original and _exists(original)

    resp = _edit_photo(logged_in_client, pool_id, "notes.txt", "text/plain")
    assert "error=" in resp.headers["location"]
    assert _image_path(pool_id) == original
    assert _exists(original)


def test_valid_replacement_removes_old_file(logged_in_client):
    pool_id = _create_pool(logged_in_client, "Photo Pool")
    _edit_photo(logged_in_client, pool_id, "a.png", "image/png")
    first = _image_path(pool_id)
    _edit_photo(logged_in_client, pool_id, "b.png", "image/png")
    second = _image_path(pool_id)
    assert second != first
    assert _exists(second) and not _exists(first)

