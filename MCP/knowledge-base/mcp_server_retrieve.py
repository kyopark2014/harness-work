"""Streamable-HTTP MCP server for harness-skills Knowledge Base retrieve."""

import logging
import sys

import mcp_retrieve
from mcp.server.fastmcp import FastMCP

logging.basicConfig(
    level=logging.INFO,
    format="%(filename)s:%(lineno)d | %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("retrieve-server")

try:
    mcp = FastMCP(
        name="knowledge-base",
        instructions=(
            "You retrieve documents from the project Knowledge Base using RAG. "
            "Results are scoped to documents owned by the caller's actor_id. "
            "Always pass actor_id from the system prompt (account login id, "
            "e.g. ksdyb) — never a nickname or display name."
        ),
        host="0.0.0.0",
        stateless_http=True,
    )
    logger.info("MCP server initialized successfully")
except Exception as e:
    logger.info(f"Error: {str(e)}")
    raise


@mcp.tool()
def retrieve(keyword: str, actor_id: str) -> str:
    """
    Query the Knowledge Base with RAG.
    Only returns documents owned by the given actor_id (metadata owner filter).

    keyword: search query text
    actor_id: account login id from the system prompt
        (KB owner metadata / docs/{actor_id}/). Do NOT use a nickname or display name. 
    return: JSON list of {contents, reference} hits
    """
    logger.info(f"search --> keyword: {keyword}, actor_id: {actor_id}")
    try:
        result = mcp_retrieve.retrieve(keyword, actor_id=actor_id)
        logger.info(f"result length: {len(result)}")
        return result
    except Exception as e:
        logger.error(f"Error in retrieve function: {e}")
        return f"Error retrieving data: {str(e)}"


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
