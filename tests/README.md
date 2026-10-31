# Rootly MCP Server Testing Guide

This directory contains comprehensive testing tools for the Rootly MCP Server, including unit tests, integration tests, and development utilities.

## Quick Start

**1. Install dependencies:**
```bash
uv sync --dev
```

**2. Set API token:**
```bash
export ROOTLY_API_TOKEN="your_rootly_api_token_here"
```

**3. Run tests:**
```bash
# All tests
uv run pytest

# Unit tests only (fast)
uv run pytest tests/unit/

# Integration tests
uv run pytest tests/integration/

# Remote server tests (requires token)
uv run pytest tests/integration/remote/ -m remote
```

## Test Structure

```
tests/
├── conftest.py                 # Shared fixtures and configuration
├── unit/                       # Unit tests (fast, no external dependencies)
│   ├── test_server.py         # Server creation, configuration
│   ├── test_authentication.py # Auth logic (hosted vs local)
│   └── test_tools.py          # Tool integration
├── integration/               # Integration tests
│   ├── local/                 # Local server testing
│   │   └── test_basic.py      # Basic functionality
│   └── remote/                # Remote server testing  
│       └── test_essential.py  # 5 critical remote tests
└── test_client.py             # Manual testing utility
```

## Token Setup

### Getting a Token
1. Go to [Rootly API Documentation](https://docs.rootly.com/api-reference/overview#how-to-generate-an-api-key%3F)
2. Create account and generate API token
3. Token should start with `rootly_` and be ~71 characters

### Setting Token

**For current session (temporary):**
```bash
export ROOTLY_API_TOKEN="rootly_your_token_here"
```

**For permanent setup:**
```bash
# Bash
echo 'export ROOTLY_API_TOKEN="rootly_your_token_here"' >> ~/.bashrc
source ~/.bashrc

# Zsh
echo 'export ROOTLY_API_TOKEN="rootly_your_token_here"' >> ~/.zshrc
source ~/.zshrc
```

**Verify token:**
```bash
echo "Token length: ${#ROOTLY_API_TOKEN}"  # Should be ~71
echo "Token prefix: ${ROOTLY_API_TOKEN:0:7}"  # Should be "rootly_"
```

## Test Categories

### Unit Tests (31 tests)
**Fast, no external dependencies**
- Server creation and configuration
- Authentication logic (hosted vs local modes)
- HTTP client functionality
- Tool integration

```bash
# Run unit tests
uv run pytest tests/unit/ -v

# With coverage
uv run pytest tests/unit/ --cov=src/rootly_mcp_server
```

### Local Integration Tests (13 tests)
**Test local server functionality**
- Server creation with real configuration
- Authentication with environment tokens
- Basic API integration

```bash
# Run local integration tests
uv run pytest tests/integration/local/ -v
```

### Remote Integration Tests (13 tests)
**Test remote server functionality (uses mocks)**
- Remote server connectivity
- Bearer token authentication
- Tool listing and execution
- Response time validation
- Error handling

```bash
# Run remote tests
uv run pytest tests/integration/remote/ -v
```

## Test Markers

Use pytest markers to run specific test categories:

```bash
# Run only unit tests
uv run pytest -m unit

# Run only integration tests  
uv run pytest -m integration

# Run only remote tests
uv run pytest -m remote

# Run tests that require API token
uv run pytest -m "not remote"  # Skip remote tests
```

## Development Workflow

### Before Making Changes
```bash
# Run full test suite to establish baseline
uv run pytest
```

### After Making Changes
```bash
# Quick feedback loop
uv run pytest tests/unit/ -x  # Stop on first failure

# If unit tests pass, run integration
uv run pytest tests/integration/local/ -x

# Check code quality
uv run ruff check .
uv run pyright
```

### Before Committing
```bash
# Full test suite with coverage
uv run pytest --cov=src/rootly_mcp_server --cov-report=term-missing
```

## Manual Testing Tool

### `test_client.py`
Interactive test client for manual verification:

```bash
python test_client.py
```

**What it tests:**
- ✅ Server initialization (24+ tools expected)
- ✅ Authentication modes (hosted vs local)
- ✅ Tool functionality (search_incidents with limits)
- ✅ API connectivity and error handling

**Use when:**
- Debugging authentication issues
- Verifying new functionality manually
- Troubleshooting API connectivity
- Testing against live remote server

## GitHub Actions CI/CD

Tests run automatically on push/PR via `.github/workflows/test.yml`:

**Pull Request Testing:**
- Code quality checks (ruff, pyright)
- Unit tests with coverage
- Local integration tests

**Main Branch Testing (after 7min deployment wait):**
- All PR tests PLUS
- Remote server essential tests

## Troubleshooting

### Common Issues

**"ROOTLY_API_TOKEN not set"**
```bash
# Check if token is set
env | grep ROOTLY_API_TOKEN

# Set token temporarily
export ROOTLY_API_TOKEN="your_token_here"
```

**"401 Unauthorized" errors**
```bash
# Test token directly
curl -H "Authorization: Bearer $ROOTLY_API_TOKEN" \
     -H "Content-Type: application/vnd.api+json" \
     "https://api.rootly.com/v1/incidents" | head -20
```

**Tests timeout or hang**
```bash
# Run with timeout
uv run pytest --timeout=60

# Check for async issues
uv run pytest -v --tb=short
```

**Import errors**
```bash
# Reinstall dependencies
uv sync --dev

# Check Python path
uv run python -c "import rootly_mcp_server; print('OK')"
```

### Test Debugging

**Verbose output:**
```bash
uv run pytest tests/unit/test_server.py::TestServerCreation::test_create_server_with_defaults -v -s
```

**Debug specific test:**
```bash
uv run pytest tests/unit/test_authentication.py -k "test_local_mode" --pdb
```

**Check fixtures:**
```bash
uv run pytest --fixtures test_specific_file.py
```

## Adding New Tests

### For New Features
1. **Unit tests first** - Test logic in isolation
2. **Integration tests** - Test with real dependencies
3. **Update markers** - Add appropriate `@pytest.mark.*`
4. **Update documentation** - Document new test behavior

### Test Writing Guidelines
- Use descriptive test names
- One assertion per concept tested
- Use appropriate fixtures from `conftest.py`
- Mock external dependencies appropriately
- Add docstrings explaining test purpose

### Example Test Structure
```python
@pytest.mark.unit
class TestNewFeature:
    """Test new feature functionality."""

    def test_feature_basic_case(self, mock_dependency):
        """Test basic functionality works."""
        # Given
        setup_conditions()

        # When
        result = new_feature()

        # Then
        assert result.is_expected()
```

## Performance

Current test performance targets:
- **Unit tests:** <30 seconds
- **Local integration:** <30 seconds  
- **Remote integration:** <60 seconds (including mocks)
- **Full suite:** <2 minutes locally

Monitor test performance and optimize slow tests.