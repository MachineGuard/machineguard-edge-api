"""Respaldo en caliente de la base SQLite de Edge con verificación y rotación.

Usa la API de respaldo en línea de sqlite3, así que puede ejecutarse mientras
Edge escribe. Cada copia se verifica con PRAGMA integrity_check antes de
publicarse; si falla, no queda ninguna copia nueva y no se borra nada.
"""
import argparse
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

PREFIX = 'edge-backup-'
SUFFIX = '.db'
BACKUP_NAME = re.compile(r'edge-backup-\d{8}T\d{6}\d{6}Z\.db')
DEFAULT_KEEP = 7
BUSY_TIMEOUT_SECONDS = 30


class BackupError(RuntimeError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def integrity_is_ok(database: Path) -> bool:
    connection = sqlite3.connect(str(database))
    try:
        return connection.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
    finally:
        connection.close()


def backup_names(destination: Path) -> list[str]:
    return sorted(p.name for p in destination.iterdir() if p.is_file() and BACKUP_NAME.fullmatch(p.name))


def prune(destination: Path, keep: int, newest: str) -> list[str]:
    """Deletes the oldest copies beyond `keep`. Only files with the backup name are candidates."""
    removed = []
    for name in backup_names(destination)[:-keep]:
        if name == newest:
            continue
        (destination / name).unlink()
        removed.append(name)
    return removed


def create_backup(source: Path, destination: Path, keep: int = DEFAULT_KEEP, clock=utc_now) -> dict:
    if keep < 1:
        raise BackupError('keep must be at least 1')
    source = Path(source)
    if not source.is_file():
        # Checked first: sqlite3.connect would silently create an empty database.
        raise BackupError(f'source database not found: {source.name}')
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)

    stamp = clock().strftime('%Y%m%dT%H%M%S%fZ')
    final_name = f'{PREFIX}{stamp}{SUFFIX}'
    temporary = destination / f'.{final_name}.partial'
    try:
        source_connection = sqlite3.connect(str(source), timeout=BUSY_TIMEOUT_SECONDS)
        try:
            target_connection = sqlite3.connect(str(temporary))
            try:
                source_connection.backup(target_connection)
                # A single self-contained file, without WAL side files next to the copy.
                target_connection.execute('PRAGMA journal_mode=DELETE')
            finally:
                target_connection.close()
        finally:
            source_connection.close()
        if not integrity_is_ok(temporary):
            raise BackupError('integrity_check failed for the new copy; nothing was published')
        os.replace(temporary, destination / final_name)
    except BackupError:
        temporary.unlink(missing_ok=True)
        raise
    except (sqlite3.Error, OSError) as error:
        temporary.unlink(missing_ok=True)
        raise BackupError(f'backup failed: {error}') from error

    removed = prune(destination, keep, final_name)
    return {
        'file': final_name,
        'bytes': (destination / final_name).stat().st_size,
        'retained': len(backup_names(destination)),
        'removed': removed,
    }


def parse_args(argv):
    default_database = os.getenv('EDGE_DATABASE_PATH', 'edge.db')
    parser = argparse.ArgumentParser(description='Respaldo en caliente de la base SQLite de Edge.')
    parser.add_argument('--database', default=default_database,
                        help='archivo SQLite de Edge (por defecto EDGE_DATABASE_PATH)')
    parser.add_argument('--destination', default=os.getenv('EDGE_BACKUP_DIR') or None,
                        help='carpeta de copias (por defecto EDGE_BACKUP_DIR o backups junto a la base)')
    parser.add_argument('--keep', type=int, default=os.getenv('EDGE_BACKUP_KEEP', str(DEFAULT_KEEP)),
                        help=f'copias que se conservan (por defecto {DEFAULT_KEEP})')
    args = parser.parse_args(argv)
    if args.destination is None:
        args.destination = str(Path(args.database).resolve().parent / 'backups')
    return args


def main(argv=None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        result = create_backup(Path(args.database), Path(args.destination), args.keep)
    except BackupError as error:
        print(f'ERROR: {error}', file=sys.stderr)
        return 1
    print(f"OK file={result['file']} bytes={result['bytes']} retained={result['retained']} "
          f"removed={len(result['removed'])}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
