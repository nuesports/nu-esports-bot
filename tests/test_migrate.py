import pytest

from utils import db, migrate


@pytest.mark.asyncio
async def test_run_migrations_applies_every_file(migrated_db):
    files = sorted(migrate.MIGRATIONS_DIR.glob("*.sql"))
    applied = await db.fetch_all("SELECT version FROM schema_migrations;")
    applied_names = {row[0] for row in applied}

    assert applied_names == {f.name for f in files}


@pytest.mark.asyncio
async def test_run_migrations_is_idempotent(migrated_db):
    """Running twice shouldn't error or duplicate rows -- migrate.py checks
    schema_migrations before applying anything, this just proves that holds."""
    before = await db.fetch_all("SELECT version FROM schema_migrations;")

    await migrate.run_migrations()

    after = await db.fetch_all("SELECT version FROM schema_migrations;")
    assert sorted(before) == sorted(after)


@pytest.mark.asyncio
async def test_run_migrations_raises_if_no_files(monkeypatch, tmp_path):
    monkeypatch.setattr(migrate, "MIGRATIONS_DIR", tmp_path)
    with pytest.raises(RuntimeError):
        await migrate.run_migrations()
