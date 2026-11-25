"""Private shopping lists that belong to one account.

Only the owner can see them, whatever household they are in. Selected products
can be sent to a household cart, which applies that household's cart rules.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, Response

from backend.shared.api import API_PREFIX
from backend.shared.database import DatabaseConnection

from . import checkout_session
from .app import Caller, apply_account_age, household_access, require_visible_cart, visible_product
from .db import transaction
from .lists import (
    MAX_LIST_QUANTITY, AddItem, AddItems, ChangeResult, CreateList, ListChanges, ListDetail, ListItem, ListSummary,
    MoveResult, MoveToCart, RenameList, SkippedItem, UpdateItem, add_rows_to_cart, item_visible, now_iso,
)

router = APIRouter()
LISTS = f"{API_PREFIX}/my-lists"
LIST = f"{LISTS}/{{list_id}}"


class MoveMyItems(MoveToCart):
    household_id: str


@contextmanager
def my_access(request: Request, caller: Caller, *, write: bool = False) -> Iterator[DatabaseConnection]:
    with transaction(request.app.state.db_path, write=write) as db:
        apply_account_age(db, caller)
        yield db


def require_my_list(db: DatabaseConnection, list_id: str, caller: Caller):
    row = db.execute(
        "SELECT id, name, updated_at FROM personal_lists WHERE id = ? AND user_id = ?", (list_id, caller.user_id),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="List not found")
    return row


def to_my_item(row, caller: Caller) -> ListItem:
    checked = bool(row["checked"])
    return ListItem(
        id=row["id"], sku=row["sku"], custom_name=row["custom_name"], quantity=row["quantity"], note=row["note"],
        added_by=caller.user_id, added_by_name="You", checked=checked,
        checked_by=caller.user_id if checked else None, checked_by_name="You" if checked else None,
        created_at=row["created_at"], updated_at=row["updated_at"], can_delete=True,
    )


def load_my_items(db: DatabaseConnection, request: Request, caller: Caller, list_id: str) -> list[ListItem]:
    rows = db.execute(
        "SELECT * FROM personal_list_items WHERE list_id = ? ORDER BY created_at, id", (list_id,),
    ).fetchall()
    return [to_my_item(row, caller) for row in rows if item_visible(request, caller, row["sku"])]


def summary(row, items: list[ListItem], caller: Caller) -> ListSummary:
    return ListSummary(
        id=row["id"], name=row["name"], created_by=caller.user_id, created_by_name="You", item_count=len(items),
        checked_count=sum(item.checked for item in items), updated_at=row["updated_at"], can_manage=True,
    )


def detail(db: DatabaseConnection, request: Request, caller: Caller, list_id: str) -> ListDetail:
    row = require_my_list(db, list_id, caller)
    items = load_my_items(db, request, caller, list_id)
    return ListDetail(**summary(row, items, caller).model_dump(), items=items)


def require_my_item(db: DatabaseConnection, request: Request, list_id: str, item_id: str, caller: Caller):
    row = db.execute(
        "SELECT * FROM personal_list_items WHERE list_id = ? AND id = ?", (list_id, item_id),
    ).fetchone()
    if row is None or not item_visible(request, caller, row["sku"]):
        raise HTTPException(status_code=404, detail="Item not found")
    return row


def touch(db: DatabaseConnection, list_id: str, now: str) -> None:
    db.execute("UPDATE personal_lists SET updated_at = ? WHERE id = ?", (now, list_id))


def insert(db: DatabaseConnection, request: Request, list_id: str, body: AddItem, caller: Caller, now: str) -> str:
    if body.sku is not None:
        visible_product(request, body.sku, caller)
        existing = db.execute(
            "SELECT id, quantity FROM personal_list_items WHERE list_id = ? AND sku = ? AND checked = 0 LIMIT 1",
            (list_id, body.sku),
        ).fetchone()
        if existing is not None:
            db.execute(
                "UPDATE personal_list_items SET quantity = ?, note = COALESCE(?, note), updated_at = ? WHERE id = ?",
                (min(MAX_LIST_QUANTITY, existing["quantity"] + body.quantity), body.note or None, now, existing["id"]),
            )
            return existing["id"]
    item_id = str(uuid4())
    db.execute(
        """INSERT INTO personal_list_items
           (id, list_id, sku, custom_name, quantity, note, checked, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)""",
        (item_id, list_id, body.sku, body.custom_name, body.quantity, body.note or None, now, now),
    )
    return item_id


@router.get(LISTS)
def get_lists(request: Request, caller: Caller) -> list[ListSummary]:
    with my_access(request, caller) as db:
        rows = db.execute(
            "SELECT id, name, updated_at FROM personal_lists WHERE user_id = ? ORDER BY updated_at DESC, id",
            (caller.user_id,),
        ).fetchall()
        items: dict[str, list[ListItem]] = {}
        for row in db.execute(
            """SELECT items.* FROM personal_list_items AS items JOIN personal_lists AS lists ON lists.id = items.list_id
               WHERE lists.user_id = ?""",
            (caller.user_id,),
        ).fetchall():
            if item_visible(request, caller, row["sku"]):
                items.setdefault(row["list_id"], []).append(to_my_item(row, caller))
        return [summary(row, items.get(row["id"], []), caller) for row in rows]


@router.post(LISTS, status_code=201)
def create_list(body: CreateList, request: Request, caller: Caller) -> ListDetail:
    list_id, now = str(uuid4()), now_iso()
    with my_access(request, caller, write=True) as db:
        db.execute(
            "INSERT INTO personal_lists (id, user_id, name, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (list_id, caller.user_id, body.name, now, now),
        )
        return detail(db, request, caller, list_id)


@router.get(LIST)
def get_list(list_id: str, request: Request, caller: Caller) -> ListDetail:
    with my_access(request, caller) as db:
        return detail(db, request, caller, list_id)


@router.patch(LIST)
def rename_list(list_id: str, body: RenameList, request: Request, caller: Caller) -> ListDetail:
    with my_access(request, caller, write=True) as db:
        require_my_list(db, list_id, caller)
        db.execute("UPDATE personal_lists SET name = ?, updated_at = ? WHERE id = ?", (body.name, now_iso(), list_id))
        return detail(db, request, caller, list_id)


@router.delete(LIST, status_code=204)
def delete_list(list_id: str, request: Request, caller: Caller) -> Response:
    with my_access(request, caller, write=True) as db:
        require_my_list(db, list_id, caller)
        db.execute("DELETE FROM personal_list_items WHERE list_id = ?", (list_id,))
        db.execute("DELETE FROM personal_lists WHERE id = ?", (list_id,))
    return Response(status_code=204)


@router.post(f"{LIST}/items", status_code=201)
def add_item(list_id: str, body: AddItem, request: Request, caller: Caller) -> ListItem:
    now = now_iso()
    with my_access(request, caller, write=True) as db:
        require_my_list(db, list_id, caller)
        item_id = insert(db, request, list_id, body, caller, now)
        touch(db, list_id, now)
        return to_my_item(require_my_item(db, request, list_id, item_id, caller), caller)


@router.post(f"{LIST}/items/bulk", status_code=201)
def add_items(list_id: str, body: AddItems, request: Request, caller: Caller) -> ListDetail:
    started = datetime.now(timezone.utc)
    with my_access(request, caller, write=True) as db:
        require_my_list(db, list_id, caller)
        for index, item in enumerate(body.items):
            insert(db, request, list_id, item, caller, (started + timedelta(microseconds=index)).isoformat())
        touch(db, list_id, (started + timedelta(microseconds=len(body.items))).isoformat())
        return detail(db, request, caller, list_id)


@router.post(f"{LIST}/items/clear-checked")
def clear_checked(list_id: str, request: Request, caller: Caller) -> ListDetail:
    with my_access(request, caller, write=True) as db:
        require_my_list(db, list_id, caller)
        db.execute("DELETE FROM personal_list_items WHERE list_id = ? AND checked = 1", (list_id,))
        touch(db, list_id, now_iso())
        return detail(db, request, caller, list_id)


def apply_update(
    db: DatabaseConnection, request: Request, list_id: str, item_id: str, body: UpdateItem, caller: Caller, now: str,
) -> None:
    require_my_item(db, request, list_id, item_id, caller)
    if body.quantity is not None:
        db.execute("UPDATE personal_list_items SET quantity = ? WHERE id = ?", (body.quantity, item_id))
    if "note" in body.model_fields_set:
        db.execute("UPDATE personal_list_items SET note = ? WHERE id = ?", (body.note or None, item_id))
    if body.checked is not None:
        db.execute("UPDATE personal_list_items SET checked = ? WHERE id = ?", (int(body.checked), item_id))
    db.execute("UPDATE personal_list_items SET updated_at = ? WHERE id = ?", (now, item_id))


@router.post(f"{LIST}/changes")
def change_items(list_id: str, body: ListChanges, request: Request, caller: Caller) -> ChangeResult:
    started = datetime.now(timezone.utc)
    now = (started + timedelta(microseconds=len(body.add))).isoformat()
    skipped: list[SkippedItem] = []
    with my_access(request, caller, write=True) as db:
        require_my_list(db, list_id, caller)
        for index, item in enumerate(body.add):
            try:
                insert(db, request, list_id, item, caller, (started + timedelta(microseconds=index)).isoformat())
            except HTTPException as error:
                skipped.append(SkippedItem(id=item.sku or "", reason=str(error.detail)))
        for item in body.set:
            try:
                apply_update(db, request, list_id, item.id, item, caller, now)
            except HTTPException as error:
                skipped.append(SkippedItem(id=item.id, reason=str(error.detail)))
        for item_id in body.remove:
            db.execute("DELETE FROM personal_list_items WHERE list_id = ? AND id = ?", (list_id, item_id))
        touch(db, list_id, now)
        return ChangeResult(list=detail(db, request, caller, list_id), skipped=skipped)


@router.patch(f"{LIST}/items/{{item_id}}")
def update_item(list_id: str, item_id: str, body: UpdateItem, request: Request, caller: Caller) -> ListItem:
    now = now_iso()
    with my_access(request, caller, write=True) as db:
        require_my_list(db, list_id, caller)
        apply_update(db, request, list_id, item_id, body, caller, now)
        touch(db, list_id, now)
        return to_my_item(require_my_item(db, request, list_id, item_id, caller), caller)


@router.delete(f"{LIST}/items/{{item_id}}", status_code=204)
def delete_item(list_id: str, item_id: str, request: Request, caller: Caller) -> Response:
    with my_access(request, caller, write=True) as db:
        require_my_list(db, list_id, caller)
        require_my_item(db, request, list_id, item_id, caller)
        db.execute("DELETE FROM personal_list_items WHERE id = ?", (item_id,))
        touch(db, list_id, now_iso())
    return Response(status_code=204)


@router.post(f"{LIST}/move-to-cart")
def move_to_cart(list_id: str, body: MoveMyItems, request: Request, caller: Caller) -> MoveResult:
    now = now_iso()
    with household_access(request, body.household_id, caller, write=True) as (db, is_primary):
        require_my_list(db, list_id, caller)
        cart = require_visible_cart(
            db, body.household_id, checkout_session.open_cart_id(db, body.target), caller, is_primary,
        )
        result = add_rows_to_cart(
            db, request, body.household_id, cart, caller, body.item_ids, now,
            find="SELECT id, sku, quantity FROM personal_list_items WHERE list_id = ? AND id = ?", list_id=list_id,
        )
        for item_id in result.moved:
            db.execute("UPDATE personal_list_items SET checked = 1, updated_at = ? WHERE id = ?", (now, item_id))
        if result.moved:
            touch(db, list_id, now)
        return result
