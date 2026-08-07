from utils import db


def test_get_db_conninfo_reads_secrets_yaml(monkeypatch):
    monkeypatch.setattr(db.config, "secrets", {
        "database": {
            "host": "db",
            "port": 5432,
            "dbname": "nu-esports-bot",
            "user": "bot",
            "password": "hunter2",
        }
    })
    assert db.get_db_conninfo() == "host=db port=5432 dbname=nu-esports-bot user=bot password=hunter2"
