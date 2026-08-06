from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from application.api.routes_auth import require_user_id
from application import task_store

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


class TaskCreate(BaseModel):
    model_name: str | None = None
    skills: list[str] | None = None
    mcp_servers: list[str] | None = None
    guardrail_enabled: bool = False
    llm_gateway_enabled: bool = False
    memory_enabled: bool = True
    title: str = "New task"


class TaskPatch(BaseModel):
    title: str | None = None
    model_name: str | None = None
    skills: list[str] | None = None
    mcp_servers: list[str] | None = None
    guardrail_enabled: bool | None = None
    llm_gateway_enabled: bool | None = None
    memory_enabled: bool | None = None
    pinned: bool | None = None


@router.get("")
def list_tasks(request: Request, limit: int = 100):
    user_id = require_user_id(request)
    return {"tasks": task_store.list_tasks(user_id, limit=limit)}


@router.post("")
def create_task(body: TaskCreate, request: Request):
    user_id = require_user_id(request)
    task = task_store.create_task(
        user_id,
        model_name=body.model_name,
        skills=body.skills,
        mcp_servers=body.mcp_servers,
        guardrail_enabled=body.guardrail_enabled,
        llm_gateway_enabled=body.llm_gateway_enabled,
        memory_enabled=body.memory_enabled,
        title=body.title,
    )
    return task


@router.get("/{task_id}")
def get_task(task_id: str, request: Request):
    user_id = require_user_id(request)
    task = task_store.get_task(task_id, user_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.patch("/{task_id}")
def patch_task(task_id: str, body: TaskPatch, request: Request):
    user_id = require_user_id(request)
    task = task_store.get_task(task_id, user_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    patch = body.model_dump(exclude_unset=True)
    updated = task_store.update_task(task_id, user_id, **patch)
    if "skills" in patch or "mcp_servers" in patch:
        try:
            from application import utils

            utils.save_favorite_tools(
                skills=updated.get("skills") if updated else None,
                mcp_servers=updated.get("mcp_servers") if updated else None,
            )
        except Exception:
            pass
    return updated


@router.delete("/{task_id}")
def remove_task(task_id: str, request: Request):
    user_id = require_user_id(request)
    if not task_store.delete_task(task_id, user_id):
        raise HTTPException(status_code=404, detail="Task not found")
    return {"ok": True}


@router.get("/{task_id}/messages")
def get_messages(task_id: str, request: Request):
    user_id = require_user_id(request)
    task = task_store.get_task_refreshing(task_id, user_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"messages": task_store.list_messages(task_id)}
