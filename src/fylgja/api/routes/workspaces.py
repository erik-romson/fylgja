"""``GET /workspaces`` and ``GET /workspaces/{name}``: ``/now``'s ``workspaces[]``, and the manual
label writes ``PUT`` and ``DELETE /workspaces/{name}/label`` (WP4.2, ``PLAN-final-plan.md`` §4.2).

The list is empty while no collector that reports windows is available. The two label writes are
mutating routes, so the mutation guard (bearer, Origin, JSON, body limit) has passed before any
code here runs.

* ``PUT /workspaces/{name}/label``: ``{"label": "..."}``, non-empty and at most 80 characters after
  whitespace is collapsed. A manual label beats the inferred one until it is cleared. Answers
  **200** with the workspace as ``/now`` shows it; **404** for a workspace that is not open.
* ``DELETE /workspaces/{name}/label``: clears it, so the inferred label shows again. Clearing a
  workspace without a manual label is fine. Answers **200** with the workspace; **404** as above.

A body that is not an object with exactly the ``label`` string is **422**; the answer never repeats
the label.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, ValidationError

from fylgja.api.deps import current_now, daemon_of, json_response, not_found
from fylgja.api.now_models import Workspace, dump_json
from fylgja.core.labels import LabelValidationError, WorkspaceNotFound

__all__ = ["router"]

router = APIRouter()


@router.get("/workspaces")
async def list_workspaces(request: Request) -> Response:
    return json_response(dump_json(current_now(request).workspaces or [], kind=Workspace))


@router.get("/workspaces/{name}")
async def get_workspace(request: Request, name: str) -> Response:
    for workspace in current_now(request).workspaces or []:
        if workspace.name == name:
            return json_response(dump_json(workspace))
    raise not_found("workspace")


class LabelBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str


def _workspace_json(request: Request, name: str) -> Response:
    """The workspace as ``/now`` shows it right now."""
    for workspace in current_now(request).workspaces or []:
        if workspace.name == name:
            return json_response(dump_json(workspace))
    raise not_found("workspace")


@router.put("/workspaces/{name}/label")
async def put_label(request: Request, name: str) -> Response:
    try:
        body = LabelBody.model_validate_json(await request.body())
    except ValidationError:
        raise HTTPException(status_code=422, detail="invalid body (check: label)") from None
    try:
        await daemon_of(request).labels.set_manual(name, body.label)
    except WorkspaceNotFound:
        raise not_found("workspace") from None
    except LabelValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    return _workspace_json(request, name)


@router.delete("/workspaces/{name}/label")
async def delete_label(request: Request, name: str) -> Response:
    try:
        await daemon_of(request).labels.clear_manual(name)
    except WorkspaceNotFound:
        raise not_found("workspace") from None
    return _workspace_json(request, name)
