#!/bin/sh
# Offline end-to-end (mock MCP + runner + viewer). No device, no Flutter.
# Reads the interpreter from the repo's .mcp.json like the skill docs do.
set -e
cd "$(dirname "$0")/../../../.."
PY=$(python3 -c "import json;print(json.load(open('.mcp.json'))['mcpServers']['rustdesk']['command'])")
exec "$PY" mcp/skills/rustdesk-jianhui/scripts/mock_e2e.py "$@"
