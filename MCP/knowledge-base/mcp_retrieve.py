"""Bedrock Knowledge Base retrieve helper for harness-skills MCP."""

from __future__ import annotations

import json
import logging
import os
import sys
from urllib.parse import quote

import boto3
from botocore.exceptions import ClientError

logging.basicConfig(
    level=logging.INFO,
    format="%(filename)s:%(lineno)d | %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("retrieve")

script_dir = os.path.dirname(os.path.abspath(__file__))
config_path = os.path.join(script_dir, "config.json")


def load_config() -> dict:
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning(f"Failed to load config.json: {e}")
        return {}


config = load_config()

bedrock_region = (
    os.environ.get("AWS_REGION")
    or os.environ.get("AWS_DEFAULT_REGION")
    or config.get("region")
    or "us-west-2"
)
project_name = os.environ.get("PROJECT_NAME") or config.get("projectName") or ""
knowledge_base_id = (
    os.environ.get("KNOWLEDGE_BASE_ID") or config.get("knowledge_base_id") or ""
)
sharing_url = (
    os.environ.get("SHARING_URL") or config.get("sharing_url") or ""
).rstrip("/")
doc_prefix = "docs/"
number_of_results = int(os.environ.get("NUMBER_OF_RESULTS") or "5")


def resolve_knowledge_base_id() -> str:
    """Resolve KB id from env/config, or look up by project name."""
    global knowledge_base_id
    if knowledge_base_id:
        return knowledge_base_id

    if not project_name:
        logger.error("PROJECT_NAME / projectName is not set; cannot resolve Knowledge Base")
        return ""

    try:
        bedrock_agent = boto3.client("bedrock-agent", region_name=bedrock_region)
        response = bedrock_agent.list_knowledge_bases(maxResults=50)
        for kb in response.get("knowledgeBaseSummaries", []):
            if kb.get("name") == project_name:
                knowledge_base_id = kb["knowledgeBaseId"]
                logger.info(
                    f"Resolved Knowledge Base '{project_name}' → {knowledge_base_id}"
                )
                return knowledge_base_id
    except Exception as e:
        logger.error(f"Failed to resolve Knowledge Base by name: {e}")

    logger.error(f"Knowledge Base named '{project_name}' not found")
    return ""


bedrock_agent_runtime_client = boto3.client(
    "bedrock-agent-runtime",
    region_name=bedrock_region,
)

logger.info(
    "retrieve config: project=%s kb=%s region=%s sharing_url=%s",
    project_name,
    knowledge_base_id or "(resolve-at-call)",
    bedrock_region,
    sharing_url or "(none)",
)


def _owner_filter(user_id: str) -> dict:
    """Filter so only documents whose STRING_LIST ``owner`` contains user_id.

    Uses listContains (Bedrock Knowledge Base metadata filter for string lists).
    See: https://docs.aws.amazon.com/bedrock/latest/userguide/kb-test-config.html
    """
    return {
        "listContains": {
            "key": "owner",
            "value": user_id,
        }
    }


def _retrieval_configuration(user_id: str) -> dict:
    return {
        "vectorSearchConfiguration": {
            "numberOfResults": number_of_results,
            "filter": _owner_filter(user_id),
        }
    }


def _call_retrieve(query: str, kb_id: str, user_id: str):
    return bedrock_agent_runtime_client.retrieve(
        retrievalQuery={"text": query},
        knowledgeBaseId=kb_id,
        retrievalConfiguration=_retrieval_configuration(user_id),
    )


def retrieve(query: str, actor_id: str | None = None, user_id: str | None = None) -> str:
    """Retrieve RAG hits scoped to the given actor_id's owned documents.

    actor_id must be passed by the caller (separate AgentCore Runtime MCP has no
    shared login env from the harness process). ``user_id`` is accepted as a
    deprecated alias.
    """
    uid = (actor_id or user_id or "").strip()
    if not uid:
        logger.error("actor_id is empty; refusing unscoped RAG retrieve")
        return json.dumps(
            {"error": "actor_id is required for RAG retrieve"},
            ensure_ascii=False,
        )

    kb_id = resolve_knowledge_base_id()
    if not kb_id:
        return json.dumps(
            [{"contents": "Knowledge Base is not configured.", "reference": {}}],
            ensure_ascii=False,
        )

    logger.info("RAG retrieve for actor_id=%s query=%s", uid, query)

    try:
        response = _call_retrieve(query, kb_id, uid)
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        if error_code == "ResourceNotFoundException" and project_name:
            logger.warning(f"ResourceNotFoundException: {e}; resolving KB by name")
            global knowledge_base_id
            knowledge_base_id = ""
            kb_id = resolve_knowledge_base_id()
            if not kb_id:
                raise
            response = _call_retrieve(query, kb_id, uid)
        else:
            logger.error(f"Error retrieving: {e}")
            raise

    json_docs = []
    for result in response.get("retrievalResults", []):
        text = url = name = None

        content = result.get("content") or {}
        if "text" in content:
            text = content["text"]

        location = result.get("location") or {}
        if "s3Location" in location:
            uri = (location["s3Location"] or {}).get("uri") or ""
            name = uri.split("/")[-1] if uri else None
            if sharing_url and name:
                url = f"{sharing_url}/{doc_prefix}{quote(name)}"
            else:
                url = uri
        elif "webLocation" in location:
            url = (location["webLocation"] or {}).get("url") or ""
            name = "WEB"

        page = None
        raw_page = (result.get("metadata") or {}).get(
            "x-amz-bedrock-kb-document-page-number"
        )
        if raw_page is not None:
            try:
                page = int(raw_page) + 1
            except (TypeError, ValueError):
                page = raw_page

        reference = {"url": url, "title": name, "from": "RAG"}
        if page is not None:
            reference["page"] = page

        json_docs.append({"contents": text, "reference": reference})

    logger.info(f"retrieve results: {len(json_docs)} (actor_id={uid})")
    return json.dumps(json_docs, ensure_ascii=False)
