"""Lookup for the per-type fields of workflow task ``task_params``.

The workflow task write tools take ``task_params``, a union of 153 task types.
Inlined, that union made create_workflow_task and update_workflow_task about
32k tokens each in every ``tools/list``. ``spec_transform.compact_large_unions``
reduces it to the ``task_type`` enum; this tool serves one type's fields, from
the bundled spec, with no API call.
"""

from __future__ import annotations

import difflib
from typing import Annotated, Any

from mcp.types import ToolAnnotations
from pydantic import Field

from ..spec_transform import WORKFLOW_TASK_PARAMS_SCHEMA_TOOL

JsonDict = dict[str, Any]


def register_workflow_task_tools(mcp: Any, task_params_variants: dict[str, JsonDict]) -> None:
    """Register the task_params lookup when the union was compacted."""
    if not task_params_variants:
        return
    task_types = sorted(task_params_variants)

    @mcp.tool(
        name=WORKFLOW_TASK_PARAMS_SCHEMA_TOOL,
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )
    async def get_workflow_task_params_schema(
        task_type: Annotated[
            str,
            Field(
                description=(
                    "Workflow task type, e.g. 'send_slack_message' or 'create_jira_issue'. "
                    "The task_type enum on create_workflow_task lists every value."
                )
            ),
        ],
    ) -> JsonDict:
        """Get the task_params fields for one workflow task type.

        Call this before create_workflow_task or update_workflow_task: their
        task_params schema lists only the task types, not each type's fields.
        Pass the result's fields inside data.attributes.task_params, together
        with task_type.
        """
        key = task_type.strip().lower()
        schema = task_params_variants.get(key)
        if schema is None:
            close = difflib.get_close_matches(key, task_types, n=5, cutoff=0.5)
            return {
                "error": True,
                "error_type": "validation_error",
                "message": (
                    f"Unknown task_type {task_type!r}."
                    + (f" Did you mean: {', '.join(close)}?" if close else "")
                ),
                "task_types": task_types,
            }
        return {
            "task_type": key,
            "required": list(schema.get("required") or []),
            "fields": schema.get("properties") or {},
        }
