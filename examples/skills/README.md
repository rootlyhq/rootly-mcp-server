# Rootly MCP Skills

This directory contains pre-built Claude Code skills that demonstrate how to effectively use the Rootly MCP server for incident management workflows.

## What are Skills?

Skills are specialized AI agents that combine multiple tools and follow specific workflows. They provide Claude Code with domain expertise and structured approaches to complex tasks.

## Available Skills

### 🚨 Rootly Incident Responder

**File:** `rootly-incident-responder.md`

An experienced SRE specialist that handles production incidents from detection to resolution.

**Capabilities:**
- Analyzes incidents with full Rootly context
- Leverages ML-based similarity to find related historical incidents
- Provides AI-powered solution recommendations from past resolutions
- Coordinates with on-call teams (timezone-aware)
- Correlates incidents with code deployments via GitHub
- Creates structured action items and remediation plans
- Tracks confidence levels and resolution time estimates

**Best for:**
- Production incident response
- Post-incident analysis
- On-call handoffs
- Learning from historical incident patterns

## Installation

### Option 1: Project-Specific Installation

Copy the skill to your project's `.claude/skills/` directory:

```bash
mkdir -p .claude/skills
cp rootly-incident-responder.md .claude/skills/
```

The skill will be available for use in that project.

### Option 2: Global Installation

Install the skill globally for use across all projects:

```bash
mkdir -p ~/.claude/skills
cp rootly-incident-responder.md ~/.claude/skills/
```

## Usage

Once installed, you can invoke skills in Claude Code using the `@` symbol:

```
@rootly-incident-responder analyze incident #12345
```

Or let Claude automatically use the skill when appropriate:

```
Can you help me respond to the current production incident?
```

Claude will recognize the context and automatically engage the Rootly Incident Responder skill.

## Prerequisites

These skills require the Rootly MCP server to be configured in your Claude Code settings:

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

For GitHub integration (code correlation), also add:

```json
{
  "mcpServers": {
    "github": {
      "command": "uvx",
      "args": ["--from", "mcp-server-github", "mcp-server-github"],
      "env": {
        "GITHUB_TOKEN": "<YOUR_GITHUB_TOKEN>"
      }
    }
  }
}
```

## Contributing Skills

Have an idea for a new Rootly skill? We welcome contributions!

**Potential skill ideas:**
- **Post-Incident Reviewer**: Analyzes resolved incidents and generates comprehensive post-mortems
- **On-Call Optimizer**: Analyzes on-call metrics and suggests schedule improvements
- **Incident Pattern Detector**: Identifies recurring incident patterns and suggests preventive measures
- **Severity Calibrator**: Helps teams maintain consistent severity classifications
- **Runbook Generator**: Creates runbooks from resolved incident patterns

To contribute:
1. Create your skill following the format in `rootly-incident-responder.md`
2. Test it thoroughly with real Rootly incidents
3. Submit a pull request with documentation and examples

## Learn More

- [Rootly MCP Server Documentation](../../README.md)
- [Claude Code Skills Guide](https://docs.anthropic.com/claude/docs/skills)
- [Contributing Guidelines](../../CONTRIBUTING.md)

## Support

- **Issues**: [GitHub Issues](https://github.com/rootlyhq/rootly-mcp-server/issues)
- **Discussions**: [GitHub Discussions](https://github.com/rootlyhq/rootly-mcp-server/discussions)
- **Rootly Support**: [docs.rootly.com](https://docs.rootly.com)
