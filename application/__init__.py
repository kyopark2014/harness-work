"""AgentCore Harness application package (React UI + FastAPI)."""

import os
import sys

# Modules (chat, agentcore_client, mcp_*, …) use bare imports when loaded from
# this directory. Ensure the same resolution when imported as `application.*`.
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)
