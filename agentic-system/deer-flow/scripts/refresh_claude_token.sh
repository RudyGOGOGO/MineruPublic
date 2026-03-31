#!/usr/bin/env bash
# Refresh Claude Code OAuth token from macOS Keychain and update .env
#
# Usage:
#   ./scripts/refresh_claude_token.sh          # update .env in project root
#   source ./scripts/refresh_claude_token.sh   # also export to current shell

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$PROJECT_DIR/.env"

# Extract fresh token from macOS Keychain via the existing export script
TOKEN=$(python3 "$SCRIPT_DIR/export_claude_code_oauth.py" --print-token 2>/dev/null)

if [[ -z "$TOKEN" ]]; then
    echo "ERROR: Could not read OAuth token from Keychain." >&2
    echo "Run 'claude' first to login/refresh, then re-run this script." >&2
    exit 1
fi

# Check token prefix
if [[ "$TOKEN" != sk-ant-oat* ]]; then
    echo "WARNING: Token does not look like a Claude OAuth token (expected sk-ant-oat prefix)." >&2
fi

# Update .env file
if [[ -f "$ENV_FILE" ]]; then
    if grep -q '^CLAUDE_CODE_OAUTH_TOKEN=' "$ENV_FILE"; then
        # Replace existing line (macOS-compatible sed)
        sed -i '' "s|^CLAUDE_CODE_OAUTH_TOKEN=.*|CLAUDE_CODE_OAUTH_TOKEN=$TOKEN|" "$ENV_FILE"
        echo "Updated CLAUDE_CODE_OAUTH_TOKEN in $ENV_FILE"
    else
        echo "" >> "$ENV_FILE"
        echo "CLAUDE_CODE_OAUTH_TOKEN=$TOKEN" >> "$ENV_FILE"
        echo "Added CLAUDE_CODE_OAUTH_TOKEN to $ENV_FILE"
    fi
else
    echo "CLAUDE_CODE_OAUTH_TOKEN=$TOKEN" > "$ENV_FILE"
    echo "Created $ENV_FILE with CLAUDE_CODE_OAUTH_TOKEN"
fi

# Export to current shell (useful when sourced)
export CLAUDE_CODE_OAUTH_TOKEN="$TOKEN"

# Show truncated token for confirmation
echo "Token: ${TOKEN:0:20}...${TOKEN: -6} (refreshed)"
