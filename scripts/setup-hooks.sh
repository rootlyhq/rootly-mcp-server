#!/bin/bash
# Setup git hooks for this repository
# Run this script after cloning: ./scripts/setup-hooks.sh

set -e

REPO_ROOT="$(git rev-parse --show-toplevel)"
HOOKS_DIR="$REPO_ROOT/.git/hooks"

echo "🔧 Setting up git hooks..."

# Create pre-commit hook. It delegates to `make check` so the hook and CI
# share a single definition of the pre-push gate (lint, format-check,
# typecheck, unit tests).
cat > "$HOOKS_DIR/pre-commit" << 'EOF'
#!/bin/bash
# Pre-commit hook: run the shared quality gate before committing.
set -e

echo "🔍 Running pre-commit checks (make check)..."
if ! make check; then
    echo "❌ Pre-commit checks failed!"
    echo "💡 Fix the issues above, or skip with: git commit --no-verify"
    exit 1
fi
echo "✅ All pre-commit checks passed! Proceeding with commit..."
exit 0
EOF

chmod +x "$HOOKS_DIR/pre-commit"

echo "✅ Git hooks installed successfully!"
echo ""
echo "Before every commit, 'make check' runs:"
echo "  - Ruff lint + format check"
echo "  - Pyright and mypy type checks"
echo "  - Unit tests"
echo ""
echo "To skip hooks (not recommended): git commit --no-verify"
