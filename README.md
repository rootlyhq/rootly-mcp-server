<!-- mcp-name: com.rootly/mcp-server -->
# Rootly MCP Server

[![PyPI version](https://badge.fury.io/py/rootly-mcp-server.svg)](https://pypi.org/project/rootly-mcp-server/)
[![PyPI - Downloads](https://img.shields.io/pypi/dm/rootly-mcp-server)](https://pypi.org/project/rootly-mcp-server/)
[![Python Version](https://img.shields.io/pypi/pyversions/rootly-mcp-server.svg)](https://pypi.org/project/rootly-mcp-server/)

An MCP server for the [Rootly API](https://docs.rootly.com/api-reference/overview) for Cursor, Windsurf, Claude, and other MCP clients.

![Demo GIF](https://raw.githubusercontent.com/Rootly-AI-Labs/Rootly-MCP-server/refs/heads/main/rootly-mcp-server-demo.gif)

## Quick Start

Use the hosted MCP server. No local installation required.

### Hosted Transport Options

- **Streamable HTTP (recommended):** `https://mcp.rootly.com/mcp`
- **SSE (stable alternative):** `https://mcp.rootly.com/sse`
- **Code Mode:** `https://mcp.rootly.com/mcp-codemode`

Hosted tool profiles:

- **Full (default):** use the URLs above as-is
- **Slim (~70 tools):** add `?tool_profile=slim` to the hosted URL, for example `https://mcp.rootly.com/mcp?tool_profile=slim`

### General Remote Setup

**With OAuth2 (recommended):**

```json
{
  "mcpServers": {
    "rootly": {
      "url": "https://mcp.rootly.com/mcp"
    }
  }
}
```

Your MCP client handles OAuth2 login automatically — a browser window opens for you to authenticate with Rootly. No API token needed.

**With API Token:**

```json
{
  "mcpServers": {
    "rootly": {
      "url": "https://mcp.rootly.com/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_ROOTLY_API_TOKEN"
      }
    }
  }
}
```

SSE (alternative):

```json
{
  "mcpServers": {
    "rootly": {
      "url": "https://mcp.rootly.com/sse"
    }
  }
}
```

Code Mode:

```json
{
  "mcpServers": {
    "rootly": {
      "url": "https://mcp.rootly.com/mcp-codemode"
    }
  }
}
```

### Agent Setup

<details>
<summary><strong>Claude Code</strong></summary>

<br>

**With OAuth2 (recommended):**

```bash
claude mcp add --transport http rootly https://mcp.rootly.com/mcp

# Code Mode:
claude mcp add --transport http rootly-codemode https://mcp.rootly.com/mcp-codemode
```

**With API Token:**

```bash
claude mcp add --transport http rootly https://mcp.rootly.com/mcp \
  --header "Authorization: Bearer YOUR_ROOTLY_API_TOKEN"
```

**Manual Configuration** — Create `.mcp.json` in your project root:

```json
{
  "mcpServers": {
    "rootly": {
      "type": "http",
      "url": "https://mcp.rootly.com/mcp"
    }
  }
}
```

</details>

<details>
<summary><strong>Gemini CLI</strong></summary>

<br>

Install the extension:

```bash
gemini extensions install https://github.com/Rootly-AI-Labs/Rootly-MCP-server
```

Or configure manually in `~/.gemini/settings.json`:

```json
{
  "mcpServers": {
    "rootly": {
      "command": "uvx",
      "args": ["--from", "rootly-mcp-server", "rootly-mcp-server"],
      "env": {
        "ROOTLY_API_TOKEN": "<YOUR_ROOTLY_API_TOKEN>"
      }
    }
  }
}
```

</details>

<details>
<summary><strong>Cursor</strong></summary>

<br>

Add to `.cursor/mcp.json` or `~/.cursor/mcp.json`:

**With OAuth2 (recommended):**

```json
{
  "mcpServers": {
    "rootly": {
      "url": "https://mcp.rootly.com/mcp"
    }
  }
}
```

**With API Token:**

```json
{
  "mcpServers": {
    "rootly": {
      "url": "https://mcp.rootly.com/mcp",
      "headers": {
        "Authorization": "Bearer <YOUR_ROOTLY_API_TOKEN>"
      }
    }
  }
}
```

</details>

<details>
<summary><strong>Windsurf</strong></summary>

<br>

Add to `~/.codeium/windsurf/mcp_config.json`:

**With OAuth2 (recommended):**

```json
{
  "mcpServers": {
    "rootly": {
      "serverUrl": "https://mcp.rootly.com/mcp"
    }
  }
}
```

**With API Token:**

```json
{
  "mcpServers": {
    "rootly": {
      "serverUrl": "https://mcp.rootly.com/mcp",
      "headers": {
        "Authorization": "Bearer <YOUR_ROOTLY_API_TOKEN>"
      }
    }
  }
}
```

</details>

<details>
<summary><strong>Codex</strong></summary>

<br>

Add to `~/.codex/config.toml`:

**With OAuth2 (recommended):**

```toml
[mcp_servers.rootly]
url = "https://mcp.rootly.com/mcp"
```

**With API Token:**

```toml
[mcp_servers.rootly]
url = "https://mcp.rootly.com/mcp"
bearer_token_env_var = "ROOTLY_API_TOKEN"
```

</details>

<details>
<summary><strong>Claude Desktop</strong></summary>

<br>

**With OAuth2 (recommended):**

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "rootly": {
      "url": "https://mcp.rootly.com/mcp"
    }
  }
}
```

Claude Desktop handles OAuth2 login automatically.

**With API Token (via mcp-remote):**

```json
{
  "mcpServers": {
    "rootly": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "https://mcp.rootly.com/mcp",
        "--transport",
        "http",
        "--header",
        "Authorization: Bearer <YOUR_ROOTLY_API_TOKEN>"
      ]
    }
  }
}
```

</details>

## Rootly CLI

Standalone CLI for incidents, alerts, services, and on-call operations.

Install via Homebrew:

```bash
brew install rootlyhq/tap/rootly-cli
```

Or via Go:

```bash
go install github.com/rootlyhq/rootly-cli/cmd/rootly@latest
```

For more details, see the [Rootly CLI repository](https://github.com/rootlyhq/rootly-cli).

## Alternative Installation (Local)

Run the MCP server locally if you do not want to use the hosted service.

### Prerequisites

- Python 3.12 or higher
- `uv` package manager
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```
- [Rootly API token](https://docs.rootly.com/api-reference/overview#how-to-generate-an-api-key%3F)

### API Token Types

Choose the token type based on the access you need:

- **Global API Key**: Full access across the Rootly instance. Best for organization-wide visibility.
- **Team API Key**: Access limited to entities owned by that team.
- **Personal API Key**: Access matches the user who created it.

A **Global API Key** is recommended for organization-wide queries and for actions that modify data, especially when workflows may span multiple teams, schedules, or incidents.

### With uv

```json
{
  "mcpServers": {
    "rootly": {
      "command": "uv",
      "args": [
        "tool",
        "run",
        "--from",
        "rootly-mcp-server",
        "rootly-mcp-server"
      ],
      "env": {
        "ROOTLY_API_TOKEN": "<YOUR_ROOTLY_API_TOKEN>",
        "ROOTLY_MCP_ENABLE_WRITE_TOOLS": "true"
      }
    }
  }
}
```

## Self-Hosted Transport Options

Choose one transport per server process:

- **Streamable HTTP** endpoint path: `/mcp`
- **SSE** endpoint path: `/sse`
- **Code Mode (experimental)** endpoint path: `/mcp-codemode` in hosted dual-transport mode

Hosted and self-hosted deployments now both expose the full tool surface by default.

- Hosted default: full surface
- Hosted slim profile: about 70 high-usage tools via `?tool_profile=slim`
- Self-hosted default: full surface

To restrict either deployment to read-only tools, start the server with `--no-enable-write-tools` or set `ROOTLY_MCP_ENABLE_WRITE_TOOLS=false`.

For hosted clients that want the smaller remote profile, append `?tool_profile=slim` to the MCP URL or send `X-Rootly-Tool-Profile: slim`.

To override the hosted or self-hosted default profile entirely, set `ROOTLY_MCP_ENABLED_TOOLS` (or pass `--enabled-tools`) with a comma-separated allowlist of exact tool names. When that variable is set, it fully replaces the default selection.

To expose only a specific subset of MCP tools on a self-hosted deployment, set `ROOTLY_MCP_ENABLED_TOOLS` (or pass `--enabled-tools`) with a comma-separated allowlist of exact tool names, for example `list_incidents,getIncident,get_server_version`.

To discover the exact tool names available under your current self-hosted configuration, run:

```bash
ROOTLY_API_TOKEN=<YOUR_ROOTLY_API_TOKEN> \
uv run python -m rootly_mcp_server --list-tools
```

This prints the effective MCP tool names after applying your current settings, including `ROOTLY_MCP_ENABLE_WRITE_TOOLS` and `ROOTLY_MCP_ENABLED_TOOLS`.

Smoke-test a self-hosted allowlist:

```bash
ROOTLY_API_TOKEN=<YOUR_ROOTLY_API_TOKEN> \
ROOTLY_MCP_ENABLED_TOOLS=list_incidents,getIncident,get_server_version \
uv run python -m rootly_mcp_server --transport streamable-http --log-level ERROR
```

Then connect an MCP client to `http://127.0.0.1:8000/mcp` and verify `tools/list` returns only:

```text
get_server_version
getIncident
list_incidents
```

To include specific write tools for self-hosted testing, add both the write flag and the allowlist:

```bash
ROOTLY_API_TOKEN=<YOUR_ROOTLY_API_TOKEN> \
ROOTLY_MCP_ENABLE_WRITE_TOOLS=true \
ROOTLY_MCP_ENABLED_TOOLS=createIncident,createWorkflowTask,listTeams \
uv run python -m rootly_mcp_server --transport streamable-http --log-level ERROR
```

Example Docker run (Streamable HTTP):

```bash
docker run -p 8000:8000 \
  -e ROOTLY_TRANSPORT=streamable-http \
  -e ROOTLY_API_TOKEN=<YOUR_ROOTLY_API_TOKEN> \
  -e ROOTLY_MCP_ENABLE_WRITE_TOOLS=true \
  rootly-mcp-server
```

Example Docker run (SSE):

```bash
docker run -p 8000:8000 \
  -e ROOTLY_TRANSPORT=sse \
  -e ROOTLY_API_TOKEN=<YOUR_ROOTLY_API_TOKEN> \
  rootly-mcp-server
```

Example Docker run (Dual transport + Code Mode):

```bash
docker run -p 8000:8000 \
  -e ROOTLY_TRANSPORT=both \
  -e ROOTLY_API_TOKEN=<YOUR_ROOTLY_API_TOKEN> \
  rootly-mcp-server
```

## Workflow-Focused Tool Subsets

The full hosted and self-hosted surface exposes 200+ tools. If you want tighter workflow-specific subsets, use `ROOTLY_MCP_ENABLED_TOOLS`:

### 🚨 Incident Response (25 tools)
*Essential tools for emergency responders and incident commanders*

```bash
ROOTLY_MCP_ENABLED_TOOLS="list_incidents,getIncident,createIncident,updateIncident,search_incidents,find_related_incidents,suggest_solutions,createIncidentActionItem,listIncidentActionItems,updateIncidentFormFieldSelection,listTeams,getCurrentUser,listServices,listSeverities,getAlert,listAlerts,get_alert_by_short_id,listEscalationPolicies,getEscalationPolicy,listOnCallRoles,listSchedules,getScheduleShifts,get_oncall_handoff_summary,get_shift_incidents,list_endpoints"
```

### 📅 On-Call Management (35 tools)  
*For schedule coordinators and on-call managers*

```bash
ROOTLY_MCP_ENABLED_TOOLS="listSchedules,getSchedule,updateSchedule,getScheduleShifts,listShifts,list_shifts,createScheduleRotation,updateScheduleRotation,listScheduleRotations,getScheduleRotation,listScheduleRotationUsers,updateScheduleRotationUser,createOnCallShadow,updateOnCallShadow,listOnCallShadows,createOverrideShift,updateOverrideShift,listOverrideShifts,listOnCallRoles,updateOnCallRole,get_oncall_schedule_summary,get_oncall_shift_metrics,check_oncall_health_risk,check_responder_availability,create_override_recommendation,listTeams,getTeam,listUsers,getUser,getCurrentUser,listEscalationPolicies,updateEscalationPolicy,listEscalationPaths,updateEscalationPath,listEscalationLevels"
```

### 📊 Monitoring & Alerting (40 tools)
*For platform teams setting up observability*

```bash
ROOTLY_MCP_ENABLED_TOOLS="listAlerts,getAlert,get_alert_by_short_id,createAlertGroup,updateAlertGroup,listAlertGroups,createAlertRoutingRule,updateAlertRoutingRule,listAlertRoutingRules,listAlertEvents,getAlertEvent,updateAlertEvent,createHeartbeat,updateHeartbeat,listHeartbeats,getHeartbeat,createPulse,updatePulse,listPulses,getPulse,createDashboard,updateDashboard,listDashboards,getDashboard,createDashboardPanel,updateDashboardPanel,listStatusPages,getStatusPage,updateStatusPage,listStatusPageTemplates,getStatusPageTemplate,listCommunicationsTemplates,updateCommunicationsTemplate,createLiveCallRouter,updateLiveCallRouter,listServices,listTeams,getCurrentUser,listEnvironments,listSeverities,list_endpoints"
```

### 📋 Post-Incident Analysis (30 tools)
*For SREs doing retrospectives and process improvement*

```bash
ROOTLY_MCP_ENABLED_TOOLS="getIncident,updateIncident,find_related_incidents,suggest_solutions,listIncidentActionItems,createIncidentActionItem,updateIncidentFormFieldSelection,createPostIncidentReview,updatePostIncidentReview,listPostIncidentReviews,getPostIncidentReview,createRetrospectiveStep,updateRetrospectiveStep,listRetrospectiveSteps,createRetrospectiveProcess,updateRetrospectiveProcess,listRetrospectiveProcesses,createPlaybook,updatePlaybook,listPlaybooks,getPlaybook,createPlaybookTask,updatePlaybookTask,listCauses,getCause,updateCause,listIncidentTypes,getIncidentType,updateIncidentType,getCurrentUser"
```

### 📈 Analytics & Reporting (15 tools)
*For leadership and metrics teams (read-only focus)*

```bash
ROOTLY_MCP_ENABLED_TOOLS="list_incidents,search_incidents,collect_incidents,listTeams,listServices,listSchedules,get_oncall_shift_metrics,get_shift_incidents,listDashboards,getDashboard,listAlerts,listHeartbeats,listPulses,getCurrentUser,list_endpoints"
```

### Multiple MCP Instances for Different Teams

You can run multiple MCP instances with different tool subsets:

```json
{
  "mcpServers": {
    "rootly-incident-response": {
      "command": "uvx", "args": ["--from", "rootly-mcp-server", "rootly-mcp-server"],
      "env": {
        "ROOTLY_API_TOKEN": "<token>",
        "ROOTLY_MCP_ENABLED_TOOLS": "list_incidents,getIncident,createIncident,find_related_incidents,suggest_solutions..."
      }
    },
    "rootly-oncall-management": {
      "command": "uvx", "args": ["--from", "rootly-mcp-server", "rootly-mcp-server"],
      "env": {
        "ROOTLY_API_TOKEN": "<token>",
        "ROOTLY_MCP_ENABLED_TOOLS": "listSchedules,updateSchedule,createOverrideShift,get_oncall_shift_metrics..."
      }
    }
  }
}
```

### With uvx

```json
{
  "mcpServers": {
    "rootly": {
      "command": "uvx",
      "args": [
        "--from",
        "rootly-mcp-server",
        "rootly-mcp-server"
      ],
      "env": {
        "ROOTLY_API_TOKEN": "<YOUR_ROOTLY_API_TOKEN>"
      }
    }
  }
}
```

## Features

- **Dynamic Tool Generation**: Automatically creates MCP resources from Rootly's OpenAPI (Swagger) specification
- **Smart Pagination**: Uses bounded pagination and compact incident responses to prevent context window overflow
- **API Filtering**: Limits exposed API endpoints for security and performance
- **Intelligent Incident Analysis**: Smart tools that analyze historical incident data
  - **`find_related_incidents`**: Uses TF-IDF similarity analysis to find historically similar incidents
  - **`suggest_solutions`**: Mines past incident resolutions to recommend actionable solutions
- **MCP Resources**: Exposes incidents, teams, on-call status, and workflow guides as structured resources for AI context
- **Intelligent Pattern Recognition**: Automatically identifies services, error types, and resolution patterns
- **On-Call Health Integration**: Detects workload health risk in scheduled responders

## Supported Tools

The default tool surface depends on deployment profile:

- Hosted default: about **218 tools**
- Hosted slim profile: about **70 tools**
- Self-hosted default: about **218 tools**

### Custom Agentic Tools

- `check_oncall_health_risk`
- `check_responder_availability`
- `collect_incidents`
- `createIncident` - create a new incident with a scoped set of fields for agent workflows
- `create_override_recommendation`
- `find_related_incidents`
- `getIncident` - retrieve a single incident for direct verification, including PIR-related fields
- `get_alert_by_short_id`
- `get_oncall_handoff_summary`
- `get_oncall_schedule_summary`
- `get_oncall_shift_metrics`
- `get_server_version`
- `get_shift_incidents`
- `list_endpoints`
- `list_incidents`
- `list_shifts`
- `search_incidents`
- `suggest_solutions`
- `updateIncident` - scoped incident update tool for `summary` and `retrospective_progress_status`

### OpenAPI-Generated Tools

```text
ListWorkflowRuns
createIncidentActionItem
createIncidentFormFieldSelection
createWorkflowTask
getAlert
getAlertEvent
getAlertGroup
getAlertRoutingRule
getAlertSource
getAlertUrgency
getCatalog
getCatalogEntity
getCause
getCurrentUser
getCustomForm
getEnvironment
getEscalationLevel
getEscalationPath
getEscalationPolicy
getFormField
getFormFieldOption
getFunctionality
getFunctionalityIncidentsChart
getFunctionalityUptimeChart
getIncidentActionItems
getIncidentFormFieldSelection
getIncidentType
getOnCallRole
getOnCallShadow
getOverrideShift
getSchedule
getScheduleRotation
getScheduleShifts
getService
getServiceIncidentsChart
getServiceUptimeChart
getSeverity
getStatusPage
getStatusPageTemplate
getTeam
getTeamIncidentsChart
getUser
getWorkflow
getWorkflowFormFieldCondition
getWorkflowGroup
getWorkflowTask
listAlertEvents
listAlertGroups
listAlertRoutingRules
listAlertSources
listAlertUrgencies
listAlerts
listAllIncidentActionItems
listCatalogEntities
listCatalogs
listCauses
listCustomForms
listEnvironments
listEscalationLevels
listEscalationLevelsPaths
listEscalationPaths
listEscalationPolicies
listFormFieldOptions
listFormFields
listFunctionalities
listIncidentActionItems
listIncidentAlerts
listIncidentFormFieldSelections
listIncident_Types
listIncidents  (deprecated alias — use `list_incidents`)
listOnCallRoles
listOnCallShadows
listOverrideShifts
listScheduleRotationActiveDays
listScheduleRotationUsers
listScheduleRotations
listSchedules
listServices
listSeverities
listShifts
listStatusPageTemplates
listStatusPages
listTeams
listUsers
listWorkflowFormFieldConditions
listWorkflowGroups
listWorkflows
listWorkflowTasks
updateEnvironment
updateEscalationLevel
updateEscalationPath
updateEscalationPolicy
updateFunctionality
updateIncidentType
updateOnCallRole
updateOnCallShadow
updateOverrideShift
updateSchedule
updateScheduleRotation
updateService
updateSeverity
updateTeam
updateWorkflow
updateIncidentFormFieldSelection
updateWorkflowTask
```

**Major Expansion**: This version includes 50+ new endpoints covering communications, dashboards, playbooks, post-incident reviews, monitoring, and advanced form management - while carefully excluding security-sensitive operations like API key management, user creation/deletion, role management, and webhook configuration.

Delete operations remain disabled in the default tool surface.

## On-Call Health Integration

Integrates with [On-Call Health](https://oncallhealth.ai) to detect workload health risk in scheduled responders.

### Setup

Set the `ONCALLHEALTH_API_KEY` environment variable:

```json
{
  "mcpServers": {
    "rootly": {
      "command": "uvx",
      "args": ["--from", "rootly-mcp-server", "rootly-mcp-server"],
      "env": {
        "ROOTLY_API_TOKEN": "your_rootly_token",
        "ONCALLHEALTH_API_KEY": "och_live_your_key"
      }
    }
  }
}
```

### Usage

```
check_oncall_health_risk(
    start_date="2026-02-09",
    end_date="2026-02-15"
)
```

Returns at-risk users who are scheduled, recommended safe replacements, and action summaries.

## Example Skills

Pre-built Claude Code skills:

### 🚨 [Rootly Incident Responder](examples/skills/rootly-incident-responder.md)

This skill:
- Analyzes production incidents with full context
- Finds similar historical incidents using ML-based similarity matching
- Suggests solutions based on past successful resolutions
- Coordinates with on-call teams across timezones
- Correlates incidents with recent code changes and deployments
- Creates action items and remediation plans
- Provides confidence scores and time estimates

**Quick Start:**
```bash
# Copy the skill to your project
mkdir -p .claude/skills
cp examples/skills/rootly-incident-responder.md .claude/skills/

# Then in Claude Code, invoke it:
# @rootly-incident-responder analyze incident #12345
```

It demonstrates a full incident response workflow using Rootly tools and GitHub context.

### On-Call Shift Metrics

Get on-call shift metrics for any time period, grouped by user, team, or schedule. Includes primary/secondary role tracking, shift counts, hours, and days on-call.

```
get_oncall_shift_metrics(
    start_date="2025-10-01",
    end_date="2025-10-31",
    group_by="user"
)
```

### On-Call Handoff Summary

Complete handoff: current/next on-call + incidents during shifts.

```python
# All on-call (any timezone)
get_oncall_handoff_summary(
    team_ids="team-1,team-2",
    timezone="America/Los_Angeles"
)

# Regional filter - only show APAC on-call during APAC business hours
get_oncall_handoff_summary(
    timezone="Asia/Tokyo",
    filter_by_region=True
)
```

Regional filtering shows only people on-call during business hours (9am-5pm) in the specified timezone.

Returns: `schedules` with `current_oncall`, `next_oncall`, and `shift_incidents`

### MCP Resources for Context

AI agents can access these resources for situational awareness:

- **`incident://{incident_id}`** - Detailed incident information for specific incidents
- **`team://{team_id}`** - Team details including name, color, and metadata  
- **`rootly://incidents`** - List of recent incidents for quick reference
- **`rootly://oncall-status`** - Current on-call status across all schedules (critical for incident response)
- **`rootly://workflow-guide`** - Step-by-step workflow guidance for common operations

Example usage: *"Check the current on-call status"* → AI reads `rootly://oncall-status` resource

### Shift Incidents

Incidents during a time period, with filtering by severity/status/tags.

```python
get_shift_incidents(
    start_time="2025-10-20T09:00:00Z",
    end_time="2025-10-20T17:00:00Z",
    severity="critical",  # optional
    status="resolved",    # optional
    tags="database,api"   # optional
)
```

Returns: `incidents` list + `summary` (counts, avg resolution time, grouping)


## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for developer setup and guidelines.

## Play with it on Postman
[<img src="https://run.pstmn.io/button.svg" alt="Run In Postman" style="width: 128px; height: 32px;">](https://god.gw.postman.com/run-collection/45004446-1074ba3c-44fe-40e3-a932-af7c071b96eb?action=collection%2Ffork&source=rip_markdown&collection-url=entityId%3D45004446-1074ba3c-44fe-40e3-a932-af7c071b96eb%26entityType%3Dcollection%26workspaceId%3D4bec6e3c-50a0-4746-85f1-00a703c32f24)


## About Rootly AI Labs

This project was developed by [Rootly AI Labs](https://labs.rootly.ai/), where we're building the future of system reliability and operational excellence. As an open-source incubator, we share ideas, experiment, and rapidly prototype solutions that benefit the entire community.
![Rootly AI logo](https://github.com/Rootly-AI-Labs/EventOrOutage/raw/main/rootly-ai.png)
