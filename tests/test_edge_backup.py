import importlib.util
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('edge_backup', ROOT / 'scripts' / 'edge-backup.py')
edge_backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(edge_backup)


def make_source(path: Path, rows: int = 3) -> Path:
    """Creates a WAL database like Edge's, with committed rows."""
    connection = sqlite3.connect(str(path))
    connection.execute('PRAGMA journal_mode=WAL')
    connection.execute('CREATE TABLE readings (id INTEGER PRIMARY KEY, value TEXT NOT NULL)')
    connection.executemany('INSERT INTO readings (value) VALUES (?)', [(f'row-{i}',) for i in range(rows)])
    connection.commit()
    connection.close()
    return path


def count_rows(path: Path) -> int:
    connection = sqlite3.connect(str(path))
    try:
        return connection.execute('SELECT COUNT(*) FROM readings').fetchone()[0]
    finally:
        connection.close()


class StepClock:
    """Each call returns a later instant, so every backup gets a distinct name."""
    def __init__(self):
        self.now = datetime(2026, 11, 7, 3, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        self.now += timedelta(seconds=1)
        return self.now


BACKUP_FILE = re.compile(r'edge-backup-\d{8}T\d{6}\d{6}Z\.db')


def backups(destination: Path) -> list[str]:
    return sorted(p.name for p in destination.iterdir() if BACKUP_FILE.fullmatch(p.name))


def test_backup_copies_committed_data_with_timestamped_name(tmp_path):
    source = make_source(tmp_path / 'edge.db', rows=5)
    destination = tmp_path / 'copies'
    result = edge_backup.create_backup(source, destination, clock=StepClock())
    assert result['file'] == 'edge-backup-20261107T030001000000Z.db'
    assert backups(destination) == [result['file']]
    assert count_rows(destination / result['file']) == 5
    assert result['bytes'] > 0 and result['removed'] == []


def test_backup_file_is_self_contained_without_wal_side_files(tmp_path):
    source = make_source(tmp_path / 'edge.db')
    destination = tmp_path / 'copies'
    result = edge_backup.create_backup(source, destination, clock=StepClock())
    assert [p.name for p in destination.iterdir()] == [result['file']]
    connection = sqlite3.connect(str(destination / result['file']))
    try:
        assert connection.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
    finally:
        connection.close()


def test_rotation_keeps_newest_copies_and_ignores_foreign_files(tmp_path):
    source = make_source(tmp_path / 'edge.db')
    destination = tmp_path / 'copies'
    destination.mkdir()
    (destination / 'notes.txt').write_text('keep me')
    (destination / 'edge-backup-manual.db').write_text('not our naming scheme')
    clock = StepClock()
    created = [edge_backup.create_backup(source, destination, keep=3, clock=clock) for _ in range(5)]

    names = backups(destination)
    assert names == sorted(r['file'] for r in created[-3:])
    assert created[3]['removed'] == [created[0]['file']]
    assert created[4]['removed'] == [created[1]['file']]
    assert (destination / 'notes.txt').read_text() == 'keep me'
    assert (destination / 'edge-backup-manual.db').exists()


def test_default_keep_is_seven(tmp_path):
    source = make_source(tmp_path / 'edge.db')
    destination = tmp_path / 'copies'
    clock = StepClock()
    for _ in range(9):
        result = edge_backup.create_backup(source, destination, clock=clock)
    assert len(backups(destination)) == 7
    assert result['retained'] == 7


def test_integrity_failure_publishes_nothing_and_keeps_old_copies(tmp_path, monkeypatch):
    source = make_source(tmp_path / 'edge.db')
    destination = tmp_path / 'copies'
    clock = StepClock()
    old = edge_backup.create_backup(source, destination, keep=2, clock=clock)
    before = backups(destination)

    monkeypatch.setattr(edge_backup, 'integrity_is_ok', lambda path: False)
    with pytest.raises(edge_backup.BackupError, match='integrity_check'):
        edge_backup.create_backup(source, destination, keep=1, clock=clock)

    assert backups(destination) == before == [old['file']]
    assert not list(destination.glob('.*.partial'))


def test_corrupt_source_fails_closed(tmp_path):
    source = tmp_path / 'edge.db'
    source.write_bytes(b'this is not a sqlite database' * 500)
    destination = tmp_path / 'copies'
    with pytest.raises(edge_backup.BackupError):
        edge_backup.create_backup(source, destination)
    assert backups(destination) == []
    assert not list(destination.glob('.*.partial'))


def test_backup_works_while_writer_holds_an_open_transaction(tmp_path):
    source = make_source(tmp_path / 'edge.db', rows=3)
    writer = sqlite3.connect(str(source), isolation_level=None)
    try:
        writer.execute('BEGIN IMMEDIATE')
        writer.executemany('INSERT INTO readings (value) VALUES (?)', [('uncommitted',)] * 50)
        result = edge_backup.create_backup(source, tmp_path / 'copies', clock=StepClock())
        assert count_rows(tmp_path / 'copies' / result['file']) == 3
    finally:
        writer.execute('ROLLBACK')
        writer.close()


def test_backup_is_consistent_while_writer_commits_concurrently(tmp_path):
    source = make_source(tmp_path / 'edge.db', rows=1)
    stop = threading.Event()
    committed = []

    def write():
        connection = sqlite3.connect(str(source), timeout=30)
        try:
            while not stop.is_set() and len(committed) < 300:
                connection.execute('INSERT INTO readings (value) VALUES (?)', ('live',))
                connection.commit()
                committed.append(1)
        finally:
            connection.close()

    thread = threading.Thread(target=write)
    thread.start()
    try:
        results = [edge_backup.create_backup(source, tmp_path / 'copies', keep=10, clock=StepClock())
                   for _ in range(3)]
    finally:
        stop.set()
        thread.join(timeout=60)
    for result in results:
        copy = tmp_path / 'copies' / result['file']
        assert edge_backup.integrity_is_ok(copy)
        assert 1 <= count_rows(copy) <= 1 + len(committed)
    assert count_rows(source) == 1 + len(committed)


def test_missing_source_is_not_created(tmp_path):
    source = tmp_path / 'absent.db'
    with pytest.raises(edge_backup.BackupError, match='not found'):
        edge_backup.create_backup(source, tmp_path / 'copies')
    assert not source.exists()


def test_keep_must_be_at_least_one(tmp_path):
    source = make_source(tmp_path / 'edge.db')
    with pytest.raises(edge_backup.BackupError, match='keep'):
        edge_backup.create_backup(source, tmp_path / 'copies', keep=0)


def test_cli_reports_failure_with_exit_code_one(tmp_path, capsys):
    code = edge_backup.main(['--database', str(tmp_path / 'absent.db'), '--destination', str(tmp_path / 'c')])
    assert code == 1
    assert 'not found' in capsys.readouterr().err


def test_cli_defaults_to_backups_folder_next_to_database(tmp_path, monkeypatch):
    monkeypatch.delenv('EDGE_BACKUP_DIR', raising=False)
    monkeypatch.delenv('EDGE_BACKUP_KEEP', raising=False)
    source = make_source(tmp_path / 'edge.db')
    args = edge_backup.parse_args(['--database', str(source)])
    assert Path(args.destination) == tmp_path.resolve() / 'backups'
    assert args.keep == 7


def test_cli_reads_destination_and_keep_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv('EDGE_BACKUP_DIR', str(tmp_path / 'from-env'))
    monkeypatch.setenv('EDGE_BACKUP_KEEP', '4')
    args = edge_backup.parse_args([])
    assert args.destination == str(tmp_path / 'from-env')
    assert args.keep == 4
    assert edge_backup.parse_args(['--keep', '2']).keep == 2
