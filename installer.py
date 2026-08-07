#!/usr/bin/env python3
"""
AWS Infrastructure Installer using boto3
This script provisions AgentCore Harness (S3, skills, VPC, S3 Files mount,
CloudFront, IAM roles, Memory, S3 Vectors Knowledge Base, KB + artifact-share
MCP Runtimes behind a shared AgentCore Gateway, CreateHarness)
and deploys the React+FastAPI Web UI to Amazon ECS Fargate (ALB + CloudFront),
similar to strands-work.
"""

import argparse
import base64
import boto3
import getpass
import json
import time
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import mimetypes
import uuid
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from botocore.exceptions import ClientError, NoCredentialsError
from bedrock_agentcore.memory import MemoryClient

import s3_files_vpc
from ecs_web import EcsWebDeployer

KB_MCP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "MCP", "knowledge-base")
ARTIFACT_SHARE_MCP_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "MCP", "artifact-share"
)

# Configuration
project_name = "harness-work"  # at least 3 characters
region = "us-west-2"
DEFAULT_MODEL_ID = "global.anthropic.claude-opus-4-7"

# CreateHarness harnessName: Pattern [a-zA-Z][a-zA-Z0-9_]{0,39} — no hyphens.
_HARNESS_NAME_API_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,39}$")

# Cognito Web UI auth
COGNITO_ADMIN_USERNAME = "admin"
COGNITO_CLIENT_NAME = f"{project_name}-web-ui"
SESSION_SIGNING_KEY_SECRET_NAME = f"{project_name}/session-signing-key"

sts_client = boto3.client("sts", region_name=region)
account_id = str(sts_client.get_caller_identity()["Account"])

# Bedrock Knowledge Base + S3 Vectors (same pattern as power-runtime)
vector_index_name = project_name
vector_bucket_name = f"{project_name}-{account_id}"
embedding_dimensions = 1024
embedding_data_type = "float32"
distance_metric = "cosine"
# Bedrock Knowledge Base requires these metadata keys as non-filterable on S3 Vectors index
BEDROCK_NON_FILTERABLE_METADATA_KEYS = [
    "AMAZON_BEDROCK_TEXT",
    "AMAZON_BEDROCK_METADATA",
]

iam_client = boto3.client("iam", region_name=region)
s3_client = boto3.client("s3", region_name=region)
ec2_client = boto3.client("ec2", region_name=region)
s3files_client = boto3.client("s3files", region_name=region)
s3vectors_client = boto3.client("s3vectors", region_name=region)
cloudfront_client = boto3.client("cloudfront", region_name=region)
cognito_idp_client = boto3.client("cognito-idp", region_name=region)
secretsmanager_client = boto3.client("secretsmanager", region_name=region)
agentcore_control_client = boto3.client(
    "bedrock-agentcore-control",
    region_name=region,
)

WORKING_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(WORKING_DIR, "application", "config.json")
SKILLS_DIR = os.path.join(WORKING_DIR, "skills")
SKILLS_S3_PREFIX = "skills"


def s3_vectors_bucket_arn(bucket_name: str | None = None) -> str:
    """ARN for an S3 vector bucket."""
    name = bucket_name or vector_bucket_name
    return f"arn:aws:s3vectors:{region}:{account_id}:bucket/{name}"


def s3_vectors_index_arn(
    index_name: str | None = None,
    bucket_name: str | None = None,
) -> str:
    """ARN for a vector index within an S3 vector bucket."""
    idx = index_name or vector_index_name
    return f"{s3_vectors_bucket_arn(bucket_name)}/index/{idx}"


def _bucket_name() -> str:
    """Project-scoped bucket (same pattern as strands-work)."""
    return f"storage-for-{project_name}-{account_id}-{region}"


def _cloudfront_comment() -> str:
    # Distinct from UI CloudFront (CloudFront-for-{project}) in ecs_web.py.
    return f"CloudFront-S3-for-{project_name}"


def _oai_comment() -> str:
    return f"OAI for {project_name}"


def setup_logging(log_level=logging.INFO):
    """Setup logging configuration."""
    log_format = "%(asctime)s - %(levelname)s - %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    logging.basicConfig(
        level=log_level,
        format=log_format,
        datefmt=date_format,
        handlers=[
            logging.StreamHandler(),
        ],
    )

    return logging.getLogger(__name__)


logger = setup_logging()


def harness_name_for_api(name: str) -> str:
    """
    Map projectName to CreateHarness harnessName.
    Only for harnessName: replace '-' with '_' (API disallows hyphens).
    """
    normalized = (name or "").replace("-", "_")
    if not _HARNESS_NAME_API_RE.fullmatch(normalized):
        logger.error(
            "CreateHarness harnessName must match [a-zA-Z][a-zA-Z0-9_]{0,39} "
            f"(after '-'→'_'): got {normalized!r} from projectName={name!r}"
        )
        sys.exit(1)
    return normalized


def get_max_output_tokens(model_id: str = "") -> int:
    """Return max output tokens (`max_tokens` cap) per Amazon Bedrock Anthropic Claude model cards."""
    mid = model_id.lower()
    if "claude-opus-4-7" in mid or "claude-opus-4-6" in mid:
        return 128000
    if "claude-opus-4-5" in mid:
        return 64000
    if "claude-opus-4" in mid or "claude-4-opus" in mid:
        return 128000
    if "claude-sonnet-4" in mid or "claude-4-sonnet" in mid or "claude-haiku-4" in mid:
        return 64000
    return 8192


def create_iam_role(
    role_name: str,
    assume_role_policy: Dict,
    managed_policies: Optional[List[str]] = None,
    description: Optional[str] = None,
) -> tuple[str, bool]:
    """Create IAM role (or update trust/policies if it already exists).

    Returns (role_arn, created) where created is True only for a newly created role.
    """
    logger.debug(f"Creating IAM role: {role_name}")

    try:
        response = iam_client.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(assume_role_policy),
            Description=description or f"Role for {role_name}",
        )
        role_arn = response["Role"]["Arn"]
        logger.debug(f"Role created: {role_arn}")

        if managed_policies:
            logger.debug(f"Attaching {len(managed_policies)} managed policies")
            for policy_arn in managed_policies:
                iam_client.attach_role_policy(
                    RoleName=role_name,
                    PolicyArn=policy_arn,
                )
                logger.debug(f"Attached policy: {policy_arn}")

        logger.info(f"✓ IAM role created: {role_name}")
        return role_arn, True

    except ClientError as e:
        if e.response["Error"]["Code"] == "EntityAlreadyExists":
            logger.warning(f"IAM role already exists: {role_name}")
            response = iam_client.get_role(RoleName=role_name)
            role_arn = response["Role"]["Arn"]

            try:
                logger.info(f"Updating trust policy for existing role: {role_name}")
                iam_client.update_assume_role_policy(
                    RoleName=role_name,
                    PolicyDocument=json.dumps(assume_role_policy),
                )
                logger.info(f"✓ Updated trust policy for role: {role_name}")
            except ClientError as trust_policy_error:
                logger.error(
                    f"✗ Failed to update trust policy for role {role_name}: "
                    f"{trust_policy_error}"
                )
                raise

            if managed_policies:
                try:
                    attached = iam_client.list_attached_role_policies(RoleName=role_name)
                    current = {
                        p["PolicyArn"] for p in attached["AttachedPolicies"]
                    }
                    for policy_arn in managed_policies:
                        if policy_arn not in current:
                            iam_client.attach_role_policy(
                                RoleName=role_name,
                                PolicyArn=policy_arn,
                            )
                            logger.debug(f"Attached missing policy: {policy_arn}")
                except ClientError as policy_error:
                    logger.warning(f"Could not update managed policies: {policy_error}")

            return role_arn, False
        logger.error(f"Failed to create IAM role {role_name}: {e}")
        raise


def attach_inline_policy(role_name: str, policy_name: str, policy_document: Dict):
    """Attach or update inline policy to IAM role."""
    logger.debug(f"Attaching/updating inline policy {policy_name} to {role_name}")

    try:
        iam_client.put_role_policy(
            RoleName=role_name,
            PolicyName=policy_name[:128],
            PolicyDocument=json.dumps(policy_document),
        )
        logger.debug(f"Policy {policy_name} attached/updated successfully")
    except ClientError as e:
        logger.error(f"Error attaching/updating policy {policy_name}: {e}")
        raise


def load_config(config_path: str) -> Dict:
    """Load application config, creating defaults when missing."""
    global project_name, region, account_id
    global vector_index_name, vector_bucket_name
    global agentcore_control_client, s3_client, cloudfront_client
    global ec2_client, s3files_client, s3vectors_client

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except Exception as e:
        logger.warning(f"Error loading config ({config_path}): {e}; creating defaults")
        config = {
            "projectName": project_name,
            "region": region,
            "accountId": account_id,
        }
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)

    # Script constant is authoritative — do not let a copied config.json rename the project.
    region = config.get("region") or region
    raw_account = config.get("accountId")
    if raw_account is not None and str(raw_account).strip() != "":
        account_id = str(raw_account).strip()
    else:
        account_id = str(sts_client.get_caller_identity()["Account"])
        config["accountId"] = account_id

    config["projectName"] = project_name
    config["region"] = region
    config["accountId"] = account_id

    vector_index_name = project_name
    vector_bucket_name = f"{project_name}-{account_id}"

    agentcore_control_client = boto3.client(
        "bedrock-agentcore-control",
        region_name=region,
    )
    s3_client = boto3.client("s3", region_name=region)
    ec2_client = boto3.client("ec2", region_name=region)
    s3files_client = boto3.client("s3files", region_name=region)
    s3vectors_client = boto3.client("s3vectors", region_name=region)
    cloudfront_client = boto3.client("cloudfront", region_name=region)
    return config


def _bedrock_knowledge_base_trust_policy() -> Dict:
    """Trust policy for Bedrock Knowledge Base service role (AWS recommended)."""
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "bedrock.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": account_id},
                    "ArnLike": {
                        "aws:SourceArn": (
                            f"arn:aws:bedrock:{region}:{account_id}:knowledge-base/*"
                        )
                    },
                },
            }
        ],
    }


def wait_for_iam_role_propagation(role_name: str, wait_seconds: int = 15) -> None:
    """Wait for IAM role and inline policies to propagate."""
    logger.info(f"  Waiting {wait_seconds}s for IAM role propagation: {role_name}")
    time.sleep(wait_seconds)

    expected_policies = {
        f"kb-bedrock-policy-for-{project_name}",
        f"kb-s3-policy-for-{project_name}",
        f"kb-opensearch-policy-for-{project_name}",
        f"kb-s3vectors-policy-for-{project_name}",
    }
    for attempt in range(3):
        try:
            attached = iam_client.list_role_policies(RoleName=role_name)
            missing = expected_policies - set(attached.get("PolicyNames", []))
            if not missing:
                logger.info("  ✓ Knowledge Base role inline policies are attached")
                return
            logger.debug(
                f"  Waiting for inline policies (attempt {attempt + 1}/3): {sorted(missing)}"
            )
        except ClientError as e:
            logger.debug(f"  Could not list role policies yet: {e}")
        time.sleep(5)

    logger.warning(
        "  Some Knowledge Base role inline policies may not be visible yet; continuing"
    )


def _project_s3_bucket_arns() -> Tuple[str, str]:
    """Return (bucket ARN, object ARN) for the project storage bucket."""
    bucket_arn = f"arn:aws:s3:::{_bucket_name()}"
    return bucket_arn, f"{bucket_arn}/*"


def create_knowledge_base_role() -> str:
    """Create Knowledge Base IAM role with least-privilege policies."""
    logger.info("[3/12] Creating Knowledge Base IAM role")
    role_name = f"role-knowledge-base-for-{project_name}-{region}"

    assume_role_policy = _bedrock_knowledge_base_trust_policy()
    role_arn, role_created = create_iam_role(
        role_name,
        assume_role_policy,
        description="Bedrock Knowledge Base service role",
    )
    bucket_arn, object_arn = _project_s3_bucket_arns()

    bedrock_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "InvokeEmbeddingModels",
                "Effect": "Allow",
                "Action": [
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    "bedrock:GetInferenceProfile",
                    "bedrock:GetFoundationModel",
                ],
                "Resource": [
                    "arn:aws:bedrock:*::foundation-model/*",
                    f"arn:aws:bedrock:{region}:{account_id}:inference-profile/*",
                    f"arn:aws:bedrock:{region}:*:inference-profile/*",
                ],
            }
        ],
    }
    attach_inline_policy(role_name, f"kb-bedrock-policy-for-{project_name}", bedrock_policy)

    s3_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "ListKnowledgeBaseBucket",
                "Effect": "Allow",
                "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
                "Resource": [bucket_arn],
            },
            {
                "Sid": "ReadKnowledgeBaseObjects",
                "Effect": "Allow",
                "Action": ["s3:GetObject"],
                "Resource": [object_arn],
            },
        ],
    }
    attach_inline_policy(role_name, f"kb-s3-policy-for-{project_name}", s3_policy)

    opensearch_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "OpenSearchServerlessAccess",
                "Effect": "Allow",
                "Action": ["aoss:APIAccessAll"],
                "Resource": [f"arn:aws:aoss:{region}:{account_id}:collection/*"],
            }
        ],
    }
    attach_inline_policy(role_name, f"kb-opensearch-policy-for-{project_name}", opensearch_policy)

    vector_arn = s3_vectors_bucket_arn()
    s3vectors_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "S3VectorsAccess",
                "Effect": "Allow",
                "Action": [
                    "s3vectors:GetVectorBucket",
                    "s3vectors:ListVectorBuckets",
                    "s3vectors:GetIndex",
                    "s3vectors:ListIndexes",
                    "s3vectors:QueryVectors",
                    "s3vectors:GetVectors",
                    "s3vectors:PutVectors",
                    "s3vectors:DeleteVectors",
                    "s3vectors:ListVectors",
                ],
                "Resource": [
                    vector_arn,
                    f"{vector_arn}/index/*",
                ],
            }
        ],
    }
    attach_inline_policy(
        role_name, f"kb-s3vectors-policy-for-{project_name}", s3vectors_policy
    )

    if role_created:
        wait_for_iam_role_propagation(role_name)
    else:
        logger.info(f"  Skipping IAM wait (KB role already exists: {role_name})")
    logger.info(f"✓ Knowledge Base role ready: {role_arn}")
    return role_arn


def check_knowledge_base_exists() -> Optional[str]:
    """Check if Knowledge Base exists and return its ID if found."""
    bedrock_agent_client = boto3.client("bedrock-agent", region_name=region)

    try:
        kb_list = bedrock_agent_client.list_knowledge_bases()
        for kb in kb_list.get("knowledgeBaseSummaries", []):
            if kb["name"] == project_name:
                logger.debug(f"Knowledge Base found: {kb['knowledgeBaseId']}")
                return kb["knowledgeBaseId"]
        return None
    except Exception as e:
        logger.debug(f"Error checking Knowledge Base existence: {e}")
        return None


def delete_knowledge_base(knowledge_base_id: str) -> None:
    """Delete Knowledge Base and its data sources."""
    bedrock_agent_client = boto3.client("bedrock-agent", region_name=region)

    try:
        try:
            data_sources = bedrock_agent_client.list_data_sources(
                knowledgeBaseId=knowledge_base_id,
                maxResults=100,
            )
            for ds in data_sources.get("dataSourceSummaries", []):
                try:
                    bedrock_agent_client.delete_data_source(
                        knowledgeBaseId=knowledge_base_id,
                        dataSourceId=ds["dataSourceId"],
                    )
                    logger.debug(f"Deleted data source: {ds['dataSourceId']}")
                except Exception as e:
                    logger.warning(f"Failed to delete data source {ds['dataSourceId']}: {e}")
        except Exception as e:
            logger.debug(f"Error listing/deleting data sources: {e}")

        bedrock_agent_client.delete_knowledge_base(knowledgeBaseId=knowledge_base_id)
        logger.info(f"Deleted Knowledge Base: {knowledge_base_id}")

        logger.debug("Waiting for Knowledge Base deletion to complete...")
        max_wait = 60
        waited = 0
        while waited < max_wait:
            try:
                kb_response = bedrock_agent_client.get_knowledge_base(
                    knowledgeBaseId=knowledge_base_id
                )
                status = kb_response["knowledgeBase"]["status"]
                if status == "DELETED":
                    break
                time.sleep(5)
                waited += 5
            except ClientError as e:
                if e.response["Error"]["Code"] == "ResourceNotFoundException":
                    logger.debug("Knowledge Base deletion confirmed")
                    break
                raise

    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            logger.debug(f"Knowledge Base {knowledge_base_id} already deleted")
        else:
            logger.error(f"Failed to delete Knowledge Base {knowledge_base_id}: {e}")
            raise


def create_s3_vectors_store() -> Dict[str, str]:
    """Create S3 vector bucket and index for Bedrock Knowledge Base."""
    logger.info("[4/12] Creating S3 Vectors store (vector bucket + index)")

    vector_bucket_arn = s3_vectors_bucket_arn()
    index_arn = s3_vectors_index_arn()

    try:
        s3vectors_client.create_vector_bucket(vectorBucketName=vector_bucket_name)
        logger.info(f"  ✓ Vector bucket created: {vector_bucket_name}")
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("ConflictException", "ResourceAlreadyExistsException"):
            logger.warning(f"  Vector bucket already exists: {vector_bucket_name}")
            try:
                existing = s3vectors_client.get_vector_bucket(
                    vectorBucketName=vector_bucket_name
                )
                vector_bucket_arn = existing["vectorBucket"]["vectorBucketArn"]
            except ClientError:
                pass
        else:
            logger.error(f"Failed to create vector bucket: {e}")
            raise

    try:
        response = s3vectors_client.create_index(
            vectorBucketName=vector_bucket_name,
            indexName=vector_index_name,
            dataType=embedding_data_type,
            dimension=embedding_dimensions,
            distanceMetric=distance_metric,
            metadataConfiguration={
                "nonFilterableMetadataKeys": BEDROCK_NON_FILTERABLE_METADATA_KEYS,
            },
        )
        index_arn = response.get("indexArn", index_arn)
        logger.info(f"  ✓ Vector index created: {vector_index_name}")
        logger.info("  Waiting for vector index to be ready...")
        time.sleep(15)
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("ConflictException", "ResourceAlreadyExistsException"):
            logger.warning(f"  Vector index already exists: {vector_index_name}")
            try:
                existing = s3vectors_client.get_index(
                    vectorBucketName=vector_bucket_name,
                    indexName=vector_index_name,
                )
                index_arn = existing["index"]["indexArn"]
            except ClientError:
                pass
        else:
            logger.error(f"Failed to create vector index: {e}")
            raise

    logger.info("✓ S3 Vectors store ready")
    logger.info(f"  Vector bucket ARN: {vector_bucket_arn}")
    logger.info(f"  Vector index ARN: {index_arn}")

    return {
        "vectorBucketName": vector_bucket_name,
        "vectorBucketArn": vector_bucket_arn,
        "indexName": vector_index_name,
        "indexArn": index_arn,
    }


def ensure_data_source(
    bedrock_agent_client,
    knowledge_base_id: str,
    s3_bucket_name: str,
) -> str:
    """Create S3 data source with default parser when missing."""
    data_sources = bedrock_agent_client.list_data_sources(
        knowledgeBaseId=knowledge_base_id,
        maxResults=100,
    )
    for ds in data_sources.get("dataSourceSummaries", []):
        if ds["name"] == s3_bucket_name:
            logger.info(f"  Data source already exists: {ds['dataSourceId']}")
            return ds["dataSourceId"]

    logger.info("  Creating data source with default parser...")
    data_source_response = bedrock_agent_client.create_data_source(
        knowledgeBaseId=knowledge_base_id,
        name=s3_bucket_name,
        description=f"S3 data source: {s3_bucket_name}",
        dataDeletionPolicy="RETAIN",
        dataSourceConfiguration={
            "type": "S3",
            "s3Configuration": {
                "bucketArn": f"arn:aws:s3:::{s3_bucket_name}",
                "inclusionPrefixes": ["docs/"],
            },
        },
        vectorIngestionConfiguration={
            "chunkingConfiguration": {
                "chunkingStrategy": "FIXED_SIZE",
                "fixedSizeChunkingConfiguration": {
                    "maxTokens": 300,
                    "overlapPercentage": 20,
                },
            },
        },
    )
    data_source_id = data_source_response["dataSource"]["dataSourceId"]
    logger.info(f"  ✓ Data source created: {data_source_id}")
    return data_source_id


def create_knowledge_base_with_s3_vectors(
    s3_vectors_info: Dict[str, str], knowledge_base_role_arn: str, s3_bucket_name: str
) -> Tuple[str, str]:
    """Create Knowledge Base with S3 Vectors as the vector store."""
    logger.info("[KB] Creating Knowledge Base with S3 Vectors")

    bedrock_agent_client = boto3.client("bedrock-agent", region_name=region)

    try:
        logger.info("  Checking if Knowledge Base already exists...")
        kb_list = bedrock_agent_client.list_knowledge_bases()
        for kb in kb_list.get("knowledgeBaseSummaries", []):
            if kb["name"] == project_name:
                logger.warning(f"Knowledge Base already exists: {kb['knowledgeBaseId']}")

                kb_details = bedrock_agent_client.get_knowledge_base(
                    knowledgeBaseId=kb["knowledgeBaseId"]
                )
                storage = kb_details["knowledgeBase"]["storageConfiguration"]
                s3_cfg = storage.get("s3VectorsConfiguration", {})
                kb_index_arn = s3_cfg.get("indexArn")
                storage_type = storage.get("type")

                if storage_type != "S3_VECTORS" or kb_index_arn != s3_vectors_info["indexArn"]:
                    logger.warning("Knowledge Base is not using the expected S3 Vectors index:")
                    logger.warning(f"  Storage type: {storage_type}")
                    logger.warning(f"  Current index ARN: {kb_index_arn}")
                    logger.warning(f"  Expected index ARN: {s3_vectors_info['indexArn']}")

                    delete_knowledge_base(kb["knowledgeBaseId"])
                    break

                logger.info("Knowledge Base is using correct S3 Vectors index")
                data_source_id = ensure_data_source(
                    bedrock_agent_client, kb["knowledgeBaseId"], s3_bucket_name
                )
                return kb["knowledgeBaseId"], data_source_id
        logger.info("  Knowledge Base does not exist. Creating new one...")
    except Exception as e:
        logger.debug(f"Error checking existing Knowledge Base: {e}")

    logger.info("  Verifying Knowledge Base role configuration...")
    try:
        role_response = iam_client.get_role(
            RoleName=f"role-knowledge-base-for-{project_name}-{region}"
        )
        policy_doc = role_response["Role"]["AssumeRolePolicyDocument"]
        if isinstance(policy_doc, str):
            trust_policy = json.loads(policy_doc)
        else:
            trust_policy = policy_doc
        logger.debug(f"  Role trust policy: {json.dumps(trust_policy, indent=2)}")

        statements = trust_policy.get("Statement", [])
        bedrock_allowed = False
        for statement in statements:
            if statement.get("Effect") == "Allow":
                principal = statement.get("Principal", {})
                if principal.get("Service") == "bedrock.amazonaws.com":
                    bedrock_allowed = True
                    break

        if not bedrock_allowed:
            logger.error(
                "  ✗ Knowledge Base role trust policy does not allow bedrock.amazonaws.com"
            )
            logger.error(
                "  Please update the role trust policy manually or delete and recreate the role"
            )
            raise Exception("Knowledge Base role trust policy is incorrect")

        logger.info("  ✓ Knowledge Base role trust policy is correct")
    except ClientError as role_error:
        logger.error(f"  ✗ Failed to verify Knowledge Base role: {role_error}")
        raise

    logger.debug(
        f"Creating Knowledge Base with S3 Vectors index: {s3_vectors_info['indexArn']}"
    )
    response = bedrock_agent_client.create_knowledge_base(
        name=project_name,
        description="Knowledge base with default parser (S3 Vectors)",
        roleArn=knowledge_base_role_arn,
        tags={project_name: "true"},
        knowledgeBaseConfiguration={
            "type": "VECTOR",
            "vectorKnowledgeBaseConfiguration": {
                "embeddingModelArn": (
                    f"arn:aws:bedrock:{region}::foundation-model/"
                    "amazon.titan-embed-text-v2:0"
                ),
                "embeddingModelConfiguration": {
                    "bedrockEmbeddingModelConfiguration": {
                        "dimensions": embedding_dimensions,
                        "embeddingDataType": "FLOAT32",
                    }
                },
            },
        },
        storageConfiguration={
            "type": "S3_VECTORS",
            "s3VectorsConfiguration": {
                "vectorBucketArn": s3_vectors_info["vectorBucketArn"],
                "indexArn": s3_vectors_info["indexArn"],
            },
        },
    )

    knowledge_base_id = response["knowledgeBase"]["knowledgeBaseId"]
    logger.info(f"✓ Knowledge Base created: {knowledge_base_id}")

    logger.info("  Waiting for Knowledge Base to be active...")
    while True:
        kb_response = bedrock_agent_client.get_knowledge_base(
            knowledgeBaseId=knowledge_base_id
        )
        status = kb_response["knowledgeBase"]["status"]

        if status == "ACTIVE":
            logger.info("  Knowledge Base is now active")
            break
        if status == "FAILED":
            raise Exception("Knowledge Base creation failed")

        logger.debug(f"  Knowledge Base status: {status} (waiting...)")
        time.sleep(10)

    data_source_id = ensure_data_source(
        bedrock_agent_client, knowledge_base_id, s3_bucket_name
    )
    return knowledge_base_id, data_source_id


def _kb_mcp_repository_name() -> str:
    """ECR / Agent Runtime name: knowledge_base_of_{project} (hyphens → underscores)."""
    return f"knowledge_base_of_{project_name}".replace("-", "_")


def knowledge_base_mcp_url(agent_runtime_arn: str, runtime_region: str | None = None) -> str:
    """Streamable-HTTP MCP endpoint for an IAM-auth AgentCore Runtime."""
    r = runtime_region or region
    encoded = agent_runtime_arn.replace(":", "%3A").replace("/", "%2F")
    return (
        f"https://bedrock-agentcore.{r}.amazonaws.com/runtimes/"
        f"{encoded}/invocations?qualifier=DEFAULT"
    )


def create_knowledge_base_mcp_role() -> str:
    """IAM role assumed by the Knowledge Base MCP AgentCore Runtime."""
    logger.info("[6.1/14] Creating Knowledge Base MCP Runtime IAM role")
    role_name = f"role-kb-mcp-for-{project_name}-{region}"
    if len(role_name) > 64:
        role_name = f"role-kb-mcp-{project_name[:20]}-{region}"[:64]

    assume_role_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                "Action": "sts:AssumeRole",
            },
            {
                "Effect": "Allow",
                "Principal": {"AWS": f"arn:aws:iam::{account_id}:root"},
                "Action": "sts:AssumeRole",
            },
        ],
    }
    role_arn, _ = create_iam_role(
        role_name,
        assume_role_policy,
        description="Execution role for Knowledge Base MCP AgentCore Runtime",
    )

    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "KnowledgeBaseRetrieve",
                "Effect": "Allow",
                "Action": [
                    "bedrock:Retrieve",
                    "bedrock:RetrieveAndGenerate",
                ],
                "Resource": [
                    f"arn:aws:bedrock:{region}:{account_id}:knowledge-base/*",
                ],
            },
            {
                "Sid": "ListKnowledgeBases",
                "Effect": "Allow",
                "Action": ["bedrock:ListKnowledgeBases", "bedrock:GetKnowledgeBase"],
                "Resource": ["*"],
            },
            {
                "Sid": "EcrPull",
                "Effect": "Allow",
                "Action": [
                    "ecr:GetAuthorizationToken",
                    "ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:DescribeImages",
                    "ecr:DescribeRepositories",
                ],
                "Resource": ["*"],
            },
            {
                "Sid": "CloudWatchLogs",
                "Effect": "Allow",
                "Action": [
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "logs:DescribeLogGroups",
                    "logs:DescribeLogStreams",
                ],
                "Resource": [
                    f"arn:aws:logs:{region}:{account_id}:log-group:/aws/bedrock-agentcore/*",
                    f"arn:aws:logs:{region}:{account_id}:log-group:/aws/bedrock-agentcore/*:log-stream:*",
                ],
            },
            {
                "Sid": "CloudWatchMetrics",
                "Effect": "Allow",
                "Action": [
                    "cloudwatch:PutMetricData",
                    "xray:PutTraceSegments",
                    "xray:PutTelemetryRecords",
                ],
                "Resource": ["*"],
            },
        ],
    }
    attach_inline_policy(
        role_name,
        f"kb-mcp-inline-for-{project_name}"[:128],
        policy,
    )
    logger.info(f"✓ Knowledge Base MCP Runtime role ready: {role_arn}")
    return role_arn


def _ensure_ecr_repository(repository_name: str) -> None:
    ecr = boto3.client("ecr", region_name=region)
    try:
        ecr.describe_repositories(repositoryNames=[repository_name])
        logger.info(f"  ECR repository exists: {repository_name}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "RepositoryNotFoundException":
            raise
        logger.info(f"  Creating ECR repository: {repository_name}")
        ecr.create_repository(repositoryName=repository_name)


def _docker_ecr_login() -> None:
    ecr = boto3.client("ecr", region_name=region)
    token = ecr.get_authorization_token()["authorizationData"][0]["authorizationToken"]
    username, password = base64.b64decode(token).decode("utf-8").split(":")
    registry = f"{account_id}.dkr.ecr.{region}.amazonaws.com"
    process = subprocess.Popen(
        ["docker", "login", "--username", username, "--password-stdin", registry],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _, stderr = process.communicate(input=password)
    if process.returncode != 0:
        raise RuntimeError(f"Docker ECR login failed: {stderr}")


def _run_docker(cmd: List[str], description: str) -> None:
    logger.info(f"  {description}: {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def push_knowledge_base_mcp_image() -> Tuple[str, str]:
    """Build MCP/knowledge-base image and push to ECR. Returns (repository, tag)."""
    logger.info("[6.2/14] Building Knowledge Base MCP Docker image and pushing to ECR")

    if not shutil.which("docker"):
        raise RuntimeError("docker is required to build the Knowledge Base MCP image")
    if not os.path.isdir(KB_MCP_DIR):
        raise RuntimeError(f"MCP directory not found: {KB_MCP_DIR}")

    try:
        boto3.client("sts").get_caller_identity()
    except NoCredentialsError as e:
        raise RuntimeError("AWS credentials are not configured") from e

    repository = _kb_mcp_repository_name()
    image_tag = datetime.now().strftime("%Y%m%d%H%M%S")
    local_tag = f"{repository}:{image_tag}"
    ecr_uri = f"{account_id}.dkr.ecr.{region}.amazonaws.com/{repository}:{image_tag}"

    _ensure_ecr_repository(repository)
    _docker_ecr_login()
    _run_docker(
        [
            "docker",
            "build",
            "--platform",
            "linux/arm64",
            "--provenance=false",
            "--sbom=false",
            "-t",
            local_tag,
            KB_MCP_DIR,
        ],
        "Building Docker image",
    )
    _run_docker(["docker", "tag", local_tag, ecr_uri], "Tagging for ECR")
    _run_docker(["docker", "push", ecr_uri], "Pushing to ECR")
    logger.info(f"✓ Pushed Knowledge Base MCP image: {ecr_uri}")
    return repository, image_tag


def _find_agent_runtime_by_name(runtime_name: str) -> Optional[Dict]:
    client = agentcore_control_client
    next_token = None
    while True:
        kwargs = {}
        if next_token:
            kwargs["nextToken"] = next_token
        response = client.list_agent_runtimes(**kwargs)
        for item in response.get("agentRuntimes", []):
            if item.get("agentRuntimeName") == runtime_name:
                return item
        next_token = response.get("nextToken")
        if not next_token:
            return None


def create_or_update_knowledge_base_mcp_runtime(
    role_arn: str,
    repository: str,
    image_tag: str,
    knowledge_base_id: str,
    sharing_url: str = "",
) -> Dict[str, str]:
    """Create or update AgentCore Runtime (MCP protocol) for Knowledge Base retrieve."""
    logger.info("[6.3/14] Creating/updating Knowledge Base MCP AgentCore Runtime")
    runtime_name = repository
    container_uri = (
        f"{account_id}.dkr.ecr.{region}.amazonaws.com/{repository}:{image_tag}"
    )
    env_vars = {
        "AWS_REGION": region,
        "AWS_DEFAULT_REGION": region,
        "PROJECT_NAME": project_name,
        "KNOWLEDGE_BASE_ID": knowledge_base_id,
    }
    if sharing_url:
        env_vars["SHARING_URL"] = sharing_url.rstrip("/")

    existing = _find_agent_runtime_by_name(runtime_name)
    if existing:
        runtime_id = existing["agentRuntimeId"]
        logger.info(f"  Updating existing runtime: {runtime_name} ({runtime_id})")
        response = agentcore_control_client.update_agent_runtime(
            agentRuntimeId=runtime_id,
            description="Harness Knowledge Base retrieve MCP",
            agentRuntimeArtifact={
                "containerConfiguration": {"containerUri": container_uri}
            },
            roleArn=role_arn,
            networkConfiguration={"networkMode": "PUBLIC"},
            protocolConfiguration={"serverProtocol": "MCP"},
            environmentVariables=env_vars,
        )
        agent_runtime_arn = response["agentRuntimeArn"]
    else:
        logger.info(f"  Creating runtime: {runtime_name}")
        response = agentcore_control_client.create_agent_runtime(
            agentRuntimeName=runtime_name,
            description="Harness Knowledge Base retrieve MCP",
            agentRuntimeArtifact={
                "containerConfiguration": {"containerUri": container_uri}
            },
            networkConfiguration={"networkMode": "PUBLIC"},
            roleArn=role_arn,
            protocolConfiguration={"serverProtocol": "MCP"},
            environmentVariables=env_vars,
        )
        agent_runtime_arn = response["agentRuntimeArn"]

    mcp_url = knowledge_base_mcp_url(agent_runtime_arn)
    logger.info(f"✓ Knowledge Base MCP Runtime: {agent_runtime_arn}")
    logger.info(f"  MCP URL: {mcp_url}")
    return {
        "agent_runtime_arn": agent_runtime_arn,
        "knowledge_base_mcp_url": mcp_url,
        "ecr_repository": repository,
        "latest_image_tag": image_tag,
        "agent_runtime_role": role_arn,
    }


def deploy_knowledge_base_mcp(
    knowledge_base_id: str,
    sharing_url: str = "",
) -> Dict[str, str]:
    """Build/push image, deploy MCP Runtime, attach it to the project IAM Gateway.

    Harness ``remote_mcp`` does not SigV4-sign AgentCore Runtime URLs (403).
    A shared project Gateway (AWS_IAM) fronts Runtime MCP targets (KB and others).
    """
    role_arn = create_knowledge_base_mcp_role()
    repository, image_tag = push_knowledge_base_mcp_image()
    mcp_info = create_or_update_knowledge_base_mcp_runtime(
        role_arn=role_arn,
        repository=repository,
        image_tag=image_tag,
        knowledge_base_id=knowledge_base_id,
        sharing_url=sharing_url,
    )
    gateway_info = ensure_project_mcp_gateway_with_knowledge_base_target(
        agent_runtime_arn=mcp_info["agent_runtime_arn"],
        mcp_url=mcp_info["knowledge_base_mcp_url"],
    )
    mcp_info.update(gateway_info)
    return mcp_info


def _agentcore_gateway_name() -> str:
    """Shared project Gateway name (alphanumeric + hyphens; API pattern)."""
    return project_name[:48]


def _agentcore_gateway_role_name() -> str:
    role_name = f"role-agentcore-gateway-for-{project_name}-{region}"
    if len(role_name) > 64:
        role_name = f"role-ac-gw-{project_name[:20]}-{region}"[:64]
    return role_name


def create_agentcore_gateway_role() -> str:
    """IAM service role for the project AgentCore Gateway (all MCP targets)."""
    logger.info("[7.1/14] Creating project AgentCore Gateway IAM role")
    gateway_name = _agentcore_gateway_name()
    role_name = _agentcore_gateway_role_name()

    assume_role_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowAgentCoreGatewayAssume",
                "Effect": "Allow",
                "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": account_id},
                    "ArnLike": {
                        "aws:SourceArn": (
                            f"arn:aws:bedrock-agentcore:{region}:{account_id}:"
                            f"gateway/{gateway_name}-*"
                        )
                    },
                },
            }
        ],
    }
    role_arn, _ = create_iam_role(
        role_name,
        assume_role_policy,
        description=f"Service role for AgentCore Gateway ({project_name})",
    )

    # Shared gateway may front multiple Runtime MCP targets in this account/project.
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "InvokeAgentCoreRuntimeMcp",
                "Effect": "Allow",
                "Action": [
                    "bedrock-agentcore:InvokeAgentRuntime",
                    "bedrock-agentcore:InvokeAgentRuntimeForUser",
                ],
                "Resource": [
                    f"arn:aws:bedrock-agentcore:{region}:{account_id}:runtime/*",
                ],
            },
            {
                "Sid": "GatewayLogs",
                "Effect": "Allow",
                "Action": [
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "logs:DescribeLogGroups",
                    "logs:DescribeLogStreams",
                ],
                "Resource": [
                    f"arn:aws:logs:{region}:{account_id}:log-group:/aws/bedrock-agentcore/*",
                ],
            },
        ],
    }
    attach_inline_policy(
        role_name,
        f"agentcore-gateway-inline-for-{project_name}"[:128],
        policy,
    )
    logger.info(f"✓ AgentCore Gateway role ready: {role_arn}")
    return role_arn


def put_mcp_runtime_resource_policy(
    agent_runtime_arn: str,
    gateway_role_arn: str,
    harness_role_arn: Optional[str] = None,
) -> None:
    """Allow Gateway (and Harness) to InvokeAgentRuntime on an MCP Runtime."""
    principals = [gateway_role_arn]
    if harness_role_arn:
        principals.append(harness_role_arn)
    # PutResourcePolicy requires Resource to be exactly the target runtime ARN
    # (not "*"); multiple principals are allowed in one statement.
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowInvokeFromGatewayAndHarness",
                "Effect": "Allow",
                "Principal": {
                    "AWS": principals if len(principals) > 1 else principals[0]
                },
                "Action": [
                    "bedrock-agentcore:InvokeAgentRuntime",
                    "bedrock-agentcore:InvokeAgentRuntimeForUser",
                ],
                "Resource": agent_runtime_arn,
            }
        ],
    }
    try:
        agentcore_control_client.put_resource_policy(
            resourceArn=agent_runtime_arn,
            policy=json.dumps(policy),
        )
        logger.info(f"  ✓ Resource policy set on MCP Runtime: {agent_runtime_arn}")
    except ClientError as e:
        logger.warning(f"  Could not put resource policy on MCP Runtime: {e}")


def _wait_gateway_ready(gateway_id: str, timeout_seconds: int = 600) -> Dict:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        gateway = agentcore_control_client.get_gateway(gatewayIdentifier=gateway_id)
        status = gateway.get("status", "")
        if status == "READY":
            return gateway
        if status in ("FAILED", "UPDATE_UNSUCCESSFUL", "DELETE_UNSUCCESSFUL"):
            raise RuntimeError(f"Gateway {gateway_id} terminal status: {status}")
        logger.info(f"  Waiting for gateway {gateway_id}: {status}")
        time.sleep(8)
    raise TimeoutError(f"Timed out waiting for gateway {gateway_id}")


def _wait_gateway_target_ready(
    gateway_id: str, target_id: str, timeout_seconds: int = 600
) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        target = agentcore_control_client.get_gateway_target(
            gatewayIdentifier=gateway_id,
            targetId=target_id,
        )
        status = target.get("status", "")
        if status == "READY":
            logger.info(f"  Gateway target ready: {target_id}")
            return
        if status in (
            "FAILED",
            "UPDATE_UNSUCCESSFUL",
            "CREATE_UNSUCCESSFUL",
            "SYNCHRONIZE_UNSUCCESSFUL",
        ):
            raise RuntimeError(
                f"Gateway target {target_id} terminal status: {status} "
                f"reasons={target.get('statusReasons')}"
            )
        logger.info(f"  Waiting for gateway target {target_id}: {status}")
        time.sleep(8)
    raise TimeoutError(f"Timed out waiting for gateway target {target_id}")


def _find_gateway_target(gateway_id: str, target_name: str) -> Optional[Dict]:
    for target in (
        agentcore_control_client.list_gateway_targets(
            gatewayIdentifier=gateway_id
        ).get("items")
        or []
    ):
        if target.get("name") == target_name:
            return target
    return None


def _delete_gateway_target(gateway_id: str, target_id: str) -> None:
    logger.info(f"  Deleting gateway target {target_id}")
    try:
        agentcore_control_client.delete_gateway_target(
            gatewayIdentifier=gateway_id,
            targetId=target_id,
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        return
    deadline = time.time() + 300
    while time.time() < deadline:
        try:
            agentcore_control_client.get_gateway_target(
                gatewayIdentifier=gateway_id,
                targetId=target_id,
            )
            time.sleep(5)
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceNotFoundException":
                logger.info(f"  ✓ Deleted gateway target {target_id}")
                return
            raise
    raise TimeoutError(f"Timed out waiting for gateway target delete: {target_id}")


def _ensure_mcp_gateway_target(
    *,
    gateway_id: str,
    target_name: str,
    description: str,
    agent_runtime_arn: str,
    mcp_url: str,
    gateway_role_arn: str,
    result_key: str,
) -> Dict[str, str]:
    """Create/update an MCP Runtime gateway target; recreate if FAILED."""
    harness_role_arn = (
        f"arn:aws:iam::{account_id}:role/role-harness-for-{project_name}-{region}"
    )
    put_mcp_runtime_resource_policy(
        agent_runtime_arn=agent_runtime_arn,
        gateway_role_arn=gateway_role_arn,
        harness_role_arn=harness_role_arn,
    )

    existing = _find_gateway_target(gateway_id, target_name)
    target_id = existing.get("targetId") if existing else None
    if existing and existing.get("status") == "FAILED":
        logger.warning(
            f"  Gateway target '{target_name}' is FAILED; deleting before recreate"
        )
        _delete_gateway_target(gateway_id, target_id)
        target_id = None

    target_configuration = {
        "mcp": {"mcpServer": {"endpoint": mcp_url}}
    }
    credential_provider_configurations = [
        {
            "credentialProviderType": "GATEWAY_IAM_ROLE",
            "credentialProvider": {
                "iamCredentialProvider": {
                    "service": "bedrock-agentcore",
                    "region": region,
                }
            },
        }
    ]

    if not target_id:
        logger.info(f"  Creating gateway target '{target_name}' → {mcp_url}")
        created_target = agentcore_control_client.create_gateway_target(
            gatewayIdentifier=gateway_id,
            name=target_name,
            description=description,
            targetConfiguration=target_configuration,
            credentialProviderConfigurations=credential_provider_configurations,
        )
        target_id = created_target["targetId"]
    else:
        logger.info(f"  Updating gateway target {target_id}")
        agentcore_control_client.update_gateway_target(
            gatewayIdentifier=gateway_id,
            targetId=target_id,
            name=target_name,
            description=description,
            targetConfiguration=target_configuration,
            credentialProviderConfigurations=credential_provider_configurations,
        )

    # Create/Update already syncs tools; Synchronize while CREATING fails.
    _wait_gateway_target_ready(gateway_id, target_id)
    try:
        agentcore_control_client.synchronize_gateway_targets(
            gatewayIdentifier=gateway_id,
            targetIdList=[target_id],
        )
        _wait_gateway_target_ready(gateway_id, target_id)
    except ClientError as e:
        logger.warning(f"  synchronize_gateway_targets: {e}")

    return {result_key: target_id}


def ensure_project_agentcore_gateway() -> Dict[str, str]:
    """Create or reuse the shared project AgentCore Gateway (IAM inbound)."""
    logger.info("[7.2/14] Ensuring project AgentCore Gateway")
    gateway_name = _agentcore_gateway_name()
    gateway_role_arn = create_agentcore_gateway_role()

    gateway_id = None
    next_token = None
    while True:
        kwargs = {}
        if next_token:
            kwargs["nextToken"] = next_token
        resp = agentcore_control_client.list_gateways(**kwargs)
        for item in resp.get("items") or []:
            if item.get("name") == gateway_name:
                gateway_id = item["gatewayId"]
                logger.warning(f"  Gateway already exists: {gateway_name} ({gateway_id})")
                break
        if gateway_id:
            break
        next_token = resp.get("nextToken")
        if not next_token:
            break

    if not gateway_id:
        logger.info(f"  Creating gateway: {gateway_name}")
        time.sleep(12)
        created = agentcore_control_client.create_gateway(
            name=gateway_name,
            description=f"Shared IAM Gateway for {project_name} MCP runtimes",
            roleArn=gateway_role_arn,
            protocolType="MCP",
            authorizerType="AWS_IAM",
            tags={"Project": project_name, "Component": "agentcore-gateway"},
        )
        gateway_id = created["gatewayId"]
        logger.info(f"  ✓ Gateway created: {gateway_id}")

    gateway = _wait_gateway_ready(gateway_id)
    gateway_arn = gateway.get("gatewayArn") or (
        f"arn:aws:bedrock-agentcore:{region}:{account_id}:gateway/{gateway_id}"
    )
    logger.info(f"✓ Project AgentCore Gateway ready: {gateway_arn}")
    return {
        "agentcore_gateway_arn": gateway_arn,
        "agentcore_gateway_id": gateway_id,
        "agentcore_gateway_role": gateway_role_arn,
    }


def ensure_knowledge_base_gateway_target(
    gateway_id: str,
    agent_runtime_arn: str,
    mcp_url: str,
    gateway_role_arn: str,
) -> Dict[str, str]:
    """Attach Knowledge Base MCP Runtime as a target on the project Gateway."""
    return _ensure_mcp_gateway_target(
        gateway_id=gateway_id,
        target_name="knowledge-base",
        description="AgentCore Runtime MCP (knowledge base retrieve)",
        agent_runtime_arn=agent_runtime_arn,
        mcp_url=mcp_url,
        gateway_role_arn=gateway_role_arn,
        result_key="knowledge_base_mcp_gateway_target_id",
    )


def ensure_project_mcp_gateway_with_knowledge_base_target(
    agent_runtime_arn: str,
    mcp_url: str,
) -> Dict[str, str]:
    """Ensure shared project Gateway exists and KB MCP Runtime is a target."""
    gateway_info = ensure_project_agentcore_gateway()
    target_info = ensure_knowledge_base_gateway_target(
        gateway_id=gateway_info["agentcore_gateway_id"],
        agent_runtime_arn=agent_runtime_arn,
        mcp_url=mcp_url,
        gateway_role_arn=gateway_info["agentcore_gateway_role"],
    )
    gateway_info.update(target_info)
    return gateway_info


def refresh_knowledge_base_mcp_env(
    mcp_info: Dict[str, str],
    knowledge_base_id: str,
    sharing_url: str,
) -> None:
    """Update MCP runtime env after CloudFront / KB ids are finalized."""
    arn = mcp_info.get("agent_runtime_arn") or ""
    if not arn:
        return
    runtime_name = _kb_mcp_repository_name()
    existing = _find_agent_runtime_by_name(runtime_name)
    if not existing:
        logger.warning("  Knowledge Base MCP runtime not found for env refresh")
        return
    role_arn = mcp_info.get("agent_runtime_role") or ""
    repository = mcp_info.get("ecr_repository") or runtime_name
    image_tag = mcp_info.get("latest_image_tag")
    if not image_tag or not role_arn:
        logger.warning("  Skipping MCP env refresh (missing role/image tag)")
        return
    updated = create_or_update_knowledge_base_mcp_runtime(
        role_arn=role_arn,
        repository=repository,
        image_tag=image_tag,
        knowledge_base_id=knowledge_base_id,
        sharing_url=sharing_url,
    )
    mcp_info.update(updated)
    if updated.get("knowledge_base_mcp_url"):
        gateway_info = ensure_project_mcp_gateway_with_knowledge_base_target(
            agent_runtime_arn=updated["agent_runtime_arn"],
            mcp_url=updated["knowledge_base_mcp_url"],
        )
        mcp_info.update(gateway_info)


def _artifact_share_mcp_repository_name() -> str:
    return f"artifact_share_of_{project_name}".replace("-", "_")


def create_artifact_share_mcp_role() -> str:
    """IAM role assumed by the Artifact Share MCP AgentCore Runtime."""
    logger.info("[8.1/14] Creating Artifact Share MCP Runtime IAM role")
    role_name = f"role-artifact-share-mcp-for-{project_name}-{region}"
    if len(role_name) > 64:
        role_name = f"role-artifact-share-mcp-{project_name[:20]}-{region}"[:64]

    assume_role_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                "Action": "sts:AssumeRole",
            },
            {
                "Effect": "Allow",
                "Principal": {"AWS": f"arn:aws:iam::{account_id}:root"},
                "Action": "sts:AssumeRole",
            },
        ],
    }
    role_arn, _ = create_iam_role(
        role_name,
        assume_role_policy,
        description="Execution role for Artifact Share MCP AgentCore Runtime",
    )

    bucket = _bucket_name()
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "S3ListBucket",
                "Effect": "Allow",
                "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
                "Resource": [f"arn:aws:s3:::{bucket}"],
            },
            {
                "Sid": "S3ReadSessionObjects",
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:HeadObject"],
                "Resource": [f"arn:aws:s3:::{bucket}/*"],
            },
            {
                "Sid": "S3PutSharingObjects",
                "Effect": "Allow",
                "Action": ["s3:PutObject", "s3:AbortMultipartUpload"],
                "Resource": [
                    f"arn:aws:s3:::{bucket}/artifacts/*",
                    f"arn:aws:s3:::{bucket}/images/*",
                    f"arn:aws:s3:::{bucket}/docs/*",
                ],
            },
            {
                "Sid": "EcrPull",
                "Effect": "Allow",
                "Action": [
                    "ecr:GetAuthorizationToken",
                    "ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:DescribeImages",
                    "ecr:DescribeRepositories",
                ],
                "Resource": ["*"],
            },
            {
                "Sid": "CloudWatchLogs",
                "Effect": "Allow",
                "Action": [
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "logs:DescribeLogGroups",
                    "logs:DescribeLogStreams",
                ],
                "Resource": [
                    f"arn:aws:logs:{region}:{account_id}:log-group:/aws/bedrock-agentcore/*",
                    f"arn:aws:logs:{region}:{account_id}:log-group:/aws/bedrock-agentcore/*:log-stream:*",
                ],
            },
            {
                "Sid": "CloudWatchMetrics",
                "Effect": "Allow",
                "Action": [
                    "cloudwatch:PutMetricData",
                    "xray:PutTraceSegments",
                    "xray:PutTelemetryRecords",
                ],
                "Resource": ["*"],
            },
        ],
    }
    attach_inline_policy(
        role_name,
        f"artifact-share-mcp-inline-for-{project_name}"[:128],
        policy,
    )
    logger.info(f"✓ Artifact Share MCP Runtime role ready: {role_arn}")
    return role_arn


def push_artifact_share_mcp_image() -> Tuple[str, str]:
    """Build MCP/artifact-share image and push to ECR. Returns (repository, tag)."""
    logger.info("[8.2/14] Building Artifact Share MCP Docker image and pushing to ECR")

    if not shutil.which("docker"):
        raise RuntimeError("docker is required to build the Artifact Share MCP image")
    if not os.path.isdir(ARTIFACT_SHARE_MCP_DIR):
        raise RuntimeError(f"MCP directory not found: {ARTIFACT_SHARE_MCP_DIR}")

    try:
        boto3.client("sts").get_caller_identity()
    except NoCredentialsError as e:
        raise RuntimeError("AWS credentials are not configured") from e

    repository = _artifact_share_mcp_repository_name()
    image_tag = datetime.now().strftime("%Y%m%d%H%M%S")
    local_tag = f"{repository}:{image_tag}"
    ecr_uri = f"{account_id}.dkr.ecr.{region}.amazonaws.com/{repository}:{image_tag}"

    _ensure_ecr_repository(repository)
    _docker_ecr_login()
    _run_docker(
        [
            "docker",
            "build",
            "--platform",
            "linux/arm64",
            "--provenance=false",
            "--sbom=false",
            "-t",
            local_tag,
            ARTIFACT_SHARE_MCP_DIR,
        ],
        "Building Docker image",
    )
    _run_docker(["docker", "tag", local_tag, ecr_uri], "Tagging for ECR")
    _run_docker(["docker", "push", ecr_uri], "Pushing to ECR")
    logger.info(f"✓ Pushed Artifact Share MCP image: {ecr_uri}")
    return repository, image_tag


def create_or_update_artifact_share_mcp_runtime(
    role_arn: str,
    repository: str,
    image_tag: str,
    s3_bucket_name: str,
    sharing_url: str = "",
) -> Dict[str, str]:
    """Create or update AgentCore Runtime (MCP protocol) for S3 sharing uploads."""
    logger.info("[8.3/14] Creating/updating Artifact Share MCP AgentCore Runtime")
    runtime_name = repository
    container_uri = (
        f"{account_id}.dkr.ecr.{region}.amazonaws.com/{repository}:{image_tag}"
    )
    env_vars = {
        "AWS_REGION": region,
        "AWS_DEFAULT_REGION": region,
        "PROJECT_NAME": project_name,
        "S3_BUCKET": s3_bucket_name,
        "SESSION_STORAGE_DIR": s3_files_vpc.SESSION_STORAGE_MOUNT_PATH,
    }
    if sharing_url:
        env_vars["SHARING_URL"] = sharing_url.rstrip("/")

    existing = _find_agent_runtime_by_name(runtime_name)
    if existing:
        runtime_id = existing["agentRuntimeId"]
        logger.info(f"  Updating existing runtime: {runtime_name} ({runtime_id})")
        response = agentcore_control_client.update_agent_runtime(
            agentRuntimeId=runtime_id,
            description="Harness S3 sharing MCP (CloudFront download URLs)",
            agentRuntimeArtifact={
                "containerConfiguration": {"containerUri": container_uri}
            },
            roleArn=role_arn,
            networkConfiguration={"networkMode": "PUBLIC"},
            protocolConfiguration={"serverProtocol": "MCP"},
            environmentVariables=env_vars,
        )
        agent_runtime_arn = response["agentRuntimeArn"]
    else:
        logger.info(f"  Creating runtime: {runtime_name}")
        response = agentcore_control_client.create_agent_runtime(
            agentRuntimeName=runtime_name,
            description="Harness S3 sharing MCP (CloudFront download URLs)",
            agentRuntimeArtifact={
                "containerConfiguration": {"containerUri": container_uri}
            },
            networkConfiguration={"networkMode": "PUBLIC"},
            roleArn=role_arn,
            protocolConfiguration={"serverProtocol": "MCP"},
            environmentVariables=env_vars,
        )
        agent_runtime_arn = response["agentRuntimeArn"]

    mcp_url = knowledge_base_mcp_url(agent_runtime_arn)
    logger.info(f"✓ Artifact Share MCP Runtime: {agent_runtime_arn}")
    logger.info(f"  MCP URL: {mcp_url}")
    return {
        "agent_runtime_arn": agent_runtime_arn,
        "artifact_share_mcp_url": mcp_url,
        "ecr_repository": repository,
        "latest_image_tag": image_tag,
        "agent_runtime_role": role_arn,
    }


def ensure_artifact_share_gateway_target(
    gateway_id: str,
    agent_runtime_arn: str,
    mcp_url: str,
    gateway_role_arn: str,
) -> Dict[str, str]:
    """Attach Artifact Share MCP Runtime as a target on the project Gateway."""
    return _ensure_mcp_gateway_target(
        gateway_id=gateway_id,
        target_name="artifact-share",
        description="AgentCore Runtime MCP (S3 sharing / CloudFront URLs)",
        agent_runtime_arn=agent_runtime_arn,
        mcp_url=mcp_url,
        gateway_role_arn=gateway_role_arn,
        result_key="artifact_share_mcp_gateway_target_id",
    )


def deploy_artifact_share_mcp(
    s3_bucket_name: str,
    sharing_url: str = "",
    gateway_info: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """Build/push image, deploy MCP Runtime, attach it to the project IAM Gateway."""
    role_arn = create_artifact_share_mcp_role()
    repository, image_tag = push_artifact_share_mcp_image()
    mcp_info = create_or_update_artifact_share_mcp_runtime(
        role_arn=role_arn,
        repository=repository,
        image_tag=image_tag,
        s3_bucket_name=s3_bucket_name,
        sharing_url=sharing_url,
    )
    gw = dict(gateway_info or {})
    if not gw.get("agentcore_gateway_id"):
        gw.update(ensure_project_agentcore_gateway())
    target_info = ensure_artifact_share_gateway_target(
        gateway_id=gw["agentcore_gateway_id"],
        agent_runtime_arn=mcp_info["agent_runtime_arn"],
        mcp_url=mcp_info["artifact_share_mcp_url"],
        gateway_role_arn=gw["agentcore_gateway_role"],
    )
    # Only copy shared gateway fields — do not overwrite this runtime's ARN/role/ECR.
    for key in (
        "agentcore_gateway_arn",
        "agentcore_gateway_id",
        "agentcore_gateway_role",
    ):
        if gw.get(key):
            mcp_info[key] = gw[key]
    mcp_info.update(target_info)
    return mcp_info


def refresh_artifact_share_mcp_env(
    mcp_info: Dict[str, str],
    s3_bucket_name: str,
    sharing_url: str,
) -> None:
    """Update Artifact Share MCP runtime env after CloudFront URL is finalized."""
    arn = mcp_info.get("agent_runtime_arn") or ""
    if not arn:
        return
    runtime_name = _artifact_share_mcp_repository_name()
    existing = _find_agent_runtime_by_name(runtime_name)
    if not existing:
        logger.warning("  Artifact Share MCP runtime not found for env refresh")
        return
    role_arn = mcp_info.get("agent_runtime_role") or ""
    repository = mcp_info.get("ecr_repository") or runtime_name
    image_tag = mcp_info.get("latest_image_tag")
    if not image_tag or not role_arn:
        logger.warning("  Skipping Artifact Share MCP env refresh (missing role/image tag)")
        return
    updated = create_or_update_artifact_share_mcp_runtime(
        role_arn=role_arn,
        repository=repository,
        image_tag=image_tag,
        s3_bucket_name=s3_bucket_name,
        sharing_url=sharing_url,
    )
    mcp_info.update(updated)
    gateway_id = mcp_info.get("agentcore_gateway_id") or ""
    gateway_role = mcp_info.get("agentcore_gateway_role") or ""
    if gateway_id and gateway_role and updated.get("artifact_share_mcp_url"):
        target_info = ensure_artifact_share_gateway_target(
            gateway_id=gateway_id,
            agent_runtime_arn=updated["agent_runtime_arn"],
            mcp_url=updated["artifact_share_mcp_url"],
            gateway_role_arn=gateway_role,
        )
        mcp_info.update(target_info)


def _s3_files_provisioner() -> s3_files_vpc.S3FilesVpcProvisioner:
    return s3_files_vpc.S3FilesVpcProvisioner(
        ec2_client=ec2_client,
        s3_client=s3_client,
        s3files_client=s3files_client,
        iam_client=iam_client,
        region=region,
        account_id=account_id,
        project_name=project_name,
        logger=logger,
    )


def create_s3_bucket() -> str:
    """Create S3 bucket with CORS configuration."""
    bucket_name = _bucket_name()
    logger.info(f"[1/9] Creating S3 bucket: {bucket_name}")

    try:
        logger.debug(f"Creating bucket in region: {region}")
        if region == "us-east-1":
            s3_client.create_bucket(Bucket=bucket_name)
        else:
            s3_client.create_bucket(
                Bucket=bucket_name,
                CreateBucketConfiguration={"LocationConstraint": region},
            )
        logger.debug("Bucket created successfully")

        logger.debug("Configuring public access block")
        s3_client.put_public_access_block(
            Bucket=bucket_name,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            },
        )

        logger.debug("Setting CORS configuration")
        cors_configuration = {
            "CORSRules": [
                {
                    "AllowedHeaders": ["*"],
                    "AllowedMethods": ["GET", "POST", "PUT"],
                    "AllowedOrigins": ["*"],
                }
            ]
        }
        s3_client.put_bucket_cors(
            Bucket=bucket_name,
            CORSConfiguration=cors_configuration,
        )

        logger.debug("Configuring versioning (Enabled required for S3 Files)")
        s3_client.put_bucket_versioning(
            Bucket=bucket_name,
            VersioningConfiguration={"Status": "Enabled"},
        )

        logger.debug("Creating docs and artifacts folders")
        for folder in ["docs/", "artifacts/"]:
            try:
                s3_client.put_object(Bucket=bucket_name, Key=folder, Body=b"")
                logger.debug(f"{folder} folder created successfully")
            except ClientError as e:
                logger.warning(f"Failed to create {folder} folder: {e}")

        logger.info(f"✓ S3 bucket created successfully: {bucket_name}")
        return bucket_name

    except ClientError as e:
        if e.response["Error"]["Code"] in ["BucketAlreadyExists", "BucketAlreadyOwnedByYou"]:
            logger.warning(f"S3 bucket already exists (reusing): {bucket_name}")
            # Versioning Enabled is required for S3 Files even on a reused bucket.
            try:
                s3_client.put_bucket_versioning(
                    Bucket=bucket_name,
                    VersioningConfiguration={"Status": "Enabled"},
                )
            except ClientError as ver_error:
                logger.warning(f"Failed to ensure versioning on existing bucket: {ver_error}")
            logger.debug("Creating docs and artifacts folders in existing bucket")
            for folder in ["docs/", "artifacts/"]:
                try:
                    s3_client.put_object(Bucket=bucket_name, Key=folder, Body=b"")
                    logger.debug(f"{folder} folder created successfully")
                except ClientError as folder_error:
                    if folder_error.response["Error"]["Code"] != "NoSuchBucket":
                        logger.warning(
                            f"Failed to create {folder} folder: {folder_error}"
                        )
            return bucket_name
        logger.error(f"Failed to create S3 bucket: {e}")
        raise


def _should_skip_skill_path(rel_path: str) -> bool:
    parts = rel_path.replace("\\", "/").split("/")
    skip_dirs = {"__pycache__", ".git", ".DS_Store", "node_modules"}
    if any(p in skip_dirs for p in parts):
        return True
    basename = parts[-1] if parts else ""
    if basename.endswith((".pyc", ".pyo", ".DS_Store")):
        return True
    return False


def _local_skill_names() -> set[str]:
    """Top-level skill folder names under skills/."""
    if not os.path.isdir(SKILLS_DIR):
        return set()
    return {
        name
        for name in os.listdir(SKILLS_DIR)
        if os.path.isdir(os.path.join(SKILLS_DIR, name))
        and not name.startswith(".")
        and name not in {"__pycache__", "node_modules"}
    }


def _prune_removed_skills_from_s3(s3_bucket_name: str) -> int:
    """Delete S3 skill prefixes that no longer exist under local skills/."""
    local_names = _local_skill_names()
    prefix = f"{SKILLS_S3_PREFIX}/"
    removed = 0
    try:
        paginator = s3_client.get_paginator("list_objects_v2")
        remote_names: set[str] = set()
        for page in paginator.paginate(
            Bucket=s3_bucket_name, Prefix=prefix, Delimiter="/"
        ):
            for cp in page.get("CommonPrefixes") or []:
                p = (cp.get("Prefix") or "").rstrip("/")
                name = p.split("/")[-1] if p else ""
                if name:
                    remote_names.add(name)
        orphans = sorted(remote_names - local_names)
        for name in orphans:
            orphan_prefix = f"{prefix}{name}/"
            logger.info(
                f"  pruning removed skill from S3: s3://{s3_bucket_name}/{orphan_prefix}"
            )
            for page in paginator.paginate(Bucket=s3_bucket_name, Prefix=orphan_prefix):
                objs = page.get("Contents") or []
                if not objs:
                    continue
                s3_client.delete_objects(
                    Bucket=s3_bucket_name,
                    Delete={
                        "Objects": [{"Key": o["Key"]} for o in objs],
                        "Quiet": True,
                    },
                )
                removed += len(objs)
    except ClientError as e:
        logger.warning(f"  skill prune skipped: {e}")
        return removed
    if removed:
        logger.info(f"✓ Pruned {removed} stale skill object(s) from S3")
    return removed


def upload_skills_to_s3(s3_bucket_name: str) -> int:
    """Upload skills/ to s3://{bucket}/skills/ (AgentCore S3 skill layout).

    Also deletes remote skill prefixes that are no longer present locally
    (e.g. renamed s3-sharing → artifact-share MCP).
    """
    logger.info(f"[2/9] Uploading skills to s3://{s3_bucket_name}/{SKILLS_S3_PREFIX}/")

    if not os.path.isdir(SKILLS_DIR):
        logger.warning(f"Skills directory not found: {SKILLS_DIR}; skipping upload")
        return 0

    uploaded = 0
    failed = 0
    for root, dirs, files in os.walk(SKILLS_DIR):
        dirs[:] = [d for d in dirs if d not in {"__pycache__", ".git", "node_modules"}]
        for filename in files:
            local_path = os.path.join(root, filename)
            rel_path = os.path.relpath(local_path, SKILLS_DIR)
            if _should_skip_skill_path(rel_path):
                continue
            s3_key = f"{SKILLS_S3_PREFIX}/{rel_path.replace(os.sep, '/')}"
            content_type, _ = mimetypes.guess_type(local_path)
            upload_kwargs = {}
            if content_type:
                upload_kwargs["ExtraArgs"] = {"ContentType": content_type}
            try:
                s3_client.upload_file(
                    local_path,
                    s3_bucket_name,
                    s3_key,
                    **upload_kwargs,
                )
                uploaded += 1
                logger.debug(f"  uploaded: s3://{s3_bucket_name}/{s3_key}")
            except ClientError as e:
                failed += 1
                logger.error(f"  failed: {rel_path}: {e}")

    if failed:
        raise RuntimeError(
            f"Skills upload incomplete: {uploaded} ok, {failed} failed "
            f"(from {SKILLS_DIR})"
        )

    _prune_removed_skills_from_s3(s3_bucket_name)

    logger.info(
        f"✓ Uploaded {uploaded} skill file(s) to "
        f"s3://{s3_bucket_name}/{SKILLS_S3_PREFIX}/"
    )
    return uploaded


def create_cloudfront_distribution(s3_bucket_name: str) -> Dict[str, str]:
    """Create project-scoped CloudFront distribution with S3 origin (file sharing)."""
    logger.info("[9/12] Creating CloudFront distribution (S3 sharing)")
    comment = _cloudfront_comment()
    oai_cmt = _oai_comment()

    try:
        distributions = cloudfront_client.list_distributions()
        for dist in distributions.get("DistributionList", {}).get("Items", []):
            if comment in dist.get("Comment", ""):
                if dist.get("Enabled", False):
                    logger.warning(
                        f"CloudFront distribution already exists (reusing): "
                        f"{dist['DomainName']}"
                    )
                    return {"id": dist["Id"], "domain": dist["DomainName"]}
                logger.warning(
                    f"CloudFront distribution exists but is disabled: {dist['DomainName']}"
                )
                dist_config_response = cloudfront_client.get_distribution_config(
                    Id=dist["Id"]
                )
                dist_config = dist_config_response["DistributionConfig"]
                dist_config["Enabled"] = True
                cloudfront_client.update_distribution(
                    Id=dist["Id"],
                    DistributionConfig=dist_config,
                    IfMatch=dist_config_response["ETag"],
                )
                return {"id": dist["Id"], "domain": dist["DomainName"]}
    except Exception as e:
        logger.debug(f"Error checking existing CloudFront distributions: {e}")

    oai_id = None
    try:
        oai_list = cloudfront_client.list_cloud_front_origin_access_identities()
        for oai in oai_list.get("CloudFrontOriginAccessIdentityList", {}).get(
            "Items", []
        ):
            if oai_cmt in oai.get("Comment", ""):
                oai_id = oai["Id"]
                logger.info(f"  Using existing Origin Access Identity: {oai_id}")
                break
        if not oai_id:
            oai_response = cloudfront_client.create_cloud_front_origin_access_identity(
                CloudFrontOriginAccessIdentityConfig={
                    "CallerReference": (
                        f"{project_name}-s3-oai-{int(time.time())}"
                    ),
                    "Comment": oai_cmt,
                }
            )
            oai_id = oai_response["CloudFrontOriginAccessIdentity"]["Id"]
            logger.info(f"  Created Origin Access Identity: {oai_id}")
    except ClientError as e:
        logger.error(f"Failed to handle Origin Access Identity: {e}")
        raise

    bucket_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowCloudFrontAccess",
                "Effect": "Allow",
                "Principal": {
                    "AWS": (
                        f"arn:aws:iam::cloudfront:user/"
                        f"CloudFront Origin Access Identity {oai_id}"
                    )
                },
                "Action": "s3:GetObject",
                "Resource": f"arn:aws:s3:::{s3_bucket_name}/*",
            }
        ],
    }
    try:
        time.sleep(10)
        s3_client.put_bucket_policy(
            Bucket=s3_bucket_name, Policy=json.dumps(bucket_policy)
        )
        logger.info("  Updated S3 bucket policy for CloudFront access")
    except ClientError as e:
        logger.error(f"Failed to update S3 bucket policy: {e}")
        raise

    origin_id = f"s3-{project_name}"
    distribution_config = {
        "CallerReference": f"{project_name}-s3-{int(time.time())}",
        "Comment": comment,
        "DefaultRootObject": "index.html",
        "DefaultCacheBehavior": {
            "TargetOriginId": origin_id,
            "ViewerProtocolPolicy": "redirect-to-https",
            "AllowedMethods": {
                "Quantity": 2,
                "Items": ["GET", "HEAD"],
                "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]},
            },
            "CachePolicyId": "658327ea-f89d-4fab-a63d-7e88639e58f6",
            "Compress": True,
        },
        "Origins": {
            "Quantity": 1,
            "Items": [
                {
                    "Id": origin_id,
                    "DomainName": f"{s3_bucket_name}.s3.{region}.amazonaws.com",
                    "S3OriginConfig": {
                        "OriginAccessIdentity": (
                            f"origin-access-identity/cloudfront/{oai_id}"
                        )
                    },
                }
            ],
        },
        "Enabled": True,
        "PriceClass": "PriceClass_200",
    }

    response = cloudfront_client.create_distribution(
        DistributionConfig=distribution_config
    )
    distribution_id = response["Distribution"]["Id"]
    distribution_domain = response["Distribution"]["DomainName"]
    logger.info(f"CloudFront distribution created: {distribution_domain}")
    logger.info(f"  S3 origin: {s3_bucket_name}")
    return {"id": distribution_id, "domain": distribution_domain}


def create_harness_execution_role(
    knowledge_base_mcp_runtime_arn: Optional[str] = None,
    artifact_share_mcp_runtime_arn: Optional[str] = None,
    agentcore_gateway_arn: Optional[str] = None,
) -> str:
    """Create IAM execution role for Bedrock AgentCore harness."""
    logger.info("[9/14] Creating Harness execution IAM role")
    role_name = f"role-harness-for-{project_name}-{region}"
    if len(role_name) > 64:
        logger.error(
            f"IAM RoleName exceeds 64 characters ({len(role_name)}): {role_name!r}. "
            "Shorten projectName or region in config."
        )
        sys.exit(1)

    # Trust: bedrock-agentcore.amazonaws.com only (no SourceArn).
    # CreateHarness validates AssumeRole against this shape.
    assume_role_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowAgentCoreAssumeHarness",
                "Effect": "Allow",
                "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }

    role_arn, role_created = create_iam_role(
        role_name,
        assume_role_policy,
        description="Execution role for Bedrock AgentCore harness",
    )

    harness_execution_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "BedrockModelInvocation",
                "Effect": "Allow",
                "Action": [
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    "bedrock:GetInferenceProfile",
                    "bedrock:GetFoundationModel",
                ],
                "Resource": [
                    "arn:aws:bedrock:*::foundation-model/*",
                    f"arn:aws:bedrock:{region}:{account_id}:inference-profile/*",
                ],
            },
            {
                "Sid": "KnowledgeBaseRetrieve",
                "Effect": "Allow",
                "Action": [
                    "bedrock:Retrieve",
                    "bedrock:RetrieveAndGenerate",
                    "bedrock:StartIngestionJob",
                    "bedrock:GetIngestionJob",
                    "bedrock:ListIngestionJobs",
                ],
                "Resource": [
                    f"arn:aws:bedrock:{region}:{account_id}:knowledge-base/*",
                ],
            },
            {
                "Sid": "AgentCoreAccess",
                "Effect": "Allow",
                "Action": ["bedrock-agentcore:*"],
                "Resource": ["*"],
            },
            {
                "Sid": "CloudWatchLogsAgentCore",
                "Effect": "Allow",
                "Action": [
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "logs:DescribeLogStreams",
                ],
                "Resource": [
                    f"arn:aws:logs:{region}:{account_id}:log-group:/aws/bedrock-agentcore/*",
                ],
            },
            # VPC-mode harness pulls the managed image from AWS ECR
            # (e.g. 796669927364.dkr.ecr.<region>.amazonaws.com/harness-<region>).
            # Without these, InvokeHarness fails with Runtime health check timeout.
            {
                "Sid": "EcrManagedImagePull",
                "Effect": "Allow",
                "Action": [
                    "ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:BatchCheckLayerAvailability",
                ],
                "Resource": [f"arn:aws:ecr:{region}:*:repository/harness-*"],
            },
            {
                "Sid": "EcrManagedImageToken",
                "Effect": "Allow",
                "Action": ["ecr:GetAuthorizationToken"],
                "Resource": ["*"],
            },
            {
                "Sid": "AgentCoreSkillS3ListBucket",
                "Effect": "Allow",
                "Action": ["s3:ListBucket"],
                "Resource": [f"arn:aws:s3:::{_bucket_name()}"],
            },
            {
                "Sid": "AgentCoreSkillS3GetObject",
                "Effect": "Allow",
                "Action": ["s3:GetObject"],
                "Resource": [f"arn:aws:s3:::{_bucket_name()}/*"],
            },
            # Sharing prefix writes (CloudFront); artifact-share MCP also uses its own role
            {
                "Sid": "AgentCoreSharingS3PutObject",
                "Effect": "Allow",
                "Action": ["s3:PutObject"],
                "Resource": [
                    f"arn:aws:s3:::{_bucket_name()}/artifacts/*",
                    f"arn:aws:s3:::{_bucket_name()}/images/*",
                    f"arn:aws:s3:::{_bucket_name()}/docs/*",
                ],
            },
        ],
    }

    # Harness → Gateway (SigV4) → Knowledge Base MCP Runtime (SigV4).
    # remote_mcp cannot call IAM AgentCore Runtime URLs (403 without SigV4).
    if agentcore_gateway_arn:
        harness_execution_policy["Statement"].append(
            {
                "Sid": "InvokeAgentCoreGateway",
                "Effect": "Allow",
                "Action": ["bedrock-agentcore:InvokeGateway"],
                "Resource": [
                    agentcore_gateway_arn,
                    f"{agentcore_gateway_arn}/*",
                ],
            }
        )
    runtime_arns: List[str] = []
    if knowledge_base_mcp_runtime_arn:
        runtime_arns.extend(
            [
                knowledge_base_mcp_runtime_arn,
                f"{knowledge_base_mcp_runtime_arn}/*",
            ]
        )
    if artifact_share_mcp_runtime_arn:
        runtime_arns.extend(
            [
                artifact_share_mcp_runtime_arn,
                f"{artifact_share_mcp_runtime_arn}/*",
            ]
        )
    if runtime_arns:
        harness_execution_policy["Statement"].append(
            {
                "Sid": "InvokeProjectMcpRuntimes",
                "Effect": "Allow",
                "Action": [
                    "bedrock-agentcore:InvokeAgentRuntime",
                    "bedrock-agentcore:InvokeAgentRuntimeForUser",
                ],
                "Resource": runtime_arns,
            }
        )

    attach_inline_policy(
        role_name,
        f"harness-exec-inline-for-{role_name}",
        harness_execution_policy,
    )
    # CreateHarness validates AssumeRole immediately after a brand-new role.
    if role_created:
        wait_seconds = 20
        logger.info(
            f"  Waiting {wait_seconds}s for IAM role/policy propagation "
            f"before CreateHarness..."
        )
        time.sleep(wait_seconds)
    logger.info(f"✓ Harness execution role ready: {role_arn}")
    return role_arn


USER_PREFERENCE_PROMPT = (
    "You are tasked with analyzing conversations to extract the user's preferences. You'll be analyzing two sets of data:\n"
    "<past_conversation>\n"
    "[Past conversations between the user and system will be placed here for context]\n"
    "</past_conversation>\n"
    "<current_conversation>\n"
    "[The current conversation between the user and system will be placed here]\n"
    "</current_conversation>\n"
    "Your job is to identify and categorize the user's preferences into two main types:\n"
    "- Explicit preferences: Directly stated preferences by the user.\n"
    "- Implicit preferences: Inferred from patterns, repeated inquiries, or contextual clues. Take a close look at user's request for implicit preferences.\n"
    "For explicit preference, extract only preference that the user has explicitly shared. Do not infer user's preference.\n"
    "For implicit preference, it is allowed to infer user's preference, but only the ones with strong signals, such as requesting something multiple times.\n"
    "Use Korean.\n"
)

SUMMARY_PROMPT = (
    "You will be given a text block and a list of summaries you previously generated when available.\n"
    "<task>\n"
    "- When the previously generated is not available, your goal is to summarize the given text block.\n"
    "- When there is existing summary, your goal is to extend summary by taking into account the given text block.\n"
    "- If there are queries/topics specified in the text block, your generated summary need to cover those queries/topics.\n"
    "- If there are instructions in the text block **guiding you how to generate summary**, you MUST follow them.\n"
    "</task>\n"
    "Use Korean.\n"
)

SEMANTIC_PROMPT = (
    "You are a long-term memory extraction agent supporting a lifelong learning system.\n"
    "Your task is to identify and extract meaningful information about the users from a given list of messages.\n"
    "Analyze the conversation and extract structured information about the user according to the schema below.\n"
    "Only include details that are explicitly stated or can be logically inferred from the conversation.\n"
    "- Extract information ONLY from the user messages. You should use assistant messages only as supporting context.\n"
    "- If the conversation contains no relevant or noteworthy information, return an empty list.\n"
    "- Do NOT extract anything from prior conversation history, even if provided. Use it solely for context.\n"
    "- Do NOT incorporate external knowledge.\n"
    "- Avoid duplicate extractions.\n"
    "Use Korean.\n"
)

SEMANTIC_CONSOLIDATION_PROMPT = (
    "You consolidate newly extracted facts with existing long-term semantic memories.\n"
    "- Merge duplicates; keep the most specific and recent facts.\n"
    "- Do not invent facts that were not extracted.\n"
    "- Prefer clear, atomic statements in Korean.\n"
    "Use Korean.\n"
)

MEMORY_EXTRACTION_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


def _shared_memory_strategies() -> list:
    """UserPreference + Summary + Semantic (one of each kind per memory_id)."""
    return [
        {
            "customMemoryStrategy": {
                "name": "UserPreference",
                "namespaces": ["/users/{actorId}/preferences"],
                "configuration": {
                    "userPreferenceOverride": {
                        "extraction": {
                            "modelId": MEMORY_EXTRACTION_MODEL_ID,
                            "appendToPrompt": USER_PREFERENCE_PROMPT,
                        }
                    }
                },
            }
        },
        {
            "customMemoryStrategy": {
                "name": "Summary",
                "namespaces": ["/users/{actorId}/sessions/{sessionId}"],
                "configuration": {
                    "summaryOverride": {
                        "consolidation": {
                            "modelId": MEMORY_EXTRACTION_MODEL_ID,
                            "appendToPrompt": SUMMARY_PROMPT,
                        }
                    }
                },
            }
        },
        {
            "customMemoryStrategy": {
                "name": "Semantic",
                "namespaces": ["/users/{actorId}/facts"],
                "configuration": {
                    "semanticOverride": {
                        "extraction": {
                            "modelId": MEMORY_EXTRACTION_MODEL_ID,
                            "appendToPrompt": SEMANTIC_PROMPT,
                        },
                        "consolidation": {
                            "modelId": MEMORY_EXTRACTION_MODEL_ID,
                            "appendToPrompt": SEMANTIC_CONSOLIDATION_PROMPT,
                        },
                    }
                },
            }
        },
    ]


def create_agentcore_memory_role() -> str:
    """Create AgentCore Memory IAM role."""
    logger.info("[4/9] Creating AgentCore Memory IAM role")
    role_name = f"role-agentcore-memory-for-{project_name}-{region}"

    # Trust must include aws:SourceAccount / aws:SourceArn; CreateMemory rejects otherwise.
    # https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/long-term-configuring-custom-strategies.html
    assume_role_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "MemoryAssumeRolePolicy",
                "Effect": "Allow",
                "Principal": {
                    "Service": "bedrock-agentcore.amazonaws.com"
                },
                "Action": "sts:AssumeRole",
                "Condition": {
                    "StringEquals": {
                        "aws:SourceAccount": account_id
                    },
                    "ArnLike": {
                        "aws:SourceArn": (
                            f"arn:aws:bedrock-agentcore:{region}:{account_id}:*"
                        )
                    },
                },
            }
        ],
    }

    role_arn, role_created = create_iam_role(role_name, assume_role_policy)

    memory_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                "Resource": [
                    "arn:aws:bedrock:*::foundation-model/*",
                    "arn:aws:bedrock:*:*:inference-profile/*",
                ],
                "Condition": {
                    "StringEquals": {
                        "aws:ResourceAccount": account_id
                    }
                },
            }
        ],
    }
    attach_inline_policy(role_name, f"agentcore-memory-policy-for-{project_name}", memory_policy)

    # CreateMemory validates trust immediately after a brand-new role.
    if role_created:
        logger.info("  Waiting for IAM role trust policy to propagate...")
        time.sleep(10)

    return role_arn


def create_agentcore_memory(role_arn: str, user_id: str = "installer") -> str:
    """
    Create AgentCore Memory with shared UserPreference / Summary / Semantic strategies.

    user_id is unused for strategy naming — kept for call-site compatibility.
    User isolation uses {actorId}/{sessionId} namespace templates from CreateEvent.
    """
    logger.info("[5/9] Creating AgentCore Memory")

    memory_client = MemoryClient(region_name=region)
    # CreateMemory name: [a-zA-Z][a-zA-Z0-9_]{0,47} — hyphens not allowed.
    memory_name = harness_name_for_api(project_name)

    memories = memory_client.list_memories()
    for memory in memories:
        if memory.get("id", "").split("-")[0] == memory_name:
            memory_id = memory.get("id")
            logger.info(f"  Memory already exists: {memory_id}")
            return memory_id

    strategies = _shared_memory_strategies()
    result = memory_client.create_memory_and_wait(
        name=memory_name,
        description=f"Memory for {project_name}",
        event_expiry_days=365,
        strategies=strategies,
        memory_execution_role_arn=role_arn,
    )
    memory_id = result.get("id")
    names = [s["customMemoryStrategy"]["name"] for s in strategies]
    logger.info(f"  ✓ Memory created: {memory_id} (strategies={names})")
    return memory_id


def _memory_arn_from_id(memory_id: str) -> str:
    return f"arn:aws:bedrock-agentcore:{region}:{account_id}:memory/{memory_id}"


BASE_SYSTEM_PROMPT = (
    "당신의 이름은 서연이고, 질문에 친근한 방식으로 대답하도록 설계된 대화형 AI입니다.\n"
    "상황에 맞는 구체적인 세부 정보를 충분히 제공합니다.\n"
    "모르는 질문을 받으면 솔직히 모른다고 말합니다.\n"
    "한국어로 답변하세요.\n"
    "\n"
    "InvokeHarness 호출마다 systemPrompt에 actor_id와 ARTIFACTS_DIR이 명시됩니다.\n"
    "- ARTIFACTS_DIR = /mnt/workspace/{actor_id}/artifacts\n"
    "- 모든 산출물은 반드시 해당 ARTIFACTS_DIR 아래에 생성하세요.\n"
    "knowledge-base retrieve와 artifact-share share_artifact를 호출할 때 "
    "반드시 그 actor_id를 도구 인자로 그대로 사용하세요. "
    "닉네임·표시 이름·추측 값으로 바꾸지 마세요.\n"
    "\n"
    "## Agent Workflow\n"
    "1. 사용자 입력을 받는다\n"
    "2. 요청에 맞는 skill/도구가 있으면 해당 지침에 따라 작업을 수행한다\n"
    "3. 코드 실행·파일 생성 시 반드시 ARTIFACTS_DIR(actor별 폴더) 아래에 산출물을 저장한다\n"
    "4. 결과 파일이 있으면 artifact-share MCP의 share_artifact로 공유 URL을 제공한다 "
    "(filepath는 'artifacts/파일명' 또는 ARTIFACTS_DIR 절대경로; actor_id 필수; "
    "세션 스토리지에서 artifacts/{actor_id}/... 로 복사된다)\n"
    "5. 최종 결과를 사용자에게 전달한다\n"
)


def _paginate_list_harnesses() -> List[Dict]:
    items: List[Dict] = []
    token = None
    while True:
        kw: Dict = {"maxResults": 50}
        if token:
            kw["nextToken"] = token
        resp = agentcore_control_client.list_harnesses(**kw)
        items.extend(resp.get("harnesses") or [])
        token = resp.get("nextToken")
        if not token:
            break
    return items


def find_harness_by_api_name(harness_api_name: str) -> Optional[Dict]:
    for h in _paginate_list_harnesses():
        if h.get("harnessName") == harness_api_name:
            return h
    return None


def ensure_harness_memory_binding(harness_id: str, agent_memory_arn: str) -> None:
    """Align harness memory binding with the ensured AgentCore Memory ARN."""
    h = agentcore_control_client.get_harness(harnessId=harness_id)["harness"]
    memory_cfg = (
        ((h.get("memory") or {}).get("agentCoreMemoryConfiguration") or {})
        if isinstance(h.get("memory"), dict)
        else {}
    )
    current = memory_cfg.get("arn")
    if current == agent_memory_arn:
        return

    logger.info(
        f"Updating harness memory: {current!r} -> {agent_memory_arn!r} "
        f"(harnessId={harness_id})"
    )
    update_harness_safe(
        harness_id,
        memory={
            "optionalValue": {
                "agentCoreMemoryConfiguration": {
                    "arn": agent_memory_arn,
                },
            },
        },
    )


def wait_for_harness_ready(harness_id: str, timeout_seconds: int = 300) -> str:
    """Poll until harness reaches READY; return harness ARN."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        res = agentcore_control_client.get_harness(harnessId=harness_id)
        h = res["harness"]
        status = h["status"]
        if status == "READY":
            harness_arn = h["arn"]
            logger.info(f"✓ Harness ready: {harness_arn}")
            return harness_arn
        if status in (
            "FAILED",
            "CREATE_FAILED",
            "UPDATE_FAILED",
            "DELETING",
            "DELETE_UNSUCCESSFUL",
            "DELETE_FAILED",
        ):
            reason = h.get("failureReason") or h.get("statusReason") or ""
            raise RuntimeError(
                f"Harness {harness_id} entered terminal status: {status}"
                + (f" — {reason}" if reason else "")
            )
        logger.info(f"  Waiting for harness ({harness_id}) status: {status}")
        time.sleep(5)
    raise TimeoutError(f"Harness {harness_id} did not reach READY within {timeout_seconds}s")


def update_harness_safe(harness_id: str, *, timeout_seconds: int = 600, **kwargs) -> None:
    """UpdateHarness only when READY; retry on ConflictException (still UPDATING)."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        remaining = max(30, int(deadline - time.time()))
        wait_for_harness_ready(harness_id, timeout_seconds=remaining)
        try:
            agentcore_control_client.update_harness(
                harnessId=harness_id,
                **kwargs,
            )
            return
        except ClientError as e:
            code = (e.response.get("Error") or {}).get("Code", "")
            msg = (e.response.get("Error") or {}).get("Message", "")
            if code != "ConflictException" and "while it is UPDATING" not in msg:
                raise
            logger.warning(
                "  UpdateHarness ConflictException (harness still UPDATING); "
                "waiting and retrying..."
            )
            time.sleep(8)
    raise TimeoutError(
        f"Timed out updating harness {harness_id} within {timeout_seconds}s"
    )

def ensure_harness_environment(
    harness_id: str,
    environment: Dict,
) -> None:
    """Update harness environment when VPC / S3 Files mount differs from desired."""
    h = agentcore_control_client.get_harness(harnessId=harness_id)["harness"]
    current_env = h.get("environment") or {}
    desired = environment or {}
    current_rt = (current_env.get("agentCoreRuntimeEnvironment") or {})
    desired_rt = (desired.get("agentCoreRuntimeEnvironment") or {})

    current_mode = (current_rt.get("networkConfiguration") or {}).get("networkMode")
    desired_mode = (desired_rt.get("networkConfiguration") or {}).get("networkMode")
    current_fs = current_rt.get("filesystemConfigurations") or []
    desired_fs = desired_rt.get("filesystemConfigurations") or []

    current_ap = ""
    for cfg in current_fs:
        ap = (cfg.get("s3FilesAccessPoint") or {}).get("accessPointArn") or ""
        if ap:
            current_ap = ap
            break
    desired_ap = ""
    for cfg in desired_fs:
        ap = (cfg.get("s3FilesAccessPoint") or {}).get("accessPointArn") or ""
        if ap:
            desired_ap = ap
            break

    if current_mode == desired_mode and current_ap == desired_ap:
        logger.info(
            f"  Harness environment already matches "
            f"(networkMode={desired_mode}, s3FilesAccessPoint={'set' if desired_ap else 'none'})"
        )
        return

    logger.info(
        f"  Updating harness environment: networkMode {current_mode!r} -> {desired_mode!r}, "
        f"s3FilesAccessPoint {'set' if desired_ap else 'none'}"
    )
    update_harness_safe(
        harness_id,
        environment=desired,
        environmentVariables=_harness_environment_variables(),
    )


def _harness_environment_variables(
    *,
    s3_bucket_name: str | None = None,
    sharing_url: str | None = None,
    knowledge_base_id: str | None = None,
    data_source_id: str | None = None,
) -> Dict[str, str]:
    """Env vars for Harness runtime (session storage + artifact-share + KB)."""
    env: Dict[str, str] = {
        "LOG_LEVEL": "info",
        "SESSION_STORAGE_DIR": s3_files_vpc.SESSION_STORAGE_MOUNT_PATH,
    }
    bucket = s3_bucket_name or _bucket_name()
    if bucket:
        env["S3_BUCKET"] = bucket
    if sharing_url:
        env["SHARING_URL"] = sharing_url.rstrip("/")
    if knowledge_base_id:
        env["KNOWLEDGE_BASE_ID"] = knowledge_base_id
    if data_source_id:
        env["DATA_SOURCE_ID"] = data_source_id
    return env


def ensure_harness_sharing_env(
    harness_id: str,
    s3_bucket_name: str,
    sharing_url: str,
    knowledge_base_id: str | None = None,
    data_source_id: str | None = None,
) -> None:
    """Inject S3_BUCKET / SHARING_URL / KB ids for runtime skills."""
    if not harness_id or not s3_bucket_name:
        return
    url = (sharing_url or "").rstrip("/")
    if not url:
        logger.warning("  SHARING_URL empty; artifact-share will fall back to console URLs")
    env_vars = _harness_environment_variables(
        s3_bucket_name=s3_bucket_name,
        sharing_url=url or None,
        knowledge_base_id=knowledge_base_id,
        data_source_id=data_source_id,
    )
    logger.info(
        f"  Updating harness env for artifact-share/KB: "
        f"S3_BUCKET={s3_bucket_name}, SHARING_URL={url or '(none)'}, "
        f"KNOWLEDGE_BASE_ID={knowledge_base_id or '(none)'}"
    )
    update_harness_safe(
        harness_id,
        environmentVariables=env_vars,
    )


def ensure_harness_system_prompt(harness_id: str) -> None:
    """Keep CreateHarness systemPrompt in sync on re-install."""
    if not harness_id:
        return
    desired = [{"text": BASE_SYSTEM_PROMPT}]
    h = agentcore_control_client.get_harness(harnessId=harness_id)["harness"]
    current = h.get("systemPrompt") or []
    if current == desired:
        logger.info("  Harness systemPrompt already up to date")
        return
    logger.info(f"  Updating harness systemPrompt (harnessId={harness_id})")
    update_harness_safe(
        harness_id,
        systemPrompt=desired,
    )


def _default_harness_tools(
    agentcore_gateway_arn: Optional[str] = None,
) -> List[Dict]:
    """Default CreateHarness / UpdateHarness tools list."""
    tools: List[Dict] = [
        {
            "type": "remote_mcp",
            "name": "exa",
            "config": {"remoteMcp": {"url": "https://mcp.exa.ai/mcp"}},
        },
        {
            "type": "remote_mcp",
            "name": "aws_knowledge",
            "config": {
                "remoteMcp": {
                    "url": "https://knowledge-mcp.global.api.aws",
                }
            },
        },
        {
            "type": "agentcore_browser",
            "name": "browser",
            "config": {"agentCoreBrowser": {}},
        },
        {
            "type": "agentcore_code_interpreter",
            "name": "code",
            "config": {"agentCoreCodeInterpreter": {}},
        },
    ]
    if agentcore_gateway_arn:
        tools.append(
            {
                "type": "agentcore_gateway",
                "name": "knowledge_base",
                "config": {
                    "agentCoreGateway": {
                        "gatewayArn": agentcore_gateway_arn,
                        "outboundAuth": {"awsIam": {}},
                    }
                },
            }
        )
    return tools


def ensure_harness_tools(
    harness_id: str,
    agentcore_gateway_arn: Optional[str] = None,
) -> None:
    """Ensure Knowledge Base MCP Gateway (and defaults) are present on the harness."""
    desired = _default_harness_tools(agentcore_gateway_arn)
    h = agentcore_control_client.get_harness(harnessId=harness_id)["harness"]
    current = h.get("tools") or []
    current_by_name = {
        t.get("name"): t for t in current if isinstance(t, dict) and t.get("name")
    }
    changed = False
    for tool in desired:
        name = tool.get("name")
        existing = current_by_name.get(name)
        if not existing:
            changed = True
            break
        if name == "knowledge_base":
            cur_arn = (
                ((existing.get("config") or {}).get("agentCoreGateway") or {}).get(
                    "gatewayArn"
                )
            )
            new_arn = (
                ((tool.get("config") or {}).get("agentCoreGateway") or {}).get(
                    "gatewayArn"
                )
            )
            if cur_arn != new_arn or existing.get("type") != "agentcore_gateway":
                changed = True
                break
    if not changed and agentcore_gateway_arn:
        logger.info("  Harness tools already include agentcore gateway")
        return
    if not agentcore_gateway_arn and not changed:
        return

    logger.info(
        f"  Updating harness tools "
        f"(agentcore gateway={'set' if agentcore_gateway_arn else 'none'})"
    )
    merged_by_name = dict(current_by_name)
    for tool in desired:
        merged_by_name[tool["name"]] = tool
    update_harness_safe(
        harness_id,
        tools=list(merged_by_name.values()),
    )

def create_or_get_harness(
    execution_role_arn: str,
    agent_memory_arn: str,
    s3_files_info: Optional[Dict[str, object]] = None,
    agentcore_gateway_arn: Optional[str] = None,
) -> Dict[str, str]:
    """Create AgentCore Harness or reuse an existing one by API name."""
    logger.info("[8/12] Creating AgentCore Harness")

    harness_api_name = harness_name_for_api(project_name)
    logger.info(f"  harnessName: {harness_api_name} (from projectName={project_name!r})")

    model_id = DEFAULT_MODEL_ID
    system_prompt = [{"text": BASE_SYSTEM_PROMPT}]
    environment = s3_files_vpc.build_harness_runtime_environment(s3_files_info)
    tools = _default_harness_tools(agentcore_gateway_arn)

    existing = find_harness_by_api_name(harness_api_name)
    if existing:
        harness_id = existing["harnessId"]
        try:
            status = agentcore_control_client.get_harness(harnessId=harness_id)[
                "harness"
            ].get("status")
        except ClientError:
            status = None
        if status in ("CREATE_FAILED", "UPDATE_FAILED", "FAILED", "DELETE_FAILED"):
            reason = ""
            try:
                reason = (
                    agentcore_control_client.get_harness(harnessId=harness_id)[
                        "harness"
                    ].get("failureReason")
                    or ""
                )
            except ClientError:
                pass
            logger.warning(
                f"Harness {harness_api_name!r} is {status} "
                f"(harnessId={harness_id}); deleting to recreate."
                + (f" reason={reason!r}" if reason else "")
            )
            try:
                agentcore_control_client.delete_harness(
                    harnessId=harness_id,
                    clientToken=str(uuid.uuid4()),
                )
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
                    raise
            # Wait until gone
            deadline = time.time() + 600
            while time.time() < deadline:
                try:
                    agentcore_control_client.get_harness(harnessId=harness_id)
                    time.sleep(5)
                except ClientError as e:
                    if e.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                        break
                    raise
            else:
                raise TimeoutError(f"Timed out deleting failed harness {harness_id}")
            existing = None
        else:
            logger.warning(
                f"Harness {harness_api_name!r} already exists (harnessId={harness_id}); "
                "skipping CreateHarness."
            )

    if not existing:
        try:
            response = agentcore_control_client.create_harness(
                harnessName=harness_api_name,
                executionRoleArn=execution_role_arn,
                model={
                    "bedrockModelConfig": {
                        "modelId": model_id,
                        "maxTokens": get_max_output_tokens(model_id),
                    }
                },
                systemPrompt=system_prompt,
                tools=tools,
                memory={
                    "agentCoreMemoryConfiguration": {
                        "arn": agent_memory_arn,
                    }
                },
                truncation={
                    "strategy": "sliding_window",
                    "config": {"slidingWindow": {"messagesCount": 50}},
                },
                maxIterations=20,
                maxTokens=50000,
                timeoutSeconds=300,
                environment=environment,
                environmentVariables=_harness_environment_variables(),
                tags={"Project": project_name, "Env": "dev"},
            )
            harness_id = response["harness"]["harnessId"]
            logger.info(f"  ✓ Harness created: {harness_id}")
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "ConflictException":
                raise
            rerun = find_harness_by_api_name(harness_api_name)
            if not rerun:
                logger.error(
                    "CreateHarness ConflictException but harness not found by name "
                    f"{harness_api_name!r}. Re-run after checking console."
                )
                raise
            harness_id = rerun["harnessId"]
            logger.info(
                f"CreateHarness conflict; using existing harnessId={harness_id} "
                f"({harness_api_name!r})."
            )

    ensure_harness_memory_binding(harness_id, agent_memory_arn)
    ensure_harness_environment(harness_id, environment)
    ensure_harness_system_prompt(harness_id)
    ensure_harness_tools(harness_id, agentcore_gateway_arn)
    harness_arn = wait_for_harness_ready(harness_id)

    return {
        "harness_id": harness_id,
        "harness_arn": harness_arn,
        "harness_name": harness_api_name,
    }


def _find_cognito_user_pool_id(pool_name: str) -> Optional[str]:
    next_token = None
    while True:
        kwargs: Dict = {"MaxResults": 60}
        if next_token:
            kwargs["NextToken"] = next_token
        response = cognito_idp_client.list_user_pools(**kwargs)
        for pool in response.get("UserPools", []):
            if pool.get("Name") == pool_name:
                return pool["Id"]
        next_token = response.get("NextToken")
        if not next_token:
            return None


def _cognito_pool_id_from_config() -> Optional[str]:
    """Return cognito_user_pool_id from application/config.json if present."""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)
        pool_id = (config.get("cognito_user_pool_id") or "").strip()
        return pool_id or None
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def _cognito_user_pool_exists(user_pool_id: str) -> bool:
    try:
        cognito_idp_client.describe_user_pool(UserPoolId=user_pool_id)
        return True
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("ResourceNotFoundException", "UserPoolNotFoundException"):
            return False
        raise


def _resolve_cognito_user_pool_id(pool_name: Optional[str] = None) -> Optional[str]:
    """Prefer a live config.json pool id, then list User Pools by project name."""
    pool_id = _cognito_pool_id_from_config()
    if pool_id and _cognito_user_pool_exists(pool_id):
        return pool_id
    return _find_cognito_user_pool_id(pool_name or project_name)


def _find_cognito_client_id(user_pool_id: str, client_name: str) -> Optional[str]:
    next_token = None
    while True:
        kwargs: Dict = {"UserPoolId": user_pool_id, "MaxResults": 60}
        if next_token:
            kwargs["NextToken"] = next_token
        response = cognito_idp_client.list_user_pool_clients(**kwargs)
        for client in response.get("UserPoolClients", []):
            if client.get("ClientName") == client_name:
                return client["ClientId"]
        next_token = response.get("NextToken")
        if not next_token:
            return None


def _cognito_password_valid(password: str) -> Optional[str]:
    """Return an error message if password does not meet Cognito policy, else None."""
    if len(password) < 8:
        return "Password must be at least 8 characters"
    if not any(c.isupper() for c in password):
        return "Password must include at least one uppercase letter"
    if not any(c.islower() for c in password):
        return "Password must include at least one lowercase letter"
    if not any(c.isdigit() for c in password):
        return "Password must include at least one number"
    return None


def prompt_cognito_admin_password() -> str:
    """Prompt the operator for the Cognito admin password (confirmed twice).

    Password is read with getpass so it is not echoed to the terminal.
    Non-interactive runs are rejected — do not pass a default/hardcoded password.
    """
    if not sys.stdin.isatty():
        raise RuntimeError(
            "Cognito admin password must be entered interactively. "
            "Run `python installer.py` in a terminal and type the password when prompted."
        )
    logger.info("")
    logger.info("Cognito admin user registration")
    logger.info(f"  Username: {COGNITO_ADMIN_USERNAME}")
    logger.info(
        "  Password policy: min 8 chars, uppercase, lowercase, number "
        "(symbols optional)"
    )
    while True:
        password = getpass.getpass(
            f"Enter password for Cognito admin '{COGNITO_ADMIN_USERNAME}': "
        )
        error = _cognito_password_valid(password)
        if error:
            logger.warning(f"  {error}. Try again.")
            continue
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            logger.warning("  Passwords do not match. Try again.")
            continue
        return password


def _cognito_admin_exists(user_pool_id: str, username: str) -> bool:
    try:
        cognito_idp_client.admin_get_user(UserPoolId=user_pool_id, Username=username)
        return True
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("UserNotFoundException", "ResourceNotFoundException"):
            return False
        raise


def _create_cognito_admin_user(user_pool_id: str, username: str, password: str) -> None:
    cognito_idp_client.admin_create_user(
        UserPoolId=user_pool_id,
        Username=username,
        TemporaryPassword=password,
        MessageAction="SUPPRESS",
    )
    cognito_idp_client.admin_set_user_password(
        UserPoolId=user_pool_id,
        Username=username,
        Password=password,
        Permanent=True,
    )


def create_cognito_user_pool(
    admin_password: Optional[str] = None,
) -> Dict[str, str]:
    """Create Cognito User Pool (named project_name), app client, and admin user.

    When the admin user does not yet exist, ``admin_password`` must be provided
    (prompted interactively at install start) — never auto-generated.
    """
    logger.info("Creating Cognito User Pool for Web UI authentication")
    pool_name = project_name
    user_pool_id = _resolve_cognito_user_pool_id(pool_name)

    if user_pool_id:
        logger.info(f"  ✓ Reusing Cognito User Pool: {user_pool_id} (name={pool_name})")
    else:
        response = cognito_idp_client.create_user_pool(
            PoolName=pool_name,
            Policies={
                "PasswordPolicy": {
                    "MinimumLength": 8,
                    "RequireUppercase": True,
                    "RequireLowercase": True,
                    "RequireNumbers": True,
                    "RequireSymbols": False,
                }
            },
            MfaConfiguration="OFF",
            AdminCreateUserConfig={"AllowAdminCreateUserOnly": True},
            Schema=[
                {
                    "Name": "email",
                    "AttributeDataType": "String",
                    "Mutable": True,
                    "Required": False,
                }
            ],
        )
        user_pool_id = response["UserPool"]["Id"]
        logger.info(f"  ✓ Cognito User Pool created: {user_pool_id} (name={pool_name})")

    client_id = _find_cognito_client_id(user_pool_id, COGNITO_CLIENT_NAME)
    if client_id:
        logger.info(f"  ✓ Reusing Cognito App Client: {client_id}")
    else:
        client_response = cognito_idp_client.create_user_pool_client(
            UserPoolId=user_pool_id,
            ClientName=COGNITO_CLIENT_NAME,
            GenerateSecret=False,
            ExplicitAuthFlows=[
                "ALLOW_USER_PASSWORD_AUTH",
                "ALLOW_REFRESH_TOKEN_AUTH",
                "ALLOW_USER_SRP_AUTH",
            ],
            PreventUserExistenceErrors="ENABLED",
        )
        client_id = client_response["UserPoolClient"]["ClientId"]
        logger.info(f"  ✓ Cognito App Client created: {client_id}")

    if _cognito_admin_exists(user_pool_id, COGNITO_ADMIN_USERNAME):
        logger.info(
            f"  ✓ Cognito admin user already exists: {COGNITO_ADMIN_USERNAME}"
        )
    else:
        password = admin_password or prompt_cognito_admin_password()
        _create_cognito_admin_user(user_pool_id, COGNITO_ADMIN_USERNAME, password)
        logger.info(f"  ✓ Cognito admin user created: {COGNITO_ADMIN_USERNAME}")

    cognito_info = {
        "cognito_user_pool_id": user_pool_id,
        "cognito_user_pool_name": pool_name,
        "cognito_client_id": client_id,
        "cognito_client_name": COGNITO_CLIENT_NAME,
        "cognito_admin_username": COGNITO_ADMIN_USERNAME,
        "cognito_region": region,
    }
    if write_config(CONFIG_PATH, cognito_info):
        logger.info("  ✓ Saved Cognito settings to application/config.json")
    return cognito_info


def get_or_create_session_signing_key(*, rotate: bool = False) -> str:
    """Ensure HMAC key for Web UI session cookies exists in Secrets Manager."""
    secret_name = SESSION_SIGNING_KEY_SECRET_NAME

    try:
        existing = secretsmanager_client.get_secret_value(SecretId=secret_name)
        current = (existing.get("SecretString") or "").strip()
        if current and not rotate:
            logger.info(
                f"  ✓ Reusing session signing key from Secrets Manager: {secret_name}"
            )
            return current
        new_value = secrets.token_urlsafe(32)
        secretsmanager_client.put_secret_value(
            SecretId=secret_name,
            SecretString=new_value,
        )
        logger.info(f"  ✓ Rotated session signing key in Secrets Manager: {secret_name}")
        return new_value
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise

    new_value = secrets.token_urlsafe(32)
    try:
        secretsmanager_client.create_secret(
            Name=secret_name,
            Description=f"HMAC signing key for {project_name} Web UI session cookies",
            SecretString=new_value,
            Tags=[
                {"Key": "Name", "Value": secret_name},
                {"Key": "Project", "Value": project_name},
            ],
        )
        logger.info(f"  ✓ Created session signing key secret: {secret_name}")
        return new_value
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceExistsException":
            if rotate:
                new_value = secrets.token_urlsafe(32)
                secretsmanager_client.put_secret_value(
                    SecretId=secret_name,
                    SecretString=new_value,
                )
                logger.info(
                    f"  ✓ Rotated session signing key in Secrets Manager: {secret_name}"
                )
                return new_value
            response = secretsmanager_client.get_secret_value(SecretId=secret_name)
            return response["SecretString"]
        raise


def write_config(config_path: str, config_data: Dict, *, merge_existing: bool = True) -> bool:
    """Write config JSON, optionally merging with existing contents."""
    existing: Dict = {}
    if merge_existing:
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(f"Could not read existing {config_path}: {e}")

    existing.update(config_data)
    try:
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)
        return True
    except Exception as e:
        logger.warning(f"Could not write {config_path}: {e}")
        return False


def build_config_from_deployment_state(
    execution_role_arn: Optional[str] = None,
    agentcore_memory_role_arn: Optional[str] = None,
    memory_id: Optional[str] = None,
    agent_memory_arn: Optional[str] = None,
    harness_info: Optional[Dict[str, str]] = None,
    s3_bucket_name: Optional[str] = None,
    cloudfront_info: Optional[Dict[str, str]] = None,
    ui_cloudfront_info: Optional[Dict[str, str]] = None,
    s3_files_info: Optional[Dict[str, object]] = None,
    vpc_info: Optional[Dict[str, object]] = None,
    ecs_info: Optional[Dict[str, str]] = None,
    image_uri: Optional[str] = None,
    image_build_tag: Optional[str] = None,
    knowledge_base_id: Optional[str] = None,
    data_source_id: Optional[str] = None,
    knowledge_base_role_arn: Optional[str] = None,
    s3_vectors_info: Optional[Dict[str, str]] = None,
    knowledge_base_mcp_info: Optional[Dict[str, str]] = None,
    artifact_share_mcp_info: Optional[Dict[str, str]] = None,
    cognito_info: Optional[Dict[str, str]] = None,
) -> Dict:
    config_data: Dict = {
        "projectName": project_name,
        "accountId": account_id,
        "region": region,
    }
    if execution_role_arn:
        config_data["executionRoleArn"] = execution_role_arn
    if agentcore_memory_role_arn:
        config_data["agentcore_memory_role"] = agentcore_memory_role_arn
    if memory_id:
        config_data["memory_id"] = memory_id
    if agent_memory_arn:
        config_data["agent_memory_arn"] = agent_memory_arn
    if harness_info:
        if harness_info.get("harness_id"):
            config_data["harnessId"] = harness_info["harness_id"]
        if harness_info.get("harness_arn"):
            config_data["HARNESS_ARN"] = harness_info["harness_arn"]
    if s3_bucket_name:
        config_data["s3_bucket"] = s3_bucket_name
        config_data["s3_arn"] = f"arn:aws:s3:::{s3_bucket_name}"
    if cloudfront_info:
        config_data["sharing_url"] = f"https://{cloudfront_info.get('domain', '')}"
    if ui_cloudfront_info:
        config_data["app_url"] = f"https://{ui_cloudfront_info.get('domain', '')}"
        config_data["ui_cloudfront_domain"] = ui_cloudfront_info.get("domain", "")
        config_data["ui_cloudfront_id"] = ui_cloudfront_info.get("id", "")
    if vpc_info:
        config_data["vpc_id"] = vpc_info.get("vpc_id", "")
    if s3_files_info:
        config_data["s3_files_file_system_id"] = s3_files_info.get("file_system_id", "")
        config_data["s3_files_access_point_arn"] = s3_files_info.get(
            "access_point_arn", ""
        )
        config_data["s3_files_mount_path"] = s3_files_info.get(
            "mount_path", s3_files_vpc.SESSION_STORAGE_MOUNT_PATH
        )
        config_data["agent_runtime_vpc_subnets"] = s3_files_info.get("subnets", [])
        config_data["agent_runtime_security_groups"] = s3_files_info.get(
            "security_groups", []
        )
    if ecs_info:
        config_data["ecs_cluster_arn"] = ecs_info.get("cluster_arn", "")
        config_data["ecs_service_name"] = ecs_info.get("service_name", "")
        config_data["ecs_task_definition_arn"] = ecs_info.get(
            "task_definition_arn", ""
        )
    if image_uri:
        config_data["ecr_image_uri"] = image_uri
    if image_build_tag:
        config_data["latest_image_tag"] = image_build_tag
        config_data["build_number"] = image_build_tag
    if knowledge_base_id:
        config_data["knowledge_base_id"] = knowledge_base_id
    if data_source_id:
        config_data["data_source_id"] = data_source_id
    if knowledge_base_role_arn:
        config_data["knowledge_base_role"] = knowledge_base_role_arn
    if s3_vectors_info:
        config_data["vector_bucket_name"] = s3_vectors_info.get("vectorBucketName", "")
        config_data["vector_bucket_arn"] = s3_vectors_info.get("vectorBucketArn", "")
        config_data["vector_index_name"] = s3_vectors_info.get("indexName", "")
        config_data["vector_index_arn"] = s3_vectors_info.get("indexArn", "")
    if knowledge_base_mcp_info:
        if knowledge_base_mcp_info.get("agent_runtime_arn"):
            config_data["knowledge_base_mcp_runtime_arn"] = knowledge_base_mcp_info[
                "agent_runtime_arn"
            ]
        if knowledge_base_mcp_info.get("knowledge_base_mcp_url"):
            config_data["knowledge_base_mcp_url"] = knowledge_base_mcp_info[
                "knowledge_base_mcp_url"
            ]
        if knowledge_base_mcp_info.get("agentcore_gateway_arn"):
            config_data["agentcore_gateway_arn"] = knowledge_base_mcp_info[
                "agentcore_gateway_arn"
            ]
        if knowledge_base_mcp_info.get("agentcore_gateway_id"):
            config_data["agentcore_gateway_id"] = knowledge_base_mcp_info[
                "agentcore_gateway_id"
            ]
        if knowledge_base_mcp_info.get("agentcore_gateway_role"):
            config_data["agentcore_gateway_role"] = knowledge_base_mcp_info[
                "agentcore_gateway_role"
            ]
        if knowledge_base_mcp_info.get("knowledge_base_mcp_gateway_target_id"):
            config_data["knowledge_base_mcp_gateway_target_id"] = (
                knowledge_base_mcp_info["knowledge_base_mcp_gateway_target_id"]
            )
        if knowledge_base_mcp_info.get("agent_runtime_role"):
            config_data["knowledge_base_mcp_role"] = knowledge_base_mcp_info[
                "agent_runtime_role"
            ]
        if knowledge_base_mcp_info.get("ecr_repository"):
            config_data["knowledge_base_mcp_ecr_repository"] = knowledge_base_mcp_info[
                "ecr_repository"
            ]
        if knowledge_base_mcp_info.get("latest_image_tag"):
            config_data["knowledge_base_mcp_image_tag"] = knowledge_base_mcp_info[
                "latest_image_tag"
            ]
    if artifact_share_mcp_info:
        if artifact_share_mcp_info.get("agent_runtime_arn"):
            config_data["artifact_share_mcp_runtime_arn"] = artifact_share_mcp_info[
                "agent_runtime_arn"
            ]
        if artifact_share_mcp_info.get("artifact_share_mcp_url"):
            config_data["artifact_share_mcp_url"] = artifact_share_mcp_info[
                "artifact_share_mcp_url"
            ]
        if artifact_share_mcp_info.get("artifact_share_mcp_gateway_target_id"):
            config_data["artifact_share_mcp_gateway_target_id"] = artifact_share_mcp_info[
                "artifact_share_mcp_gateway_target_id"
            ]
        if artifact_share_mcp_info.get("agent_runtime_role"):
            config_data["artifact_share_mcp_role"] = artifact_share_mcp_info[
                "agent_runtime_role"
            ]
        if artifact_share_mcp_info.get("ecr_repository"):
            config_data["artifact_share_mcp_ecr_repository"] = artifact_share_mcp_info[
                "ecr_repository"
            ]
        if artifact_share_mcp_info.get("latest_image_tag"):
            config_data["artifact_share_mcp_image_tag"] = artifact_share_mcp_info[
                "latest_image_tag"
            ]
        # Prefer gateway fields from either MCP deploy (shared project gateway).
        if artifact_share_mcp_info.get("agentcore_gateway_arn") and not config_data.get(
            "agentcore_gateway_arn"
        ):
            config_data["agentcore_gateway_arn"] = artifact_share_mcp_info[
                "agentcore_gateway_arn"
            ]
        if artifact_share_mcp_info.get("agentcore_gateway_id") and not config_data.get(
            "agentcore_gateway_id"
        ):
            config_data["agentcore_gateway_id"] = artifact_share_mcp_info[
                "agentcore_gateway_id"
            ]
        if artifact_share_mcp_info.get("agentcore_gateway_role") and not config_data.get(
            "agentcore_gateway_role"
        ):
            config_data["agentcore_gateway_role"] = artifact_share_mcp_info[
                "agentcore_gateway_role"
            ]
    if cognito_info:
        config_data["cognito_user_pool_id"] = cognito_info.get(
            "cognito_user_pool_id", ""
        )
        config_data["cognito_user_pool_name"] = cognito_info.get(
            "cognito_user_pool_name", ""
        )
        config_data["cognito_client_id"] = cognito_info.get("cognito_client_id", "")
        config_data["cognito_client_name"] = cognito_info.get("cognito_client_name", "")
        config_data["cognito_admin_username"] = cognito_info.get(
            "cognito_admin_username", ""
        )
        config_data["cognito_region"] = cognito_info.get("cognito_region", region)
    return config_data


def main():
    global project_name, region, account_id

    parser = argparse.ArgumentParser(
        description="AgentCore Harness + ECS Web UI installer"
    )
    parser.add_argument(
        "--skip-docker-build",
        action="store_true",
        help="Skip local Docker build/push and reuse the latest image tag in ECR.",
    )
    parser.add_argument(
        "--skip-ecs",
        action="store_true",
        help="Provision Harness/VPC only; do not deploy ECS Web UI.",
    )
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("Starting AgentCore Harness Infrastructure Deployment")
    logger.info("=" * 60)

    load_config(CONFIG_PATH)

    logger.info(f"Project: {project_name}")
    logger.info(f"Region: {region}")
    logger.info(f"Account ID: {account_id}")
    logger.info(f"Bucket Name: {_bucket_name()}")
    logger.info(f"Vector Bucket: {vector_bucket_name}")
    logger.info(f"Config: {CONFIG_PATH}")
    logger.info("=" * 60)

    # Ask for Cognito admin password up front only when admin does not exist yet.
    existing_cognito_pool_id = _resolve_cognito_user_pool_id(project_name)
    cognito_admin_password: Optional[str] = None
    if existing_cognito_pool_id and _cognito_admin_exists(
        existing_cognito_pool_id, COGNITO_ADMIN_USERNAME
    ):
        logger.info(
            f"  ✓ Cognito admin '{COGNITO_ADMIN_USERNAME}' already exists "
            f"(pool={existing_cognito_pool_id}) — skipping password prompt"
        )
    else:
        cognito_admin_password = prompt_cognito_admin_password()
        logger.info("  ✓ Cognito admin password accepted (will create admin user later)")

    start_time = time.time()
    s3_bucket_name = None
    knowledge_base_role_arn = None
    s3_vectors_info = None
    knowledge_base_id = None
    data_source_id = None
    knowledge_base_mcp_info = None
    artifact_share_mcp_info = None
    execution_role_arn = None
    agentcore_memory_role_arn = None
    memory_id = None
    agent_memory_arn = None
    vpc_info = None
    s3_files_info = None
    harness_info = None
    cloudfront_info = None
    ui_cloudfront_info = None
    ecs_info = None
    image_uri = None
    image_build_tag = None
    app_environment = None
    cognito_info = None
    deployment_success = False

    try:
        get_or_create_session_signing_key()
        cognito_info = create_cognito_user_pool(admin_password=cognito_admin_password)

        s3_bucket_name = create_s3_bucket()
        upload_skills_to_s3(s3_bucket_name)

        knowledge_base_role_arn = create_knowledge_base_role()
        s3_vectors_info = create_s3_vectors_store()
        knowledge_base_id, data_source_id = create_knowledge_base_with_s3_vectors(
            s3_vectors_info, knowledge_base_role_arn, s3_bucket_name
        )

        # Prefer existing sharing_url from a prior install when building MCP image env.
        prior_sharing_url = ""
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                prior_sharing_url = (json.load(f).get("sharing_url") or "").rstrip("/")
        except Exception:
            pass

        knowledge_base_mcp_info = deploy_knowledge_base_mcp(
            knowledge_base_id=knowledge_base_id,
            sharing_url=prior_sharing_url,
        )
        artifact_share_mcp_info = deploy_artifact_share_mcp(
            s3_bucket_name=s3_bucket_name,
            sharing_url=prior_sharing_url,
            gateway_info=knowledge_base_mcp_info,
        )

        execution_role_arn = create_harness_execution_role(
            knowledge_base_mcp_runtime_arn=knowledge_base_mcp_info.get(
                "agent_runtime_arn"
            ),
            artifact_share_mcp_runtime_arn=artifact_share_mcp_info.get(
                "agent_runtime_arn"
            ),
            agentcore_gateway_arn=knowledge_base_mcp_info.get(
                "agentcore_gateway_arn"
            )
            or artifact_share_mcp_info.get("agentcore_gateway_arn"),
        )
        execution_role_name = f"role-harness-for-{project_name}-{region}"
        agentcore_memory_role_arn = create_agentcore_memory_role()
        memory_id = create_agentcore_memory(agentcore_memory_role_arn)
        agent_memory_arn = _memory_arn_from_id(memory_id)

        provisioner = _s3_files_provisioner()
        logger.info("[6/12] Ensuring VPC for Harness (S3 Files requires VPC mode)")
        vpc_info = provisioner.ensure_vpc()
        logger.info("[7/12] Creating S3 Files session storage")
        s3_files_info = provisioner.create_s3_files_session_storage(
            vpc_info,
            s3_bucket_name,
            execution_role_arn,
            execution_role_name,
        )

        harness_info = create_or_get_harness(
            execution_role_arn,
            agent_memory_arn,
            s3_files_info=s3_files_info,
            agentcore_gateway_arn=knowledge_base_mcp_info.get(
                "agentcore_gateway_arn"
            ),
        )
        logger.info("[9/12] Creating project S3 CloudFront (file sharing)")
        cloudfront_info = create_cloudfront_distribution(s3_bucket_name)
        sharing_url = f"https://{cloudfront_info.get('domain', '')}".rstrip("/")
        ensure_harness_sharing_env(
            harness_info["harness_id"],
            s3_bucket_name,
            sharing_url,
            knowledge_base_id=knowledge_base_id,
            data_source_id=data_source_id,
        )
        if sharing_url and sharing_url != prior_sharing_url:
            if knowledge_base_mcp_info:
                logger.info("Refreshing Knowledge Base MCP SHARING_URL")
                refresh_knowledge_base_mcp_env(
                    knowledge_base_mcp_info,
                    knowledge_base_id=knowledge_base_id,
                    sharing_url=sharing_url,
                )
            if artifact_share_mcp_info:
                logger.info("Refreshing Artifact Share MCP SHARING_URL")
                refresh_artifact_share_mcp_env(
                    artifact_share_mcp_info,
                    s3_bucket_name=s3_bucket_name,
                    sharing_url=sharing_url,
                )

        if args.skip_ecs:
            logger.warning("Skipping ECS Web UI deployment (--skip-ecs)")
            deployment_success = True
        else:
            deployer = EcsWebDeployer(
                project_name=project_name,
                region=region,
                account_id=account_id,
                logger=logger,
                bucket_name=s3_bucket_name,
            )
            logger.info("[10/12] Creating ECS roles / ALB / UI CloudFront")
            ecs_roles = deployer.create_ecs_roles()
            vpc_info = deployer.ensure_web_security_groups(vpc_info)
            deployer.prepare_s3files_for_ecs(
                vpc_info,
                s3_files_info,
                ecs_roles["task_role_arn"],
                ecs_roles["task_role_name"],
                execution_role_arn,
            )
            origin_header_value = deployer.get_or_create_alb_origin_header()
            alb_info = deployer.create_alb(vpc_info)
            ui_cloudfront_info = deployer.create_ui_cloudfront(
                alb_info, origin_header_value
            )

            app_environment = build_config_from_deployment_state(
                execution_role_arn=execution_role_arn,
                agentcore_memory_role_arn=agentcore_memory_role_arn,
                memory_id=memory_id,
                agent_memory_arn=agent_memory_arn,
                harness_info=harness_info,
                s3_bucket_name=s3_bucket_name,
                cloudfront_info=cloudfront_info,
                ui_cloudfront_info=ui_cloudfront_info,
                s3_files_info=s3_files_info,
                vpc_info=vpc_info,
                knowledge_base_id=knowledge_base_id,
                data_source_id=data_source_id,
                knowledge_base_role_arn=knowledge_base_role_arn,
                s3_vectors_info=s3_vectors_info,
                knowledge_base_mcp_info=knowledge_base_mcp_info,
                artifact_share_mcp_info=artifact_share_mcp_info,
                cognito_info=cognito_info,
            )
            if write_config(CONFIG_PATH, app_environment):
                logger.info(
                    "Local testing is available while ECS deploy continues:"
                )
                logger.info("  ./run_local.sh")

            logger.info("[11/12] Building Docker image and deploying ECS")
            repository_uri = deployer.create_ecr_repository()
            if args.skip_docker_build:
                image_uri = deployer.resolve_ecr_image_uri(repository_uri)
                image_build_tag = image_uri.rsplit(":", 1)[-1]
                logger.warning(f"Skipping Docker build; using: {image_uri}")
            else:
                image_uri, image_build_tag = deployer.build_and_push_docker_image(
                    repository_uri
                )
            app_environment["latest_image_tag"] = image_build_tag
            app_environment["build_number"] = image_build_tag
            app_environment["ecr_image_uri"] = image_uri
            write_config(CONFIG_PATH, app_environment)

            log_group_name = deployer.create_ecs_log_group()
            ecs_info = deployer.deploy_ecs_service(
                vpc_info,
                alb_info,
                ecs_roles,
                image_uri,
                app_environment,
                log_group_name,
                s3_files_info=s3_files_info,
                origin_header_value=origin_header_value,
            )
            logger.info("[12/12] Waiting for CloudFront / ECS readiness")
            deployer.check_application_ready(ui_cloudfront_info["domain"])
            deployment_success = True

        elapsed_time = time.time() - start_time
        logger.info("")
        logger.info("=" * 60)
        logger.info("Infrastructure Deployment Completed Successfully!")
        logger.info("=" * 60)
        logger.info(f"  S3 Bucket: {s3_bucket_name}")
        logger.info(f"  Sharing CloudFront: https://{cloudfront_info['domain']}")
        if ui_cloudfront_info:
            logger.info(f"  Web UI CloudFront: https://{ui_cloudfront_info['domain']}")
        logger.info(f"  Knowledge Base ID: {knowledge_base_id}")
        logger.info(f"  Data Source ID: {data_source_id}")
        logger.info(f"  Knowledge Base Role: {knowledge_base_role_arn}")
        logger.info(
            f"  Knowledge Base MCP Runtime: "
            f"{(knowledge_base_mcp_info or {}).get('agent_runtime_arn')}"
        )
        logger.info(
            f"  Artifact Share MCP Runtime: "
            f"{(artifact_share_mcp_info or {}).get('agent_runtime_arn')}"
        )
        logger.info(
            f"  AgentCore Gateway: "
            f"{(knowledge_base_mcp_info or {}).get('agentcore_gateway_arn')}"
        )
        logger.info(f"  Vector Bucket: {s3_vectors_info.get('vectorBucketName')}")
        logger.info(f"  Vector Index: {s3_vectors_info.get('indexName')}")
        logger.info(f"  VPC: {vpc_info.get('vpc_id')}")
        logger.info(f"  S3 Files Access Point: {s3_files_info.get('access_point_arn')}")
        logger.info(
            f"  S3 Files Mount: {s3_files_info.get('mount_path')} "
            f"(networkMode=VPC)"
        )
        logger.info(f"  Execution Role: {execution_role_arn}")
        logger.info(f"  AgentCore Memory Role: {agentcore_memory_role_arn}")
        logger.info(f"  Memory ID: {memory_id}")
        logger.info(f"  Memory ARN: {agent_memory_arn}")
        logger.info(f"  Harness ID: {harness_info['harness_id']}")
        logger.info(f"  Harness ARN: {harness_info['harness_arn']}")
        if cognito_info:
            logger.info(
                f"  Cognito User Pool: {cognito_info.get('cognito_user_pool_id')} "
                f"({cognito_info.get('cognito_user_pool_name')})"
            )
            logger.info(f"  Cognito Client ID: {cognito_info.get('cognito_client_id')}")
            logger.info(
                f"  Cognito Admin: {cognito_info.get('cognito_admin_username')}"
            )
        if ecs_info:
            logger.info(
                f"  ECS Service: {ecs_info.get('service_name')} "
                "(Fargate in private subnet)"
            )
        if image_uri:
            logger.info(f"  ECR Image: {image_uri}")
        logger.info(f"Total deployment time: {elapsed_time / 60:.2f} minutes")
        logger.info("=" * 60)
        if ui_cloudfront_info:
            logger.info("")
            logger.info("  IMPORTANT: Web UI URL")
            logger.info(f"  https://{ui_cloudfront_info['domain']}")
            logger.info(
                "  Note: CloudFront/ECS may take 10–20 minutes to fully propagate"
            )
            logger.info(
                f"  Login with Cognito username '{COGNITO_ADMIN_USERNAME}' "
                "(or a user from add_user.py)"
            )
            logger.info("=" * 60)
    except Exception as e:
        elapsed_time = time.time() - start_time
        logger.error(f"Deployment Failed after {elapsed_time / 60:.2f} minutes: {e}")
        import traceback

        logger.error(traceback.format_exc())
        raise
    finally:
        config_data = build_config_from_deployment_state(
            execution_role_arn=execution_role_arn,
            agentcore_memory_role_arn=agentcore_memory_role_arn,
            memory_id=memory_id,
            agent_memory_arn=agent_memory_arn,
            harness_info=harness_info,
            s3_bucket_name=s3_bucket_name,
            cloudfront_info=cloudfront_info,
            ui_cloudfront_info=ui_cloudfront_info,
            s3_files_info=s3_files_info,
            vpc_info=vpc_info,
            ecs_info=ecs_info,
            image_uri=image_uri,
            image_build_tag=image_build_tag,
            knowledge_base_id=knowledge_base_id,
            data_source_id=data_source_id,
            knowledge_base_role_arn=knowledge_base_role_arn,
            s3_vectors_info=s3_vectors_info,
            knowledge_base_mcp_info=knowledge_base_mcp_info,
            artifact_share_mcp_info=artifact_share_mcp_info,
            cognito_info=cognito_info,
        )
        if app_environment is not None:
            config_data = {**app_environment, **config_data}
        if write_config(CONFIG_PATH, config_data):
            if deployment_success:
                logger.info(f"Updated {CONFIG_PATH}")
            else:
                logger.info(f"Saved partial deployment info to {CONFIG_PATH}")


if __name__ == "__main__":
    main()
