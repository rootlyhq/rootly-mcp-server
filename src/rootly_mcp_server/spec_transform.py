"""Swagger/OpenAPI loading and filtering helpers for Rootly MCP server."""

from __future__ import annotations

import json
import logging
import os
import re
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import requests

from .utils import sanitize_parameter_name

logger = logging.getLogger(__name__)

# Default Swagger URL
SWAGGER_URL = "https://rootly-heroku.s3.amazonaws.com/swagger/v1/swagger.json"
_PATH_PARAM_PATTERN = re.compile(r"\{[^/{}]+\}")

# HTTP methods that denote an OpenAPI operation within a path item.
_OPENAPI_METHODS = frozenset({"get", "post", "put", "patch", "delete"})


def _normalize_path_template(path: str) -> str:
    """Normalize parameter names in path templates to support id-token variants."""
    return _PATH_PARAM_PATTERN.sub("{}", path)


def _is_single_resource_path(path: str) -> bool:
    """Whether *path* addresses one resource, i.e. its last segment is a `{param}`."""
    last_segment = path.rstrip("/").rsplit("/", 1)[-1]
    return bool(_PATH_PARAM_PATTERN.fullmatch(last_segment))


def _singular_resource_name(segment: str) -> str:
    """Best-effort human name for a collection path segment (`alert_sources` -> `alert source`)."""
    words = segment.replace("-", "_").split("_")
    last = words[-1]
    if last.endswith("ies") and len(last) > 3:
        last = last[:-3] + "y"
    elif last.endswith(("sses", "shes", "ches", "xes", "uses")):
        last = last[:-2]
    elif last.endswith("s") and not last.endswith("ss"):
        last = last[:-1]
    words[-1] = last
    return " ".join(word for word in words if word)


def _path_parameter_description(path: str, param_name: str) -> str:
    """Describe a path parameter so the model knows exactly which argument fills it.

    Models routinely call `get_alert(alert_id=...)` or `get_team(team_id=...)`
    when the URL template is `/v1/alerts/{id}` / `/v1/teams/{id}`: the bare `id`
    argument carries no description, so the model guesses a "nicer" name, the
    placeholder stays unfilled and the call fails (ROOTLY-MCP-7S). Spelling out
    the resource and the exact argument name removes the ambiguity.
    """
    segments = [segment for segment in path.split("/") if segment]
    placeholder = "{" + param_name + "}"
    resource = ""
    if placeholder in segments:
        index = segments.index(placeholder)
        if index > 0 and not _PATH_PARAM_PATTERN.fullmatch(segments[index - 1]):
            resource = _singular_resource_name(segments[index - 1])
    if param_name == "id" and resource:
        alias = resource.replace(" ", "_") + "_id"
        return (
            f"Required. ID of the {resource} (fills `{{id}}` in {path}). "
            f"Pass it as `id` -- this tool has no `{alias}` argument."
        )
    if param_name.endswith("_id"):
        subject = param_name[: -len("_id")].replace("_", " ")
        return f"Required. ID of the {subject} (fills `{placeholder}` in {path})."
    return f"Required. Value for `{placeholder}` in {path}."


def _describe_path_parameters(paths: dict[str, Any]) -> None:
    """Give every undocumented `in: path` parameter an explicit description, in place."""
    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        parameter_lists = [path_item.get("parameters")]
        parameter_lists.extend(
            operation.get("parameters")
            for method, operation in path_item.items()
            if method.lower() in _OPENAPI_METHODS and isinstance(operation, dict)
        )
        for parameters in parameter_lists:
            if not isinstance(parameters, list):
                continue
            for param in parameters:
                if not isinstance(param, dict) or param.get("in") != "path":
                    continue
                if param.get("description") or not param.get("name"):
                    continue
                param["description"] = _path_parameter_description(path, param["name"])


# camelCase / PascalCase boundary detection for operationId normalization.
_SNAKE_BOUNDARY_1 = re.compile(r"(.)([A-Z][a-z]+)")
_SNAKE_BOUNDARY_2 = re.compile(r"([a-z0-9])([A-Z])")


def to_snake_case(name: str) -> str:
    """Convert a camelCase/PascalCase identifier to snake_case.

    Examples: ``getIncident`` -> ``get_incident``, ``ListWorkflowRuns`` ->
    ``list_workflow_runs``, ``listAlertsSources`` -> ``list_alerts_sources``.
    Already-snake_case names pass through unchanged.
    """
    s = _SNAKE_BOUNDARY_1.sub(r"\1_\2", name)
    s = _SNAKE_BOUNDARY_2.sub(r"\1_\2", s)
    return s.lower()


def snakecase_operation_ids(spec: dict[str, Any]) -> dict[str, str]:
    """Rewrite every ``operationId`` in ``spec.paths`` to snake_case in place.

    Tool names are derived verbatim from operationIds by FastMCP's OpenAPI
    integration, so normalizing the operationIds here makes the entire autogen
    tool surface snake_case. Returns the mapping of original ``camelCase`` name
    to its ``snake_case`` form for every operationId that changed — callers use
    this to register deprecated-name aliases.
    """
    paths = spec.get("paths", {})
    mapping: dict[str, str] = {}
    for path_item in paths.values():
        if not isinstance(path_item, dict):
            continue
        for method, op in path_item.items():
            if method.lower() not in _OPENAPI_METHODS:
                continue
            if not isinstance(op, dict):
                continue
            op_id = op.get("operationId")
            if not op_id:
                continue
            snake = to_snake_case(op_id)
            if snake != op_id:
                op["operationId"] = snake
                mapping[op_id] = snake
    return mapping


_DEPRECATION_PREFIX = re.compile(
    r"^\s*(?:\[deprecated\]|\*\*deprecated:?\*\*:?|deprecated[.:])\s*", re.IGNORECASE
)


def deprecated_operation_guidance(spec: dict[str, Any]) -> dict[str, str]:
    """Map each operation the API marks deprecated to what replaces it.

    `_filter_openapi_spec` never makes these tools, so a client with a cached
    tool list that still calls one would only hear "Unknown tool". The spec's
    own description names the replacement; this keeps it so the server can say
    so. Run after `snakecase_operation_ids`: keys are the tool names.
    """
    guidance: dict[str, str] = {}
    for path_item in spec.get("paths", {}).values():
        if not isinstance(path_item, dict):
            continue
        for method, op in path_item.items():
            if method.lower() not in _OPENAPI_METHODS or not isinstance(op, dict):
                continue
            op_id = op.get("operationId")
            if not op_id or op.get("deprecated") is not True:
                continue
            description = _DEPRECATION_PREFIX.sub("", op.get("description") or "")
            # The first sentence names the replacement; the rest restates the
            # operation ("List Custom fields").
            first_line = description.strip().split("\n", 1)[0]
            sentence, dot, _ = first_line.partition(". ")
            guidance[op_id] = (sentence + dot).strip()
    return guidance


def _load_swagger_spec(swagger_path: str | None = None) -> dict[str, Any]:
    """
    Load the Swagger specification from a file or URL.

    Args:
        swagger_path: Path to the Swagger JSON file. If None, will fetch from URL.

    Returns:
        The Swagger specification as a dictionary.
    """
    if swagger_path:
        # Use the provided path
        logger.info(f"Using provided Swagger path: {swagger_path}")
        if not os.path.isfile(swagger_path):
            raise FileNotFoundError(f"Swagger file not found at {swagger_path}")
        with open(swagger_path, encoding="utf-8") as f:
            return cast(dict[str, Any], json.load(f))
    else:
        # First, check in the package data directory
        try:
            package_data_path = Path(__file__).parent / "data" / "swagger.json"
            if package_data_path.is_file():
                logger.info(f"Found Swagger file in package data: {package_data_path}")
                with open(package_data_path, encoding="utf-8") as f:
                    return cast(dict[str, Any], json.load(f))
        except Exception as e:
            logger.debug(f"Could not load Swagger file from package data: {e}")

        # Then, look for swagger.json in the current directory and parent directories
        logger.info("Looking for swagger.json in current directory and parent directories")
        current_dir = Path.cwd()

        # Check current directory first
        local_swagger_path = current_dir / "swagger.json"
        if local_swagger_path.is_file():
            logger.info(f"Found Swagger file at {local_swagger_path}")
            with open(local_swagger_path, encoding="utf-8") as f:
                return cast(dict[str, Any], json.load(f))

        # Check parent directories
        for parent in current_dir.parents:
            parent_swagger_path = parent / "swagger.json"
            if parent_swagger_path.is_file():
                logger.info(f"Found Swagger file at {parent_swagger_path}")
                with open(parent_swagger_path, encoding="utf-8") as f:
                    return cast(dict[str, Any], json.load(f))

        # If the file wasn't found, fetch it from the URL and save it
        logger.info("Swagger file not found locally, fetching from URL")
        swagger_spec = _fetch_swagger_from_url()

        # Save the fetched spec to the current directory
        save_swagger_path = current_dir / "swagger.json"
        logger.info(f"Saving Swagger file to {save_swagger_path}")
        try:
            with open(save_swagger_path, "w", encoding="utf-8") as f:
                json.dump(swagger_spec, f)
            logger.info(f"Saved Swagger file to {save_swagger_path}")
        except Exception as e:
            logger.warning(f"Failed to save Swagger file: {e}")

        return swagger_spec


def _fetch_swagger_from_url(url: str = SWAGGER_URL) -> dict[str, Any]:
    """
    Fetch the Swagger specification from the specified URL.

    Args:
        url: URL of the Swagger JSON file.

    Returns:
        The Swagger specification as a dictionary.
    """
    logger.info(f"Fetching Swagger specification from {url}")
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        return cast(dict[str, Any], response.json())
    except requests.RequestException as e:
        logger.error(f"Failed to fetch Swagger spec: {e}")
        raise Exception(f"Failed to fetch Swagger specification: {e}")
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse Swagger spec: {e}")
        raise Exception(f"Failed to parse Swagger specification: {e}")


_AI_AUTOMATION = (
    "An AI automation is a saved instruction (prompt) that a Rootly AI agent runs "
    "on a schedule, on a matching event (incident, alert, Slack message, webhook) "
    "or on demand. When it runs, the agent may only use the tools listed in the "
    "automation's `allowed_tool_keys` (all team-allowed tools when null), and it "
    "posts its answer to the configured destination (a Slack channel or the "
    "incident timeline), or nowhere when `destination_kind` is none."
)
_AI_SKILL = (
    "An AI skill is reusable written guidance (markdown `body`) that Rootly AI "
    "agents, users and AI automations can load while they work. Skills keep "
    "saved versions, readable with list_ai_skill_versions."
)

# Tool descriptions for operations whose spec text says only which OAuth scope
# they need. Keyed by snake_case operationId. The spec's own description is kept
# after this text, so scope, rate-limit and visibility notes still reach the model.
_OPERATION_DESCRIPTIONS: dict[str, str] = {
    "list_ai_automations": (
        "List the team's AI automations: name, prompt, triggers, destination, "
        "allowed tools, run limits and whether each is enabled. Read-only. " + _AI_AUTOMATION
    ),
    "get_ai_automation": (
        "Get one AI automation by id: its prompt, triggers, destination, allowed "
        "tools, attached skills and run limits. Read-only. " + _AI_AUTOMATION
    ),
    "create_ai_automation": (
        "Create an AI automation. Set `name` and `prompt`; `triggers` decide when "
        "it runs, `destination_kind` where its answer is posted, and "
        "`allowed_tool_keys` which tools the agent may use (null means every tool "
        "the team allows, so pass an explicit list to keep it narrow). New "
        "automations are disabled unless `enabled` is true. " + _AI_AUTOMATION
    ),
    "update_ai_automation": (
        "Change an existing AI automation. Only the attributes you send change; "
        "`triggers`, when sent, replace the automation's triggers. Changing the "
        "prompt, allowed tools or destination changes what every future run does, "
        "and the previous values are not kept. " + _AI_AUTOMATION
    ),
    "run_ai_automation": (
        "Run an existing AI automation now, once. The run executes that "
        "automation's saved prompt with only the tools in its `allowed_tool_keys`, "
        "and posts the answer to its configured destination; it cannot run "
        "arbitrary instructions. Pass `incident_id` to run it against an incident, "
        "or `run_id` to re-run a previous run. "
        "Returns the run; poll get_ai_automation_run for its status and output. " + _AI_AUTOMATION
    ),
    "get_ai_automation_run": (
        "Get one AI automation run by id: its status, timing, what triggered it, "
        "the linked incident if any, and the agent's output. Read-only."
    ),
    "list_ai_automation_runs": (
        "List recent runs of one AI automation with status, trigger and timing. Read-only."
    ),
    "list_ai_skills": (
        "List the team's AI skills: name, slug, description, who may invoke each "
        "and whether it is enabled. Read-only. " + _AI_SKILL
    ),
    "get_ai_skill": (
        "Get one AI skill by id, including its current `body`. Read-only. " + _AI_SKILL
    ),
    "create_ai_skill": (
        "Create an AI skill from a `name`, `description` and markdown `body`. "
        "`invocable_by` controls who may load it: the model, users, automations. " + _AI_SKILL
    ),
    "update_ai_skill": (
        "Change an AI skill. Only the attributes you send change, and the change "
        "applies to every agent and automation that loads the skill. " + _AI_SKILL
    ),
    "list_ai_skill_versions": (
        "List the saved versions of one AI skill, newest first. Read-only. " + _AI_SKILL
    ),
    "get_ai_skill_version": (
        "Get one saved version of an AI skill, including the `body` it had at "
        "that version. Read-only. " + _AI_SKILL
    ),
    "update_incident_sub_status": (
        "Correct one entry in an incident's sub-status history: which sub-status "
        "it records (`sub_status_id`), when it was assigned (`assigned_at`) or by "
        "whom (`assigned_by_user_id`). The `id` is the history entry's id, not "
        "the incident's. This edits the record only; it does not move the "
        "incident to another sub-status."
    ),
    "create_incident_action_item": (
        "Add a follow-up action item to an incident: a `summary`, optional "
        "`description`, `kind`, `priority`, `status`, `due_date` and assignee "
        "(`assigned_to_user_id` or `assigned_to_group_ids`). It can link an "
        "existing issue (Jira by `jira_issue_key` or `jira_issue_url`, Linear by "
        "`linear_issue_url`); it does not create one."
    ),
    "create_workflow_run": (
        "Run a Rootly workflow now. The workflow executes its configured tasks "
        "(for example posting Slack messages, creating tickets, or changing the "
        "incident), which can act on other systems and cannot be undone by this "
        "server. Pass the record to run it against (such as `incident_id`) in "
        "the request attributes; list_workflow_tasks shows what the workflow does."
    ),
    "create_workflow_task": (
        "Add a task to a Rootly workflow. Set `task_params.task_type` to the "
        "action (for example send_slack_message or create_jira_issue) and the "
        "parameters that action needs; get_workflow_task_params_schema returns "
        "them for a task_type. The task runs whenever the workflow runs."
    ),
    "update_workflow_task": (
        "Change a task in a Rootly workflow: its name, position, enabled state "
        "or `task_params`. The previous values are not kept, and the change "
        "applies to every future run of the workflow. "
        "get_workflow_task_params_schema returns the parameters for a task_type."
    ),
    "list_alert_routes": (
        "List the team's alert routes: the routing rules that decide where each "
        "incoming alert goes. To find the route that routed an alert, take the "
        "rule ids from the alert's `data.routing_rules[].id` (get_alert returns "
        "them; list_alerts does not) and find the route whose `rules[].id` "
        "contains one. Pages hold up to 100 routes, so page through on larger "
        "accounts. Editing a route recreates its rules with new ids, so alerts "
        "routed before the last edit may match no current rule."
    ),
    "get_alert_route": (
        "Get one alert route by its route id. A rule id from an alert's "
        "`data.routing_rules[].id` is not a route id; to go from a rule id to "
        "its route, use list_alert_routes and match `rules[].id`."
    ),
}


def _describe_operations(paths: dict[str, Any]) -> None:
    """Lead each operation in `_OPERATION_DESCRIPTIONS` with its written description."""
    for path_item in paths.values():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.lower() not in _OPENAPI_METHODS or not isinstance(operation, dict):
                continue
            text = _OPERATION_DESCRIPTIONS.get(to_snake_case(operation.get("operationId") or ""))
            if text is None:
                continue
            original = (operation.get("description") or "").strip()
            operation["description"] = f"{text}\n\n{original}" if original else text


def _drop_duplicate_update_verb(path_item: dict[str, Any]) -> None:
    """Drop PUT when a path documents PATCH as the update and PUT separately.

    The ``/v1/ai/*`` resources publish ``Update…`` (PATCH) and ``Put…`` (PUT)
    for the same update, which would surface as two tools for one action
    (``update_ai_skill`` and ``put_ai_skill``). Paths where PUT carries the
    ``update…`` name (``/v1/alert_routes/{id}``) are left as they are, so no
    existing tool disappears.
    """

    def _is_update(method: str) -> bool:
        operation_id = str(path_item[method].get("operationId", ""))
        return operation_id.lower().startswith("update")

    if _is_update("patch") and not _is_update("put"):
        path_item.pop("put")


def _filter_openapi_spec(
    spec: dict[str, Any],
    allowed_paths: list[str],
    delete_allowed_paths: list[str] | None = None,
    write_allowed_paths: list[str] | None = None,
    enable_write_tools: bool = True,
    enabled_operation_ids: set[str] | None = None,
) -> dict[str, Any]:
    """
    Filter an OpenAPI specification to only include specified paths and clean up schema references.

    Args:
        spec: The original OpenAPI specification.
        allowed_paths: List of paths to include.
        delete_allowed_paths: Path templates where DELETE operations are allowed.
        write_allowed_paths: Path templates where POST/PUT/PATCH are allowed.
        enable_write_tools: Whether non-destructive write operations are exposed.
        enabled_operation_ids: Optional allowlist of OpenAPI operationIds to expose.

    Returns:
        A filtered OpenAPI specification with cleaned schema references.
    """
    # Use deepcopy to ensure all nested structures are properly copied
    filtered_spec = deepcopy(spec)

    # Filter paths
    original_paths = filtered_spec.get("paths", {})
    allowed_path_set = set(allowed_paths)
    allowed_normalized_paths = {_normalize_path_template(path) for path in allowed_paths}

    filtered_paths = {}
    for path, path_item in original_paths.items():
        if path in allowed_path_set:
            filtered_paths[path] = path_item
            continue

        # Fallback for cases where OpenAPI uses {id} while allowlist uses resource-specific ids.
        if _normalize_path_template(path) in allowed_normalized_paths:
            filtered_paths[path] = path_item

    filtered_spec["paths"] = filtered_paths

    # Safety policy: only expose DELETE operations for explicitly allowed paths.
    # This keeps high-blast-radius destructive actions out of the default tool surface.
    delete_allowed_set = set(delete_allowed_paths or [])
    delete_allowed_normalized_paths = {
        _normalize_path_template(path) for path in delete_allowed_set
    }
    write_allowed_set = set(write_allowed_paths or allowed_paths)
    write_allowed_normalized_paths = {_normalize_path_template(path) for path in write_allowed_set}
    paths_to_remove: list[str] = []
    for path, path_item in filtered_paths.items():
        allow_write = enable_write_tools and (
            path in write_allowed_set
            or _normalize_path_template(path) in write_allowed_normalized_paths
        )
        if not allow_write:
            path_item.pop("post", None)
            path_item.pop("put", None)
            path_item.pop("patch", None)
        elif "patch" in path_item and "put" in path_item:
            _drop_duplicate_update_verb(path_item)

        allow_delete = path in delete_allowed_set or (
            _normalize_path_template(path) in delete_allowed_normalized_paths
        )
        if not allow_delete:
            path_item.pop("delete", None)
        # Operations the API marks deprecated are never tools: they cost every
        # client context for endpoints with a supported replacement, and an
        # allowlist entry or a spec refresh must not bring them back.
        for method in [
            method
            for method, operation in path_item.items()
            if method.lower() in _OPENAPI_METHODS
            and isinstance(operation, dict)
            and operation.get("deprecated") is True
        ]:
            path_item.pop(method)
        if not any(method.lower() in _OPENAPI_METHODS for method in path_item):
            paths_to_remove.append(path)
    for path in paths_to_remove:
        del filtered_paths[path]

    if enabled_operation_ids is not None:
        paths_to_remove = []
        for path, path_item in filtered_paths.items():
            methods_to_remove: list[str] = []
            for method, operation in path_item.items():
                if method.lower() not in _OPENAPI_METHODS:
                    continue
                operation_id = operation.get("operationId")
                if operation_id not in enabled_operation_ids:
                    methods_to_remove.append(method)
            for method in methods_to_remove:
                path_item.pop(method, None)
            if not any(method.lower() in _OPENAPI_METHODS for method in path_item):
                paths_to_remove.append(path)
        for path in paths_to_remove:
            del filtered_paths[path]

    _describe_operations(filtered_paths)

    # Clean up schema references that might be broken
    # Remove problematic schema references from request bodies and parameters
    for path, path_item in filtered_paths.items():
        for method, operation in path_item.items():
            if method.lower() not in ["get", "post", "put", "delete", "patch"]:
                continue

            # Clean request body schemas
            if "requestBody" in operation:
                request_body = operation["requestBody"]
                if "content" in request_body:
                    for _content_type, content_info in request_body["content"].items():
                        if "schema" in content_info:
                            schema = content_info["schema"]
                            # Remove problematic $ref references
                            if "$ref" in schema and "incident_trigger_params" in schema["$ref"]:
                                # Replace with a generic object schema
                                content_info["schema"] = {
                                    "type": "object",
                                    "description": "Request parameters for this endpoint",
                                    "additionalProperties": True,
                                }

            # Remove response schemas to avoid validation issues
            # FastMCP will still return the data, just without strict validation
            if "responses" in operation:
                for _status_code, response in operation["responses"].items():
                    if "content" in response:
                        for _content_type, content_info in response["content"].items():
                            if "schema" in content_info:
                                # Replace with a simple schema that accepts any response
                                content_info["schema"] = {
                                    "type": "object",
                                    "additionalProperties": True,
                                }

            # Clean parameter schemas (parameter names are already sanitized)
            if "parameters" in operation:
                for param in operation["parameters"]:
                    if "schema" in param and "$ref" in param["schema"]:
                        ref_path = param["schema"]["$ref"]
                        if "incident_trigger_params" in ref_path:
                            # Replace with a simple string schema
                            param["schema"] = {
                                "type": "string",
                                "description": param.get("description", "Parameter value"),
                            }

            # Add/modify pagination limits to alerts and incident-related endpoints to prevent infinite loops
            if method.lower() == "get" and ("alerts" in path.lower() or "incident" in path.lower()):
                if "parameters" not in operation:
                    operation["parameters"] = []

                # Pagination is meaningless on a single-resource path such as
                # `/v1/alerts/{id}`: advertising page/filter arguments there makes the
                # tool look like a list endpoint, and models then call it without the
                # `{id}` path argument (ROOTLY-MCP-7S).
                single_resource = _is_single_resource_path(path)
                if not single_resource:
                    # Find existing pagination parameters and update them with limits
                    page_size_param = None
                    page_number_param = None

                    for param in operation["parameters"]:
                        if param.get("name") == "page[size]":
                            page_size_param = param
                        elif param.get("name") == "page[number]":
                            page_number_param = param

                    # Update or add page[size] parameter with limits
                    if page_size_param:
                        # Update existing parameter with limits
                        if "schema" not in page_size_param:
                            page_size_param["schema"] = {}
                        page_size_param["schema"].update(
                            {
                                "type": "integer",
                                "default": 10,
                                "minimum": 1,
                                "maximum": 20,
                                "description": "Number of results per page (max: 20)",
                            }
                        )
                    else:
                        # Add new parameter
                        operation["parameters"].append(
                            {
                                "name": "page[size]",
                                "in": "query",
                                "required": False,
                                "schema": {
                                    "type": "integer",
                                    "default": 10,
                                    "minimum": 1,
                                    "maximum": 20,
                                    "description": "Number of results per page (max: 20)",
                                },
                            }
                        )

                    # Update or add page[number] parameter with defaults
                    if page_number_param:
                        # Update existing parameter
                        if "schema" not in page_number_param:
                            page_number_param["schema"] = {}
                        page_number_param["schema"].update(
                            {
                                "type": "integer",
                                "default": 1,
                                "minimum": 1,
                                "description": "Page number to retrieve",
                            }
                        )
                    else:
                        # Add new parameter
                        operation["parameters"].append(
                            {
                                "name": "page[number]",
                                "in": "query",
                                "required": False,
                                "schema": {
                                    "type": "integer",
                                    "default": 1,
                                    "minimum": 1,
                                    "description": "Page number to retrieve",
                                },
                            }
                        )

                # Add sparse fieldsets for alerts endpoints to reduce payload size
                if "alert" in path.lower():
                    # Add fields[alerts] parameter with essential fields only - make it required with default
                    operation["parameters"].append(
                        {
                            "name": "fields[alerts]",
                            "in": "query",
                            "required": True,
                            "schema": {
                                "type": "string",
                                "default": "id,summary,status,started_at,ended_at,short_id,alert_urgency_id,source,noise",
                                "description": "Comma-separated list of alert fields to include (reduces payload size)",
                            },
                        }
                    )

                # Add include parameter for alerts endpoints to minimize relationships
                if "alert" in path.lower():
                    # Check if include parameter already exists
                    include_param_exists = any(
                        param.get("name") == "include" for param in operation["parameters"]
                    )
                    if not include_param_exists:
                        operation["parameters"].append(
                            {
                                "name": "include",
                                "in": "query",
                                "required": True,
                                "schema": {
                                    "type": "string",
                                    "default": "",
                                    "description": "Related resources to include (empty for minimal payload)",
                                },
                            }
                        )

                # Add filter parameters for alerts endpoints to enable team/date/source filtering
                if "alert" in path.lower() and not single_resource:
                    filter_params = [
                        {
                            "name": "filter[status]",
                            "description": "Filter by alert status (e.g., triggered, acknowledged, resolved)",
                        },
                        {
                            "name": "filter[groups]",
                            "description": "Filter by team/group IDs (comma-separated)",
                        },
                        {
                            "name": "filter[services]",
                            "description": "Filter by service IDs (comma-separated)",
                        },
                        {
                            "name": "filter[environments]",
                            "description": "Filter by environment IDs (comma-separated)",
                        },
                        {
                            "name": "filter[labels]",
                            "description": "Filter by labels (comma-separated)",
                        },
                        {"name": "filter[source]", "description": "Filter by alert source"},
                        {
                            "name": "filter[started_at][gte]",
                            "description": "Started at >= (ISO8601 datetime)",
                        },
                        {
                            "name": "filter[started_at][lte]",
                            "description": "Started at <= (ISO8601 datetime)",
                        },
                        {
                            "name": "filter[started_at][gt]",
                            "description": "Started at > (ISO8601 datetime)",
                        },
                        {
                            "name": "filter[started_at][lt]",
                            "description": "Started at < (ISO8601 datetime)",
                        },
                        {
                            "name": "filter[ended_at][gte]",
                            "description": "Ended at >= (ISO8601 datetime)",
                        },
                        {
                            "name": "filter[ended_at][lte]",
                            "description": "Ended at <= (ISO8601 datetime)",
                        },
                        {
                            "name": "filter[ended_at][gt]",
                            "description": "Ended at > (ISO8601 datetime)",
                        },
                        {
                            "name": "filter[ended_at][lt]",
                            "description": "Ended at < (ISO8601 datetime)",
                        },
                        {
                            "name": "filter[created_at][gte]",
                            "description": "Created at >= (ISO8601 datetime)",
                        },
                        {
                            "name": "filter[created_at][lte]",
                            "description": "Created at <= (ISO8601 datetime)",
                        },
                        {
                            "name": "filter[created_at][gt]",
                            "description": "Created at > (ISO8601 datetime)",
                        },
                        {
                            "name": "filter[created_at][lt]",
                            "description": "Created at < (ISO8601 datetime)",
                        },
                    ]

                    for param_def in filter_params:
                        # Check if param already exists
                        param_exists = any(
                            p.get("name") == param_def["name"] for p in operation["parameters"]
                        )
                        if not param_exists:
                            operation["parameters"].append(
                                {
                                    "name": param_def["name"],
                                    "in": "query",
                                    "required": False,
                                    "schema": {
                                        "type": "string",
                                        "description": param_def["description"],
                                    },
                                }
                            )

                # Add sparse fieldsets for incidents endpoints to reduce payload size.
                # Optional with a default so LLMs aren't pushed to override it — the default
                # already produces a compact payload.
                if "incident" in path.lower():
                    operation["parameters"].append(
                        {
                            "name": "fields[incidents]",
                            "in": "query",
                            "required": False,
                            "schema": {
                                "type": "string",
                                "default": "id,title,summary,status,severity,created_at,updated_at,url,started_at",
                                "description": "Comma-separated list of incident fields to include (reduces payload size)",
                            },
                        }
                    )

                # Add include parameter for incidents endpoints to minimize relationships.
                # Optional with an empty default — relationships are heavy and rarely needed.
                if "incident" in path.lower():
                    include_param_exists = any(
                        param.get("name") == "include" for param in operation["parameters"]
                    )
                    if not include_param_exists:
                        operation["parameters"].append(
                            {
                                "name": "include",
                                "in": "query",
                                "required": False,
                                "schema": {
                                    "type": "string",
                                    "default": "",
                                    "description": "Related resources to include (empty for minimal payload)",
                                },
                            }
                        )

    # Also clean up any remaining broken references in components.
    # Schemas with broken $refs are patched in-place (broken refs replaced with generic objects)
    # rather than removed, so that write tools like createWorkflow retain their field structure.
    if "components" in filtered_spec and "schemas" in filtered_spec["components"]:
        schemas = filtered_spec["components"]["schemas"]
        schemas_to_remove = []
        for schema_name, schema_def in schemas.items():
            if isinstance(schema_def, dict) and _has_broken_references(schema_def):
                patched = _patch_broken_refs(schema_def)
                if _has_broken_references(patched):
                    # Still broken after patching — remove it entirely
                    schemas_to_remove.append(schema_name)
                else:
                    schemas[schema_name] = patched
                    logger.debug(f"Patched broken references in schema: {schema_name}")

        for schema_name in schemas_to_remove:
            logger.debug(f"Removing schema with broken references: {schema_name}")
            del schemas[schema_name]

    # Clean up operation-level references to schemas that were fully removed (not just patched)
    removed_schemas: set[str] = set()
    if "components" in filtered_spec and "schemas" in filtered_spec["components"]:
        existing = set(filtered_spec["components"]["schemas"].keys())
        removed_schemas = {
            "new_workflow",
            "update_workflow",
            "workflow",
            "workflow_task",
            "workflow_response",
            "workflow_list",
            "new_workflow_task",
            "update_workflow_task",
            "workflow_task_response",
            "workflow_task_list",
        } - existing  # Only replace refs to schemas that were actually removed

    for path, path_item in filtered_spec.get("paths", {}).items():
        for method, operation in path_item.items():
            if method.lower() not in ["get", "post", "put", "delete", "patch"]:
                continue

            # Clean request body references
            if "requestBody" in operation:
                request_body = operation["requestBody"]
                if "content" in request_body:
                    for _content_type, content_info in request_body["content"].items():
                        if "schema" in content_info and "$ref" in content_info["schema"]:
                            ref_path = content_info["schema"]["$ref"]
                            schema_name = ref_path.split("/")[-1]
                            if schema_name in removed_schemas:
                                # Replace with generic object schema
                                content_info["schema"] = {
                                    "type": "object",
                                    "description": "Request data for this endpoint",
                                    "additionalProperties": True,
                                }
                                logger.debug(
                                    f"Cleaned broken reference in {method.upper()} {path} request body: {ref_path}"
                                )

            # Clean response references
            if "responses" in operation:
                for _status_code, response in operation["responses"].items():
                    if "content" in response:
                        for _content_type, content_info in response["content"].items():
                            if "schema" in content_info and "$ref" in content_info["schema"]:
                                ref_path = content_info["schema"]["$ref"]
                                schema_name = ref_path.split("/")[-1]
                                if schema_name in removed_schemas:
                                    # Replace with generic object schema
                                    content_info["schema"] = {
                                        "type": "object",
                                        "description": "Response data from this endpoint",
                                        "additionalProperties": True,
                                    }
                                    logger.debug(
                                        f"Cleaned broken reference in {method.upper()} {path} response: {ref_path}"
                                    )

    _describe_path_parameters(filtered_spec.get("paths", {}))
    _ensure_array_items(filtered_spec)

    return filtered_spec


def _ensure_array_items(spec: Any) -> None:
    """Ensure all array schemas define an items schema to satisfy tool validation."""
    if isinstance(spec, dict):
        if spec.get("type") == "array" and "items" not in spec:
            # OpenAPI allows arrays without items, but MCP tool schema validation does not.
            spec["items"] = {}
        for value in spec.values():
            _ensure_array_items(value)
        return
    if isinstance(spec, list):
        for item in spec:
            _ensure_array_items(item)


def _walk_openapi_tree(node: Any, visitor: Any, path: str = "") -> None:
    """Walk an OpenAPI document and invoke a visitor with each node and its path."""
    visitor(node, path)
    if isinstance(node, dict):
        for key, value in node.items():
            child_path = f"{path}.{key}" if path else key
            _walk_openapi_tree(value, visitor, child_path)
        return
    if isinstance(node, list):
        for index, value in enumerate(node):
            _walk_openapi_tree(value, visitor, f"{path}[{index}]")


def collect_missing_array_items(spec: dict[str, Any]) -> list[str]:
    """Collect array schema paths that do not define an items schema."""
    missing: list[str] = []

    def visitor(node: Any, path: str) -> None:
        if isinstance(node, dict) and node.get("type") == "array" and "items" not in node:
            missing.append(path or "<root>")

    _walk_openapi_tree(spec, visitor)
    return missing


def collect_broken_internal_refs(spec: dict[str, Any]) -> list[dict[str, str]]:
    """Collect broken internal $ref pointers in an OpenAPI document."""
    broken: list[dict[str, str]] = []

    def visitor(node: Any, path: str) -> None:
        if not isinstance(node, dict):
            return
        ref = node.get("$ref")
        if not isinstance(ref, str) or not ref.startswith("#/"):
            return

        current: Any = spec
        for part in ref[2:].split("/"):
            if not isinstance(current, dict) or part not in current:
                broken.append({"path": path or "<root>", "ref": ref})
                return
            current = current[part]

    _walk_openapi_tree(spec, visitor)
    return broken


def collect_duplicate_operation_ids(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect duplicate OpenAPI operationIds."""
    counts: Counter[str] = Counter()
    for _path, path_item in spec.get("paths", {}).items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.lower() not in _OPENAPI_METHODS:
                continue
            if not isinstance(operation, dict):
                continue
            operation_id = operation.get("operationId")
            if isinstance(operation_id, str):
                counts[operation_id] += 1

    return [
        {"operationId": operation_id, "count": count}
        for operation_id, count in sorted(counts.items())
        if count > 1
    ]


def collect_sanitized_parameter_collisions(spec: dict[str, Any]) -> list[dict[str, str]]:
    """Collect cases where distinct parameter names sanitize to the same MCP key."""
    collisions: list[dict[str, str]] = []
    for path, path_item in spec.get("paths", {}).items():
        if not isinstance(path_item, dict):
            continue
        path_level_params = path_item.get("parameters", [])
        for method, operation in path_item.items():
            if method.lower() not in _OPENAPI_METHODS:
                continue
            if not isinstance(operation, dict):
                continue

            seen: dict[str, str] = {}
            parameters = list(path_level_params) + list(operation.get("parameters", []))
            for param in parameters:
                if not isinstance(param, dict):
                    continue
                name = param.get("name")
                if not isinstance(name, str):
                    continue
                sanitized = sanitize_parameter_name(name)
                previous = seen.get(sanitized)
                if previous and previous != name:
                    collisions.append(
                        {
                            "path": path,
                            "method": method.upper(),
                            "sanitized": sanitized,
                            "first": previous,
                            "second": name,
                        }
                    )
                else:
                    seen[sanitized] = name

    return collisions


def audit_openapi_spec(spec: dict[str, Any]) -> dict[str, list[Any]]:
    """Audit an OpenAPI document for schema issues that break MCP tool generation."""
    return {
        "missing_array_items": collect_missing_array_items(spec),
        "broken_internal_refs": collect_broken_internal_refs(spec),
        "duplicate_operation_ids": collect_duplicate_operation_ids(spec),
        "sanitized_parameter_collisions": collect_sanitized_parameter_collisions(spec),
    }


def has_openapi_audit_findings(findings: dict[str, list[Any]]) -> bool:
    """Return True when any audit category contains findings."""
    return any(findings.values())


def _has_broken_references(schema_def: dict[str, Any]) -> bool:
    """Check if a schema definition has broken references."""
    if "$ref" in schema_def:
        ref_path = schema_def["$ref"]
        # List of known broken references in the Rootly API spec
        broken_refs = [
            "incident_trigger_params",
            "new_workflow",
            "update_workflow",
            "workflow",
            "new_workflow_task",
            "update_workflow_task",
            "workflow_task",
            "workflow_task_response",
            "workflow_task_list",
            "workflow_response",
            "workflow_list",
            "workflow_custom_field_selection_response",
            "workflow_custom_field_selection_list",
            "workflow_form_field_condition_response",
            "workflow_form_field_condition_list",
            "workflow_group_response",
            "workflow_group_list",
            "workflow_run_response",
            "workflow_runs_list",
        ]
        if any(broken_ref in ref_path for broken_ref in broken_refs):
            return True

    # Recursively check nested schemas
    for _key, value in schema_def.items():
        if isinstance(value, dict):
            if _has_broken_references(value):
                return True
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and _has_broken_references(item):
                    return True

    return False


def _patch_broken_refs(schema_def: dict[str, Any]) -> dict[str, Any]:
    """Recursively replace broken $refs with a generic object schema.

    This preserves the parent schema's field structure (e.g. new_workflow keeps
    name/description/etc.) while neutralising unresolvable nested references like
    incident_trigger_params.
    """
    if "$ref" in schema_def and _has_broken_references(schema_def):
        return {"type": "object", "additionalProperties": True}

    result: dict[str, Any] = {}
    for key, value in schema_def.items():
        if isinstance(value, dict):
            result[key] = _patch_broken_refs(value)
        elif isinstance(value, list):
            result[key] = [
                _patch_broken_refs(item) if isinstance(item, dict) else item for item in value
            ]
        else:
            result[key] = value
    return result


# A request-body union larger than this (serialized, with $refs resolved) is
# replaced in the tool schema by its discriminator enum. Workflow task_params
# (153 task types, ~131k chars) is the only union over it today; the
# escalation-rule and workflow-trigger unions (10-15k) stay inline, since a
# model writes those in one call and the lookup round trip would cost more.
LARGE_UNION_MAX_CHARS = 40_000

WORKFLOW_TASK_PARAMS_SCHEMA_TOOL = "get_workflow_task_params_schema"

# Discriminator -> the tool that serves one variant of a compacted union. A
# union is only compacted when its discriminator has a lookup tool here, so a
# compacted schema always names a tool that exists.
UNION_LOOKUP_TOOLS: dict[str, str] = {"task_type": WORKFLOW_TASK_PARAMS_SCHEMA_TOOL}


def _single_value_discriminator(variants: list[dict[str, Any]]) -> str | None:
    """The property every variant pins to one enum value, e.g. ``task_type``."""
    common: set[str] | None = None
    for variant in variants:
        pinned = {
            name
            for name, prop in (variant.get("properties") or {}).items()
            if isinstance(prop, dict)
            and isinstance(prop.get("enum"), list)
            and len(prop["enum"]) == 1
        }
        common = pinned if common is None else common & pinned
        if not common:
            return None
    return sorted(common)[0] if common else None


def _merge_variants(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    """One schema accepting either variant's fields."""
    merged = deepcopy(first)
    merged["properties"] = {**(second.get("properties") or {}), **(first.get("properties") or {})}
    required = set(first.get("required") or []) & set(second.get("required") or [])
    if required:
        merged["required"] = sorted(required)
    else:
        merged.pop("required", None)
    return merged


def compact_large_unions(
    spec: dict[str, Any], *, max_chars: int = LARGE_UNION_MAX_CHARS
) -> dict[str, dict[str, dict[str, Any]]]:
    """Replace oversized discriminated unions in request schemas, in place.

    ``task_params`` on the workflow task write tools is a oneOf/anyOf of 153
    task types, inlined into each tool's input schema: about 32k tokens per
    tool, sent to every client on every ``tools/list``. The union becomes an
    open object whose discriminator is an enum of the variant names, and the
    variants are returned (``{discriminator: {value: schema}}``) so a lookup
    tool can serve one at a time.

    Only unions whose every variant pins the same property to a single enum
    value are compacted; anything else is left untouched.
    """
    schemas = spec.get("components", {}).get("schemas", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict) and isinstance(node.get("$ref"), str):
            target = schemas.get(node["$ref"].split("/")[-1])
            return deepcopy(target) if isinstance(target, dict) else node
        return node

    found: dict[str, dict[str, dict[str, Any]]] = {}

    def visit(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, dict):
            return
        for key in ("oneOf", "anyOf"):
            raw = node.get(key)
            if not isinstance(raw, list) or len(raw) < 2:
                continue
            # A broken $ref was already patched to a bare open object (see
            # _patch_broken_refs); it names no fields, so it is not a variant.
            variants = [
                resolved
                for resolved in (resolve(item) for item in raw)
                if resolved != {"type": "object", "additionalProperties": True}
            ]
            if not all(isinstance(item, dict) for item in variants):
                continue
            if len(json.dumps(variants)) <= max_chars:
                continue
            discriminator = _single_value_discriminator(variants)
            if discriminator is None or discriminator not in UNION_LOOKUP_TOOLS:
                continue
            by_value: dict[str, dict[str, Any]] = {}
            for variant in variants:
                value = str(variant["properties"][discriminator]["enum"][0])
                existing = by_value.get(value)
                # The spec reuses a value across variants (e.g. Quip and
                # Google Docs both declare create_google_docs_page): keep every
                # field, require only what all of them require.
                by_value[value] = (
                    variant if existing is None else _merge_variants(existing, variant)
                )
            found.setdefault(discriminator, {}).update(by_value)
            description = node.get("description")
            node.clear()
            node.update(
                {
                    "type": "object",
                    "description": (
                        (f"{description.rstrip('.')}. " if isinstance(description, str) else "")
                        + f"Set `{discriminator}` to one of the {len(by_value)} values below. "
                        "Each takes its own fields, which this schema leaves out to keep the "
                        f"tool small: call {UNION_LOOKUP_TOOLS[discriminator]}"
                        f"({discriminator}=...) for them before writing."
                    ),
                    "properties": {
                        discriminator: {"type": "string", "enum": sorted(by_value)},
                    },
                    "additionalProperties": True,
                }
            )
            return
        for value in node.values():
            visit(value)

    visit(spec.get("paths", {}))
    for schema in schemas.values():
        visit(schema)
    return found


# Property descriptions that mark a field the API ignores or no longer wants.
# The field is left out of the tool schema; a client that still sends it is
# unaffected (attributes pass through and the API ignores or derives them).
_IGNORED_PROPERTY_PREFIXES = (
    "accepted for json:api client compatibility, but ignored",
    "deprecated.",
    "[deprecated]",
)

# An enum this long is a reference list, not a choice a model makes from the
# schema: the IANA time zone list (~308 names) appeared 8 times, ~36k chars.
_LONG_ENUM_MIN_VALUES = 100
_TIME_ZONE_HINT = "IANA time zone name, e.g. 'America/New_York' or 'Europe/Paris'."


def _is_time_zone_enum(values: list[Any]) -> bool:
    return (
        len(values) >= _LONG_ENUM_MIN_VALUES
        and {"America/New_York", "Etc/UTC"} & set(map(str, values)) != set()
    )


def trim_tool_schemas(spec: dict[str, Any]) -> dict[str, Any]:
    """Drop schema detail that costs context and tells a model nothing, in place.

    * properties the API documents as ignored or deprecated (the JSON:API
      ``data.id`` on updates, ``slug`` derived from ``name``, ...);
    * the IANA time zone enum, replaced by a string with an example.

    Returns counts of what was trimmed and ``removed_property_names``: body
    repair still treats those as known attributes, so a caller that sends
    ``slug`` in a flat body is repaired as before rather than refused.
    """
    counts: dict[str, Any] = {"ignored_properties": 0, "time_zone_enums": 0}
    removed: set[str] = set()

    def visit(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, dict):
            return
        properties = node.get("properties")
        if isinstance(properties, dict):
            ignored = [
                name
                for name, prop in properties.items()
                if isinstance(prop, dict)
                and str(prop.get("description", "")).lower().startswith(_IGNORED_PROPERTY_PREFIXES)
            ]
            for name in ignored:
                del properties[name]
            if ignored and isinstance(node.get("required"), list):
                node["required"] = [name for name in node["required"] if name not in ignored]
                if not node["required"]:
                    del node["required"]
            counts["ignored_properties"] += len(ignored)
            removed.update(ignored)
        values = node.get("enum")
        if isinstance(values, list) and _is_time_zone_enum(values):
            del node["enum"]
            description = str(node.get("description", "")).strip()
            node["description"] = f"{description} {_TIME_ZONE_HINT}".strip()
            counts["time_zone_enums"] += 1
        for value in node.values():
            visit(value)

    visit(spec.get("paths", {}))
    visit(spec.get("components", {}).get("schemas", {}))
    counts["removed_property_names"] = frozenset(removed)
    return counts
