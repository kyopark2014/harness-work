"""Task/message store backed by DynamoDB.

Tables (names derive from the project name; override with env vars):

* ``{project}-tasks``                PK ``user_id``, SK ``task_id``
* ``{project}-messages``             PK ``task_id``, SK ``message_id``,
                                      LSI ``task_id-created_at-index`` (chronological order)

Every write is a single-item, field-level operation, so concurrent ECS tasks (or the
same user on several machines) can no longer overwrite each other with a stale
whole-file copy the way the former per-user SQLite files could.

Local development: set ``DYNAMODB_ENDPOINT_URL`` (``run_local.sh`` starts DynamoDB
Local) and the tables are created automatically on startup.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "Claude 5.0 Sonnet"

_MESSAGES_BY_CREATED_INDEX = "task_id-created_at-index"

# DynamoDB items are capped at 400 KB, but the messages table has an LSI that projects
# ALL attributes and the limit applies to the item plus its index copy (~200 KB each).
_MAX_ITEM_PAYLOAD_BYTES = 180_000

_local = threading.local()
_tables_ready = False
_tables_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Configuration / clients
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _app_config() -> dict[str, Any]:
    try:
        from application import utils

        cfg = utils.load_config()
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _project_name() -> str:
    env_name = (os.environ.get("TASK_DB_PROJECT") or "").strip()
    if env_name:
        return env_name
    name = _app_config().get("projectName")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return "harness-work"


def _region() -> str:
    return (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or str(_app_config().get("region") or "").strip()
        or "us-west-2"
    )


def _endpoint_url() -> str | None:
    return (os.environ.get("DYNAMODB_ENDPOINT_URL") or "").strip() or None


def tasks_table_name() -> str:
    return (os.environ.get("TASKS_TABLE") or "").strip() or f"{_project_name()}-tasks"


def messages_table_name() -> str:
    return (os.environ.get("MESSAGES_TABLE") or "").strip() or f"{_project_name()}-messages"


def _resource():
    # boto3 resources are not thread-safe; keep one per thread.
    res = getattr(_local, "resource", None)
    if res is None:
        kwargs: dict[str, Any] = {"region_name": _region()}
        endpoint = _endpoint_url()
        if endpoint:
            kwargs["endpoint_url"] = endpoint
            # DynamoDB Local accepts any credentials; make sure some exist.
            if not os.environ.get("AWS_ACCESS_KEY_ID"):
                kwargs["aws_access_key_id"] = "local"
                kwargs["aws_secret_access_key"] = "local"
        res = boto3.resource("dynamodb", **kwargs)
        _local.resource = res
    return res


def _tasks():
    return _resource().Table(tasks_table_name())


def _messages():
    return _resource().Table(messages_table_name())


def table_specs() -> list[dict[str, Any]]:
    """CreateTable arguments for all store tables (also used for local auto-create)."""
    return [
        {
            "TableName": tasks_table_name(),
            "AttributeDefinitions": [
                {"AttributeName": "user_id", "AttributeType": "S"},
                {"AttributeName": "task_id", "AttributeType": "S"},
            ],
            "KeySchema": [
                {"AttributeName": "user_id", "KeyType": "HASH"},
                {"AttributeName": "task_id", "KeyType": "RANGE"},
            ],
            "BillingMode": "PAY_PER_REQUEST",
        },
        {
            "TableName": messages_table_name(),
            "AttributeDefinitions": [
                {"AttributeName": "task_id", "AttributeType": "S"},
                {"AttributeName": "message_id", "AttributeType": "S"},
                {"AttributeName": "created_at", "AttributeType": "S"},
            ],
            "KeySchema": [
                {"AttributeName": "task_id", "KeyType": "HASH"},
                {"AttributeName": "message_id", "KeyType": "RANGE"},
            ],
            "LocalSecondaryIndexes": [
                {
                    "IndexName": _MESSAGES_BY_CREATED_INDEX,
                    "KeySchema": [
                        {"AttributeName": "task_id", "KeyType": "HASH"},
                        {"AttributeName": "created_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            "BillingMode": "PAY_PER_REQUEST",
        },
    ]


def ensure_tables() -> None:
    """Create any missing tables (DynamoDB Local / first run). No-op for existing tables."""
    client = _resource().meta.client
    for spec in table_specs():
        name = spec["TableName"]
        try:
            client.describe_table(TableName=name)
            continue
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ResourceNotFoundException":
                raise
        try:
            client.create_table(**spec)
            client.get_waiter("table_exists").wait(TableName=name)
            logger.info("Created DynamoDB table %s", name)
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ResourceInUseException":
                raise


def init_db() -> None:
    """Prepare the store. Auto-creates tables for DynamoDB Local or TASK_STORE_AUTO_CREATE=1."""
    global _tables_ready
    with _tables_lock:
        auto_create = bool(_endpoint_url()) or (
            os.environ.get("TASK_STORE_AUTO_CREATE", "").strip().lower() in {"1", "true", "yes", "on"}
        )
        if auto_create:
            ensure_tables()
        _tables_ready = True
    logger.info(
        "Task store using DynamoDB (tasks=%s, messages=%s, endpoint=%s)",
        tasks_table_name(),
        messages_table_name(),
        _endpoint_url() or "aws",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _query_all(table: Any, **kwargs: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    while True:
        resp = table.query(**kwargs)
        items.extend(resp.get("Items", []))
        last = resp.get("LastEvaluatedKey")
        if not last:
            return items
        kwargs["ExclusiveStartKey"] = last


def _is_conditional_failure(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"




def _item_to_task(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item["task_id"],
        "user_id": item["user_id"],
        "title": item.get("title") or "New task",
        # Checkpoints must be isolated per task; use task id as the stable fallback.
        "runtime_session_id": item.get("runtime_session_id") or item["task_id"],
        "model_name": item.get("model_name") or DEFAULT_MODEL,
        "skills": list(item.get("skills") or []),
        "mcp_servers": list(item.get("mcp_servers") or []),
        "guardrail_enabled": bool(item.get("guardrail_enabled", False)),
        "llm_gateway_enabled": bool(item.get("llm_gateway_enabled", False)),
        "memory_enabled": bool(item.get("memory_enabled", True)),
        "pinned": bool(item.get("pinned", False)),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
    }


def _loads_list(raw: Any) -> list[Any]:
    if not raw:
        return []
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


def _item_to_message(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item["message_id"],
        "task_id": item["task_id"],
        "role": item["role"],
        "content": item.get("content") or "",
        "images": _loads_list(item.get("images_json")),
        "tool_events": _loads_list(item.get("tool_events_json")),
        "created_at": item.get("created_at"),
    }


def _fit_payload(
    content: str, images: list[str], tool_events: list[dict[str, Any]]
) -> tuple[str, str, str]:
    """Serialize message fields, shrinking them if the item would exceed the budget."""
    content = content or ""
    images_json = json.dumps(images, ensure_ascii=False)
    events_json = json.dumps(tool_events, ensure_ascii=False)

    def size() -> int:
        return len(content.encode("utf-8")) + len(images_json.encode("utf-8")) + len(
            events_json.encode("utf-8")
        )

    if size() <= _MAX_ITEM_PAYLOAD_BYTES:
        return content, images_json, events_json

    try:
        from application.tool_events_slim import slim_tool_events

        events_json = json.dumps(slim_tool_events(tool_events), ensure_ascii=False)
    except Exception:
        events_json = "[]"
    if size() > _MAX_ITEM_PAYLOAD_BYTES:
        events_json = "[]"
    if size() > _MAX_ITEM_PAYLOAD_BYTES:
        budget = max(_MAX_ITEM_PAYLOAD_BYTES - len(images_json.encode("utf-8")) - 64, 1_000)
        content = content.encode("utf-8")[:budget].decode("utf-8", errors="ignore") + "…"
    logger.warning("Message payload exceeded DynamoDB item budget; truncated before write")
    return content, images_json, events_json


def _touch_task(user_id: str, task_id: str, *, updated_at: str) -> bool:
    """Bump ``updated_at`` and ``message_count``. Returns False if the task does not exist."""
    try:
        _tasks().update_item(
            Key={"user_id": user_id, "task_id": task_id},
            UpdateExpression="SET updated_at = :u ADD message_count :one",
            ExpressionAttributeValues={":u": updated_at, ":one": 1},
            ConditionExpression="attribute_exists(task_id)",
        )
        return True
    except ClientError as exc:
        if _is_conditional_failure(exc):
            logger.warning("Task %s/%s not found while updating timestamp", user_id, task_id)
            return False
        raise


def _set_title_if_default(user_id: str, task_id: str, title: str) -> None:
    try:
        _tasks().update_item(
            Key={"user_id": user_id, "task_id": task_id},
            UpdateExpression="SET title = :t",
            ConditionExpression=(
                "attribute_exists(task_id) AND "
                "(attribute_not_exists(title) OR title = :empty OR title = :default)"
            ),
            ExpressionAttributeValues={":t": title, ":empty": "", ":default": "New task"},
        )
    except ClientError as exc:
        if not _is_conditional_failure(exc):
            raise


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


def list_tasks(user_id: str, limit: int = 100) -> list[dict[str, Any]]:
    items = _query_all(
        _tasks(),
        KeyConditionExpression=Key("user_id").eq(user_id),
        ConsistentRead=True,
    )
    tasks = [_item_to_task(i) for i in items]
    tasks.sort(key=lambda t: (t["pinned"], t["updated_at"] or ""), reverse=True)
    return tasks[:limit]


def get_task(task_id: str, user_id: str | None = None) -> dict[str, Any] | None:
    if not user_id:
        raise ValueError("user_id is required to resolve the task")
    resp = _tasks().get_item(
        Key={"user_id": user_id, "task_id": task_id}, ConsistentRead=True
    )
    item = resp.get("Item")
    return _item_to_task(item) if item else None


def get_task_refreshing(task_id: str, user_id: str | None = None) -> dict[str, Any] | None:
    """Kept for call-site compatibility: reads are strongly consistent, no refresh needed."""
    if not user_id:
        return None
    return get_task(task_id, user_id)


def create_task(
    user_id: str,
    *,
    model_name: str | None = None,
    skills: list[str] | None = None,
    mcp_servers: list[str] | None = None,
    guardrail_enabled: bool = False,
    llm_gateway_enabled: bool = False,
    memory_enabled: bool = True,
    title: str = "New task",
) -> dict[str, Any]:
    task_id = str(uuid.uuid4())
    now = _now_iso()
    item = {
        "user_id": user_id,
        "task_id": task_id,
        "title": title,
        # Keep the checkpoint namespace aligned with the task identity.
        "runtime_session_id": task_id,
        "model_name": model_name or DEFAULT_MODEL,
        "skills": list(skills or []),
        "mcp_servers": list(mcp_servers or []),
        "guardrail_enabled": bool(guardrail_enabled),
        "llm_gateway_enabled": bool(llm_gateway_enabled),
        "memory_enabled": bool(memory_enabled),
        "pinned": False,
        "message_count": 0,
        "created_at": now,
        "updated_at": now,
    }
    _tasks().put_item(Item=item, ConditionExpression="attribute_not_exists(task_id)")
    return _item_to_task(item)


def _update_task_fields(
    user_id: str, task_id: str, fields: dict[str, Any], *, now: str | None = None
) -> bool:
    names: dict[str, str] = {}
    values: dict[str, Any] = {":updated_at": now or _now_iso()}
    sets = ["updated_at = :updated_at"]
    for idx, (attr, value) in enumerate(fields.items()):
        names[f"#f{idx}"] = attr
        values[f":v{idx}"] = value
        sets.append(f"#f{idx} = :v{idx}")
    kwargs: dict[str, Any] = {}
    if names:
        kwargs["ExpressionAttributeNames"] = names
    try:
        _tasks().update_item(
            Key={"user_id": user_id, "task_id": task_id},
            UpdateExpression="SET " + ", ".join(sets),
            ExpressionAttributeValues=values,
            ConditionExpression="attribute_exists(task_id)",
            **kwargs,
        )
        return True
    except ClientError as exc:
        if _is_conditional_failure(exc):
            return False
        raise


def update_all_tasks(
    user_id: str,
    *,
    model_name: str | None = None,
    guardrail_enabled: bool | None = None,
    memory_enabled: bool | None = None,
) -> int:
    """Apply shared settings to every task for ``user_id``. Returns rows updated."""
    fields: dict[str, Any] = {}
    if model_name is not None:
        fields["model_name"] = str(model_name).strip() or DEFAULT_MODEL
    if guardrail_enabled is not None:
        fields["guardrail_enabled"] = bool(guardrail_enabled)
    if memory_enabled is not None:
        fields["memory_enabled"] = bool(memory_enabled)
    if not fields:
        return 0

    now = _now_iso()
    updated = 0
    items = _query_all(
        _tasks(),
        KeyConditionExpression=Key("user_id").eq(user_id),
        ProjectionExpression="task_id",
        ConsistentRead=True,
    )
    for item in items:
        if _update_task_fields(user_id, item["task_id"], fields, now=now):
            updated += 1
    return updated


def update_all_task_flags(
    user_id: str,
    *,
    guardrail_enabled: bool | None = None,
    memory_enabled: bool | None = None,
) -> int:
    """Backward-compatible alias for flag-only updates."""
    return update_all_tasks(
        user_id,
        guardrail_enabled=guardrail_enabled,
        memory_enabled=memory_enabled,
    )


def update_task(task_id: str, user_id: str, **fields: Any) -> dict[str, Any] | None:
    patch: dict[str, Any] = {}
    for key in ("title", "model_name"):
        if fields.get(key) is not None:
            patch[key] = fields[key]
    for key in ("guardrail_enabled", "llm_gateway_enabled", "memory_enabled", "pinned"):
        if fields.get(key) is not None:
            patch[key] = bool(fields[key])
    for key in ("skills", "mcp_servers"):
        if fields.get(key) is not None:
            patch[key] = list(fields[key])

    if patch:
        _update_task_fields(user_id, task_id, patch)
    return get_task(task_id, user_id)


def delete_task(task_id: str, user_id: str) -> bool:
    try:
        resp = _tasks().delete_item(
            Key={"user_id": user_id, "task_id": task_id},
            ConditionExpression="attribute_exists(task_id)",
            ReturnValues="ALL_OLD",
        )
    except ClientError as exc:
        if _is_conditional_failure(exc):
            return False
        raise
    if not resp.get("Attributes"):
        return False

    # Task row is gone first, so the task disappears from the UI immediately;
    # then remove its messages.
    keys = _query_all(
        _messages(),
        KeyConditionExpression=Key("task_id").eq(task_id),
        ProjectionExpression="task_id, message_id",
        ConsistentRead=True,
    )
    with _messages().batch_writer() as batch:
        for key in keys:
            batch.delete_item(Key={"task_id": key["task_id"], "message_id": key["message_id"]})
    return True


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def _message_items(task_id: str, user_id: str) -> list[dict[str, Any]]:
    items = _query_all(
        _messages(),
        IndexName=_MESSAGES_BY_CREATED_INDEX,
        KeyConditionExpression=Key("task_id").eq(task_id),
        ScanIndexForward=True,
        ConsistentRead=True,
    )
    return [i for i in items if i.get("user_id") in (None, user_id)]


def list_messages(task_id: str, user_id: str) -> list[dict[str, Any]]:
    return [_item_to_message(i) for i in _message_items(task_id, user_id)]


def add_message(
    task_id: str,
    role: str,
    content: str,
    *,
    user_id: str,
    images: list[str] | None = None,
    tool_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    now = _now_iso()
    content, images_json, events_json = _fit_payload(
        content, images or [], tool_events or []
    )
    item = {
        "task_id": task_id,
        "message_id": str(uuid.uuid4()),
        "user_id": user_id,
        "role": role,
        "content": content,
        "images_json": images_json,
        "tool_events_json": events_json,
        "created_at": now,
    }
    _messages().put_item(Item=item, ConditionExpression="attribute_not_exists(message_id)")
    _touch_task(user_id, task_id, updated_at=now)
    if role == "user":
        title = content.strip()[:50]
        if title:
            _set_title_if_default(user_id, task_id, title)
    return _item_to_message(item)


# ---------------------------------------------------------------------------
# Graph pipeline support
# ---------------------------------------------------------------------------


def get_message_fingerprint(user_id: str) -> dict[str, Any]:
    """Return ``{message_count, max_created_at}`` across the user's messages."""
    tasks = _query_all(
        _tasks(),
        KeyConditionExpression=Key("user_id").eq(user_id),
        ProjectionExpression="task_id, message_count",
        ConsistentRead=True,
    )
    message_count = 0
    max_created_at: str | None = None
    for task in tasks:
        message_count += int(task.get("message_count") or 0)
        resp = _messages().query(
            IndexName=_MESSAGES_BY_CREATED_INDEX,
            KeyConditionExpression=Key("task_id").eq(task["task_id"]),
            ScanIndexForward=False,
            Limit=1,
            ProjectionExpression="created_at",
            ConsistentRead=True,
        )
        for item in resp.get("Items", []):
            created = item.get("created_at")
            if created and (max_created_at is None or created > max_created_at):
                max_created_at = created
    return {"message_count": message_count, "max_created_at": max_created_at}


def export_user_sqlite(user_id: str, path: str) -> str:
    """Write the user's tasks/messages to a scratch SQLite file for ``graph/`` to read.

    The graph pipeline is a separate process that reads SQLite; this file is a
    throw-away export, never a source of truth.
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE tasks (
              id TEXT PRIMARY KEY, user_id TEXT NOT NULL, title TEXT,
              runtime_session_id TEXT, model_name TEXT, skills_json TEXT,
              mcp_servers_json TEXT, guardrail_enabled INTEGER,
              llm_gateway_enabled INTEGER, memory_enabled INTEGER, pinned INTEGER,
              created_at TEXT, updated_at TEXT
            );
            CREATE TABLE messages (
              id TEXT PRIMARY KEY, task_id TEXT NOT NULL, role TEXT NOT NULL,
              content TEXT, images_json TEXT, tool_events_json TEXT, created_at TEXT
            );
            """
        )
        for task in list_tasks(user_id, limit=10_000):
            conn.execute(
                "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    task["id"],
                    user_id,
                    task["title"],
                    task["runtime_session_id"],
                    task["model_name"],
                    json.dumps(task["skills"], ensure_ascii=False),
                    json.dumps(task["mcp_servers"], ensure_ascii=False),
                    int(task["guardrail_enabled"]),
                    int(task["llm_gateway_enabled"]),
                    int(task["memory_enabled"]),
                    int(task["pinned"]),
                    task["created_at"],
                    task["updated_at"],
                ),
            )
            for item in _message_items(task["id"], user_id):
                conn.execute(
                    "INSERT INTO messages VALUES (?,?,?,?,?,?,?)",
                    (
                        item["message_id"],
                        item["task_id"],
                        item["role"],
                        item.get("content") or "",
                        item.get("images_json") or "[]",
                        item.get("tool_events_json") or "[]",
                        item.get("created_at"),
                    ),
                )
        conn.commit()
    finally:
        conn.close()
    return path
