"""Synchronous order/attachment storage operations for the bounded worker."""

import base64
import binascii
from contextlib import closing
import fcntl
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import sqlite3
import threading
import uuid

from PIL import Image, ImageOps


MAX_ORDER_PHOTO_BYTES = 5 * 1024 * 1024
PDF_PHOTO_MAX_PIXELS = (900, 900)
ALLOWED_ORDER_PHOTO_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
_UUID_HEX = re.compile(r"^[0-9a-f]{32}$")
_PHOTO_NAME = re.compile(r"^[0-9a-f]{32}\.(?:jpg|png|webp)$")
_PDF_PHOTO_NAME = re.compile(r"^[0-9a-f]{32}\.pdf\.jpg$")
_JOURNAL_NAME = re.compile(r"^\.order-txn-([0-9a-f]{32})\.json$")


class OrderNotFoundError(Exception):
    pass


class OrderPhotoInputError(Exception):
    def __init__(self, message: str, *, too_large: bool = False, actual_size: int = 0):
        super().__init__(message)
        self.too_large = too_large
        self.actual_size = actual_size


class OrderPhotoPreparationError(Exception):
    pass


class StorageAlreadyOwnedError(RuntimeError):
    """Another process already owns this database/photo store."""


class StorageOwnerLock:
    """OS-level exclusive ownership lock tied to the database's persistent volume."""

    def __init__(self, descriptor: int, path: Path):
        self.path = path
        self._descriptor = descriptor
        self._owner_pid = os.getpid()
        self._guard = threading.Lock()

    @property
    def released(self) -> bool:
        return self._descriptor is None

    def release(self) -> None:
        with self._guard:
            descriptor = self._descriptor
            if descriptor is None:
                return
            self._descriptor = None
            try:
                # A forked child inherits the open file description. Closing
                # its copy is safe; LOCK_UN there would also unlock the parent.
                if os.getpid() == self._owner_pid:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def acquire_storage_owner_lock(db_path: Path) -> StorageOwnerLock:
    """Acquire a non-waiting process lock before touching DB or photo state."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_db_path = db_path.resolve()
    lock_path = resolved_db_path.with_name(f".{resolved_db_path.name}.owner.lock")
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as ex:
        os.close(descriptor)
        raise StorageAlreadyOwnedError("Хранилище заказов уже запущено другим процессом.") from ex
    except BaseException:
        os.close(descriptor)
        raise
    return StorageOwnerLock(descriptor, lock_path)


def _connect(db_path: Path):
    return sqlite3.connect(db_path)


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _journal_path(photo_dir: Path, transaction_id: str) -> Path:
    if not _UUID_HEX.fullmatch(transaction_id):
        raise ValueError("invalid transaction id")
    return photo_dir / f".order-txn-{transaction_id}.json"


def _write_journal(photo_dir: Path, journal: dict) -> Path:
    transaction_id = journal["transaction_id"]
    path = _journal_path(photo_dir, transaction_id)
    temporary = photo_dir / f".{path.name}.{uuid.uuid4().hex}.tmp"
    encoded = json.dumps(journal, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(photo_dir)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        os.close(descriptor)
    return path


def _remove_journal(path: Path) -> None:
    path.unlink(missing_ok=True)
    if path.parent.is_dir():
        _fsync_directory(path.parent)


def _safe_photo_path(photo_dir: Path, filename: str) -> Path:
    if not isinstance(filename, str) or not _PHOTO_NAME.fullmatch(filename):
        raise ValueError("invalid stored photo filename")
    return photo_dir / filename


def _safe_order_asset_path(photo_dir: Path, filename: str) -> Path:
    if not isinstance(filename, str) or not (_PHOTO_NAME.fullmatch(filename) or _PDF_PHOTO_NAME.fullmatch(filename)):
        raise ValueError("invalid stored order asset filename")
    return photo_dir / filename


def _safe_transaction_file(photo_dir: Path, filename: str, transaction_id: str) -> Path:
    if not _UUID_HEX.fullmatch(transaction_id):
        raise ValueError("invalid transaction id")
    if not isinstance(filename, str) or Path(filename).name != filename or ".." in filename:
        raise ValueError("invalid transaction filename")
    return photo_dir / filename


def _validate_journal_assets(journal: dict, transaction_id: str, filename: str) -> list:
    assets = journal.get("assets")
    if not isinstance(assets, list) or len(assets) > 2:
        raise ValueError("invalid delete journal assets")
    allowed = {filename, pdf_photo_path(_safe_photo_path(Path("."), filename)).name}
    seen = set()
    for asset in assets:
        if not isinstance(asset, dict):
            raise ValueError("invalid delete journal asset")
        source = asset.get("source")
        tombstone = asset.get("tombstone")
        digest = asset.get("sha256")
        if source not in allowed or source in seen:
            raise ValueError("invalid delete journal source")
        if tombstone != f".delete-{transaction_id}-{source}":
            raise ValueError("invalid delete journal tombstone")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("invalid delete journal digest")
        seen.add(source)
    return assets


def initialize_customer_orders_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(_connect(db_path)) as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS customer_orders (
                id TEXT PRIMARY KEY,
                product_name TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                customer_name TEXT NOT NULL DEFAULT '',
                contact TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                photo_filename TEXT,
                created_at TEXT NOT NULL,
                created_by INTEGER NOT NULL
            )
        """)
        connection.commit()


def list_customer_orders(db_path: Path, user_id: int) -> list:
    with closing(_connect(db_path)) as connection:
        rows = connection.execute(
            "SELECT id, product_name, quantity, customer_name, contact, note, photo_filename, created_at, created_by "
            "FROM customer_orders WHERE created_by = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    return [
        {
            "id": row[0], "product_name": row[1], "quantity": row[2],
            "customer_name": row[3], "contact": row[4], "note": row[5],
            "has_photo": bool(row[6]), "created_at": row[7], "created_by": row[8],
        }
        for row in rows
    ]


def _decode_order_photo(photo_data: str, photo_type: str) -> bytes:
    try:
        _header, encoded = photo_data.split(",", 1)
        photo_bytes = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as ex:
        raise OrderPhotoInputError("Не удалось прочитать фотографию.") from ex
    if not photo_bytes:
        raise OrderPhotoInputError("Не удалось прочитать фотографию.")
    if len(photo_bytes) > MAX_ORDER_PHOTO_BYTES:
        raise OrderPhotoInputError(
            "Размер фотографии превышает допустимый.",
            too_large=True,
            actual_size=len(photo_bytes),
        )
    signatures = {
        "image/jpeg": lambda value: value.startswith(b"\xff\xd8\xff"),
        "image/png": lambda value: value.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/webp": lambda value: len(value) >= 12 and value.startswith(b"RIFF") and value[8:12] == b"WEBP",
    }
    if photo_type not in signatures or not signatures[photo_type](photo_bytes):
        raise OrderPhotoInputError("Файл не соответствует формату изображения.")
    return photo_bytes


def _write_photo_atomically(photo_dir: Path, filename: str, transaction_id: str, photo_bytes: bytes) -> Path:
    photo_dir.mkdir(parents=True, exist_ok=True)
    final_path = _safe_photo_path(photo_dir, filename)
    temporary = photo_dir / f".{filename}.{transaction_id}.part"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(photo_bytes)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)
    os.replace(temporary, final_path)
    _fsync_directory(photo_dir)
    return final_path


def _photo_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _row_for_id(db_path: Path, order_id: str):
    with closing(_connect(db_path)) as connection:
        return connection.execute(
            "SELECT id, product_name, quantity, customer_name, contact, note, photo_filename, created_at, created_by "
            "FROM customer_orders WHERE id = ?",
            (order_id,),
        ).fetchone()


def _photo_referenced(db_path: Path, filename: str) -> bool:
    with closing(_connect(db_path)) as connection:
        return connection.execute(
            "SELECT 1 FROM customer_orders WHERE photo_filename = ? LIMIT 1",
            (filename,),
        ).fetchone() is not None


def _insert_row(db_path: Path, row: tuple) -> None:
    with closing(_connect(db_path)) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO customer_orders "
                "(id, product_name, quantity, customer_name, contact, note, photo_filename, created_at, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise


def create_customer_order(
    db_path: Path,
    photo_dir: Path,
    *,
    user_id: int,
    product_name: str,
    quantity: int,
    customer_name: str,
    contact: str,
    note: str,
    created_at: str,
    photo_data: str | None,
    photo_type: str | None,
    photo_extension: str | None,
) -> str:
    order_id = uuid.uuid4().hex
    transaction_id = uuid.uuid4().hex
    photo_filename = f"{uuid.uuid4().hex}{photo_extension}" if photo_data else None
    row = (
        order_id, product_name, quantity, customer_name, contact, note,
        photo_filename, created_at, user_id,
    )
    if not photo_data:
        _insert_row(db_path, row)
        return order_id

    photo_bytes = _decode_order_photo(photo_data, photo_type)
    photo_dir.mkdir(parents=True, exist_ok=True)
    photo_path = _safe_photo_path(photo_dir, photo_filename)
    temporary = photo_dir / f".{photo_filename}.{transaction_id}.part"
    journal = {
        "version": 1,
        "transaction_id": transaction_id,
        "operation": "create",
        "phase": "preparing",
        "order": {
            "id": order_id,
            "product_name": product_name,
            "quantity": quantity,
            "customer_name": customer_name,
            "contact": contact,
            "note": note,
            "photo_filename": photo_filename,
            "created_at": created_at,
            "created_by": user_id,
        },
        "photo_sha256": hashlib.sha256(photo_bytes).hexdigest(),
    }
    journal_path = _write_journal(photo_dir, journal)
    db_committed = False
    try:
        _write_photo_atomically(photo_dir, photo_filename, transaction_id, photo_bytes)
        journal["phase"] = "ready"
        _write_journal(photo_dir, journal)
        _insert_row(db_path, row)
        db_committed = True
    except BaseException:
        if not db_committed:
            journal["phase"] = "abort"
            try:
                _write_journal(photo_dir, journal)
                if not _photo_referenced(db_path, photo_filename):
                    if temporary.is_file() and not temporary.is_symlink():
                        temporary.unlink()
                    if photo_path.is_file() and not photo_path.is_symlink():
                        if _photo_hash(photo_path) == journal["photo_sha256"]:
                            photo_path.unlink()
                    if not temporary.exists() and not photo_path.exists():
                        _remove_journal(journal_path)
            except OSError:
                # Leave the validated journal for startup recovery; do not
                # replace the original database or disk exception.
                pass
        raise
    try:
        _remove_journal(journal_path)
    except OSError:
        # The committed row and photo are valid. Recovery will remove the
        # journal only after confirming both against SQLite.
        pass
    return order_id


def pdf_photo_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}.pdf.jpg")


def _cleanup_failed_pdf(
    db_path: Path,
    photo_dir: Path,
    order_id: str,
    user_id: int,
    journal_path: Path,
    journal: dict,
    temporary: Path,
    temporary_created: bool,
) -> None:
    try:
        if journal.get("phase") == "ready":
            # A ready transaction may have reached the atomic rename. Let the
            # normal, hash-checked recovery finish it; keep its journal if any
            # part of that verification or cleanup fails.
            _recover_pdf(db_path, photo_dir, journal_path, journal)
            return
        if temporary_created:
            if temporary.is_symlink():
                return
            temporary.unlink(missing_ok=True)
            _fsync_directory(photo_dir)
        _remove_journal(journal_path)
    except Exception:
        # Keep the journal so startup recovery can retry safely.
        return


def make_pdf_photo(
    path: Path,
    db_path: Path,
    photo_dir: Path,
    order_id: str,
    user_id: int,
) -> Path:
    thumbnail = pdf_photo_path(path)
    if thumbnail.is_symlink():
        raise OSError("PDF thumbnail is a symbolic link")
    if thumbnail.is_file() and thumbnail.stat().st_mtime >= path.stat().st_mtime:
        if os.name == "posix":
            os.chmod(thumbnail, 0o600)
        return thumbnail
    transaction_id = uuid.uuid4().hex
    temporary = thumbnail.with_name(f"{thumbnail.name}.{transaction_id}.tmp")
    journal = {
        "version": 1,
        "transaction_id": transaction_id,
        "operation": "pdf",
        "phase": "preparing",
        "order_id": order_id,
        "created_by": user_id,
        "source": path.name,
        "thumbnail": thumbnail.name,
        "temporary": temporary.name,
    }
    journal_path = _write_journal(photo_dir, journal)
    temporary_created = False
    try:
        with Image.open(path) as source:
            source.draft("RGB", PDF_PHOTO_MAX_PIXELS)
            image = ImageOps.exif_transpose(source)
            image.thumbnail(PDF_PHOTO_MAX_PIXELS, Image.Resampling.LANCZOS)
            if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
                image = image.convert("RGBA")
                flattened = Image.new("RGB", image.size, "white")
                flattened.paste(image, mask=image.getchannel("A"))
                image = flattened
            else:
                image = image.convert("RGB")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(temporary, flags, 0o600)
            temporary_created = True
            try:
                stream_context = os.fdopen(descriptor, "wb")
            except BaseException:
                os.close(descriptor)
                raise
            with stream_context as stream:
                image.save(stream, format="JPEG", quality=80, optimize=True)
                stream.flush()
                os.fsync(stream.fileno())
        journal["phase"] = "ready"
        journal["temporary_sha256"] = _photo_hash(temporary)
        _write_journal(photo_dir, journal)
        temporary.replace(thumbnail)
        _fsync_directory(photo_dir)
    except BaseException:
        _cleanup_failed_pdf(
            db_path,
            photo_dir,
            order_id,
            user_id,
            journal_path,
            journal,
            temporary,
            temporary_created,
        )
        raise
    try:
        _remove_journal(journal_path)
    except OSError:
        # Startup recovery verifies the order and thumbnail hash first.
        pass
    return thumbnail


def get_customer_order_photo(db_path: Path, photo_dir: Path, order_id: str, user_id: int, *, pdf: bool):
    with closing(_connect(db_path)) as connection:
        row = connection.execute(
            "SELECT photo_filename FROM customer_orders WHERE id = ? AND created_by = ?",
            (order_id, user_id),
        ).fetchone()
    if not row or not row[0]:
        raise OrderNotFoundError
    path = _safe_photo_path(photo_dir, row[0])
    if not path.is_file() or path.is_symlink():
        raise OrderNotFoundError
    if pdf:
        try:
            path = make_pdf_photo(path, db_path, photo_dir, order_id, user_id)
        except Exception as ex:
            raise OrderPhotoPreparationError from ex
    return path.read_bytes(), mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _delete_journal_assets(photo_dir: Path, journal: dict) -> bool:
    transaction_id = journal["transaction_id"]
    for asset in journal["assets"]:
        source = _safe_order_asset_path(photo_dir, asset["source"])
        tombstone = _safe_transaction_file(photo_dir, asset["tombstone"], transaction_id)
        if source.exists() or source.is_symlink():
            # A committed delete must have moved every asset before deleting
            # its row. An original still present is ambiguous; keep recovery data.
            return False
        if tombstone.is_symlink():
            return False
        if tombstone.is_file():
            if _photo_hash(tombstone) != asset["sha256"]:
                return False
            tombstone.unlink()
    _fsync_directory(photo_dir)
    return True


def delete_customer_order(db_path: Path, photo_dir: Path, order_id: str, user_id: int) -> bool:
    transaction_id = uuid.uuid4().hex
    journal_path = None
    moved = []
    with closing(_connect(db_path)) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT photo_filename FROM customer_orders WHERE id = ? AND created_by = ?",
                (order_id, user_id),
            ).fetchone()
            if not row:
                raise OrderNotFoundError

            assets = []
            filename = row[0]
            if filename:
                original = _safe_photo_path(photo_dir, filename)
                thumbnail = pdf_photo_path(original)
                for path in (original, thumbnail):
                    if path.is_symlink():
                        raise OSError("order attachment is a symbolic link")
                    if path.is_file():
                        assets.append({
                            "source": path.name,
                            "tombstone": f".delete-{transaction_id}-{path.name}",
                            "sha256": _photo_hash(path),
                        })

            if assets:
                journal = {
                    "version": 1,
                    "transaction_id": transaction_id,
                    "operation": "delete",
                    "order_id": order_id,
                    "created_by": user_id,
                    "photo_filename": filename,
                    "assets": assets,
                }
                photo_dir.mkdir(parents=True, exist_ok=True)
                journal_path = _write_journal(photo_dir, journal)
                for asset in assets:
                    source = _safe_order_asset_path(photo_dir, asset["source"])
                    tombstone = _safe_transaction_file(photo_dir, asset["tombstone"], transaction_id)
                    os.replace(source, tombstone)
                    moved.append((source, tombstone))
                _fsync_directory(photo_dir)

            cursor = connection.execute(
                "DELETE FROM customer_orders WHERE id = ? AND created_by = ?",
                (order_id, user_id),
            )
            if cursor.rowcount != 1:
                raise OrderNotFoundError
            connection.commit()
        except BaseException:
            connection.rollback()
            for source, tombstone in reversed(moved):
                try:
                    if tombstone.is_file() and not source.exists():
                        os.replace(tombstone, source)
                except OSError:
                    # The journal remains so startup recovery can restore it.
                    pass
            if moved:
                try:
                    _fsync_directory(photo_dir)
                except OSError:
                    pass
            if journal_path is not None and all(not tombstone.exists() for _, tombstone in moved):
                try:
                    _remove_journal(journal_path)
                except OSError:
                    pass
            raise

    if journal_path is not None:
        try:
            if _delete_journal_assets(photo_dir, journal):
                _remove_journal(journal_path)
        except OSError:
            # The row is committed as deleted. Any remaining file is a
            # journaled tombstone that startup recovery can safely retry.
            pass
    return True


def _insert_recovered_create(db_path: Path, journal: dict) -> None:
    order = journal["order"]
    row = (
        order["id"], order["product_name"], order["quantity"], order["customer_name"],
        order["contact"], order["note"], order["photo_filename"], order["created_at"],
        order["created_by"],
    )
    try:
        _insert_row(db_path, row)
    except sqlite3.IntegrityError:
        existing = _row_for_id(db_path, order["id"])
        if not existing or existing[6] != order["photo_filename"] or existing[8] != order["created_by"]:
            raise


def _recover_create(db_path: Path, photo_dir: Path, journal_path: Path, journal: dict) -> bool:
    order = journal.get("order")
    if not isinstance(order, dict):
        return False
    order_id = order.get("id")
    filename = order.get("photo_filename")
    transaction_id = journal.get("transaction_id")
    if not isinstance(order_id, str) or not _UUID_HEX.fullmatch(order_id):
        return False
    if not isinstance(filename, str) or not _PHOTO_NAME.fullmatch(filename):
        return False
    photo_path = _safe_photo_path(photo_dir, filename)
    temporary = _safe_transaction_file(photo_dir, f".{filename}.{transaction_id}.part", transaction_id)
    existing = _row_for_id(db_path, order_id)
    if existing:
        if existing[6] != filename or existing[8] != order.get("created_by"):
            return False
        if not photo_path.is_file() or photo_path.is_symlink():
            return False
        if _photo_hash(photo_path) != journal.get("photo_sha256"):
            return False
        _remove_journal(journal_path)
        return True

    if journal.get("phase") == "ready":
        if not photo_path.is_file() or photo_path.is_symlink():
            return False
        if _photo_hash(photo_path) != journal.get("photo_sha256"):
            return False
        _insert_recovered_create(db_path, journal)
        _remove_journal(journal_path)
        return True

    if journal.get("phase") not in {"preparing", "abort"}:
        return False
    if _photo_referenced(db_path, filename):
        return False
    if temporary.is_symlink():
        return False
    if temporary.is_file():
        temporary.unlink()
    if photo_path.is_symlink():
        return False
    if photo_path.is_file():
        if _photo_hash(photo_path) != journal.get("photo_sha256"):
            return False
        photo_path.unlink()
    _remove_journal(journal_path)
    return True


def _recover_delete(db_path: Path, photo_dir: Path, journal_path: Path, journal: dict) -> bool:
    order_id = journal.get("order_id")
    user_id = journal.get("created_by")
    filename = journal.get("photo_filename")
    transaction_id = journal.get("transaction_id")
    if not isinstance(order_id, str) or not _UUID_HEX.fullmatch(order_id):
        return False
    if not isinstance(user_id, int) or isinstance(user_id, bool):
        return False
    if not isinstance(filename, str) or not _PHOTO_NAME.fullmatch(filename):
        return False
    assets = _validate_journal_assets(journal, transaction_id, filename)
    existing = _row_for_id(db_path, order_id)
    if existing:
        if existing[8] != user_id or existing[6] != filename:
            return False
        for asset in assets:
            source = _safe_order_asset_path(photo_dir, asset["source"])
            tombstone = _safe_transaction_file(photo_dir, asset["tombstone"], transaction_id)
            if tombstone.is_symlink() or source.is_symlink():
                return False
            if tombstone.is_file():
                if source.exists() or _photo_hash(tombstone) != asset["sha256"]:
                    return False
                os.replace(tombstone, source)
            elif not source.is_file():
                return False
        if assets:
            _fsync_directory(photo_dir)
        _remove_journal(journal_path)
        return True

    for asset in assets:
        source = _safe_order_asset_path(photo_dir, asset["source"])
        tombstone = _safe_transaction_file(photo_dir, asset["tombstone"], transaction_id)
        if _photo_referenced(db_path, asset["source"]):
            return False
        if source.is_symlink() or tombstone.is_symlink() or source.exists():
            return False
        if tombstone.is_file():
            if _photo_hash(tombstone) != asset["sha256"]:
                return False
            tombstone.unlink()
    if assets:
        _fsync_directory(photo_dir)
    _remove_journal(journal_path)
    return True


def _recover_pdf(db_path: Path, photo_dir: Path, journal_path: Path, journal: dict) -> bool:
    transaction_id = journal.get("transaction_id")
    order_id = journal.get("order_id")
    user_id = journal.get("created_by")
    source_name = journal.get("source")
    if not isinstance(transaction_id, str) or not _UUID_HEX.fullmatch(transaction_id):
        return False
    if not isinstance(order_id, str) or not _UUID_HEX.fullmatch(order_id):
        return False
    if not isinstance(user_id, int) or isinstance(user_id, bool):
        return False
    if not isinstance(source_name, str) or not _PHOTO_NAME.fullmatch(source_name):
        return False
    source = _safe_photo_path(photo_dir, source_name)
    thumbnail = pdf_photo_path(source)
    expected_temporary = f"{thumbnail.name}.{transaction_id}.tmp"
    if journal.get("thumbnail") != thumbnail.name or journal.get("temporary") != expected_temporary:
        return False
    temporary = _safe_transaction_file(photo_dir, expected_temporary, transaction_id)
    existing = _row_for_id(db_path, order_id)
    if journal.get("phase") not in {"preparing", "ready"}:
        return False
    if existing:
        if existing[8] != user_id or existing[6] != source_name:
            return False
        if source.is_symlink() or not source.is_file():
            return False
        phase = journal.get("phase")
        if phase == "preparing":
            if temporary.is_symlink():
                return False
            if temporary.is_file():
                temporary.unlink()
            _remove_journal(journal_path)
            return True
        if phase != "ready":
            return False
        digest = journal.get("temporary_sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            return False
        if temporary.is_symlink() or thumbnail.is_symlink():
            return False
        if temporary.is_file() and _photo_hash(temporary) == digest:
            os.replace(temporary, thumbnail)
            _fsync_directory(photo_dir)
        elif not thumbnail.is_file() or _photo_hash(thumbnail) != digest:
            return False
        if temporary.exists():
            temporary.unlink()
        _remove_journal(journal_path)
        return True

    # If the order has since been deleted, clean only the validated temporary
    # output. The thumbnail may predate this operation and is left untouched.
    if temporary.is_symlink():
        return False
    digest = journal.get("temporary_sha256")
    if temporary.is_file():
        if not isinstance(digest, str) or _photo_hash(temporary) != digest:
            return False
        temporary.unlink()
        _fsync_directory(photo_dir)
    elif journal.get("phase") == "preparing":
        pass
    _remove_journal(journal_path)
    return True


def recover_order_transactions(db_path: Path, photo_dir: Path) -> tuple[int, int]:
    """Recover only journal-owned files; unknown/orphan files are untouched."""
    if not photo_dir.is_dir():
        return 0, 0
    recovered = 0
    unresolved = 0
    for journal_path in photo_dir.iterdir():
        match = _JOURNAL_NAME.fullmatch(journal_path.name)
        if not match or journal_path.is_symlink() or not journal_path.is_file():
            continue
        try:
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            if (
                not isinstance(journal, dict)
                or journal.get("version") != 1
                or journal.get("transaction_id") != match.group(1)
            ):
                unresolved += 1
                continue
            operation = journal.get("operation")
            if operation == "create":
                resolved = _recover_create(db_path, photo_dir, journal_path, journal)
            elif operation == "delete":
                resolved = _recover_delete(db_path, photo_dir, journal_path, journal)
            elif operation == "pdf":
                resolved = _recover_pdf(db_path, photo_dir, journal_path, journal)
            else:
                resolved = False
            if resolved:
                recovered += 1
            else:
                unresolved += 1
        except (OSError, UnicodeError, ValueError, TypeError, KeyError, sqlite3.Error, json.JSONDecodeError):
            unresolved += 1
    return recovered, unresolved


def order_file_to_response(db_path: Path, photo_dir: Path, order_id: str, user_id: int, pdf: bool):
    return get_customer_order_photo(db_path, photo_dir, order_id, user_id, pdf=pdf)
