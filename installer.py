#!/usr/bin/env python3
"""
AWS Infrastructure Installer using boto3
This script provisions AgentCore Harness (S3, skills, VPC, S3 Files mount,
CloudFront, IAM roles, Memory, CreateHarness) and deploys the React+FastAPI
Web UI to Amazon ECS Fargate (ALB + CloudFront), similar to strands-work.
"""

import argparse
import boto3
import json
import time
import logging
import os
import re
import sys
import mimetypes
import uuid
from typing import Dict, List, Optional
from botocore.exceptions import ClientError
from bedrock_agentcore.memory import MemoryClient

import s3_files_vpc
from ecs_web import EcsWebDeployer

# Configuration
project_name = "harness-work"  # at least 3 characters
region = "us-west-2"
DEFAULT_MODEL_ID = "global.anthropic.claude-opus-4-7"

# CreateHarness harnessName: Pattern [a-zA-Z][a-zA-Z0-9_]{0,39} — no hyphens.
_HARNESS_NAME_API_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,39}$")

sts_client = boto3.client("sts", region_name=region)
account_id = str(sts_client.get_caller_identity()["Account"])

iam_client = boto3.client("iam", region_name=region)
s3_client = boto3.client("s3", region_name=region)
ec2_client = boto3.client("ec2", region_name=region)
s3files_client = boto3.client("s3files", region_name=region)
cloudfront_client = boto3.client("cloudfront", region_name=region)
agentcore_control_client = boto3.client(
    "bedrock-agentcore-control",
    region_name=region,
)

WORKING_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(WORKING_DIR, "application", "config.json")
SKILLS_DIR = os.path.join(WORKING_DIR, "skills")
SKILLS_S3_PREFIX = "skills"


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
    global agentcore_control_client, s3_client, cloudfront_client
    global ec2_client, s3files_client

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

    agentcore_control_client = boto3.client(
        "bedrock-agentcore-control",
        region_name=region,
    )
    s3_client = boto3.client("s3", region_name=region)
    ec2_client = boto3.client("ec2", region_name=region)
    s3files_client = boto3.client("s3files", region_name=region)
    cloudfront_client = boto3.client("cloudfront", region_name=region)
    return config


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


def upload_skills_to_s3(s3_bucket_name: str) -> int:
    """Upload skills/ to s3://{bucket}/skills/ (AgentCore S3 skill layout)."""
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


def create_harness_execution_role() -> str:
    """Create IAM execution role for Bedrock AgentCore harness."""
    logger.info("[3/9] Creating Harness execution IAM role")
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
            # s3-sharing skill: put_object for CloudFront download URLs
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
    "생성된 artifact(PDF, DOCX, PNG, CSV 등)를 사용자에게 공유하거나 다운로드 링크를 제공할 때는 "
    "반드시 s3-sharing SKILL을 사용하세요. "
    "로컬 경로만 알려주지 말고, 해당 스킬로 S3에 업로드한 뒤 CloudFront 공유 URL을 전달하세요.\n"
    "\n"
    "An agent orchestrates the following workflow:\n"
    "1. Receives user input\n"
    "2. Processes the input using a language model\n"
    "3. Decides whether to use tools to gather information or perform actions\n"
    "4. Executes those tools and receives results\n"
    "5. Continues reasoning with the new information\n"
    "6. Produces a final response\n"
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
    agentcore_control_client.update_harness(
        harnessId=harness_id,
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
    agentcore_control_client.update_harness(
        harnessId=harness_id,
        environment=desired,
        environmentVariables=_harness_environment_variables(),
    )


def _harness_environment_variables(
    *,
    s3_bucket_name: str | None = None,
    sharing_url: str | None = None,
) -> Dict[str, str]:
    """Env vars for Harness runtime (session storage + s3-sharing skill)."""
    env: Dict[str, str] = {
        "LOG_LEVEL": "info",
        "SESSION_STORAGE_DIR": s3_files_vpc.SESSION_STORAGE_MOUNT_PATH,
    }
    bucket = s3_bucket_name or _bucket_name()
    if bucket:
        env["S3_BUCKET"] = bucket
    if sharing_url:
        env["SHARING_URL"] = sharing_url.rstrip("/")
    return env


def ensure_harness_sharing_env(
    harness_id: str,
    s3_bucket_name: str,
    sharing_url: str,
) -> None:
    """Inject S3_BUCKET / SHARING_URL for the s3-sharing skill."""
    if not harness_id or not s3_bucket_name:
        return
    url = (sharing_url or "").rstrip("/")
    if not url:
        logger.warning("  SHARING_URL empty; s3-sharing will fall back to console URLs")
    env_vars = _harness_environment_variables(
        s3_bucket_name=s3_bucket_name,
        sharing_url=url or None,
    )
    logger.info(
        f"  Updating harness env for s3-sharing: "
        f"S3_BUCKET={s3_bucket_name}, SHARING_URL={url or '(none)'}"
    )
    agentcore_control_client.update_harness(
        harnessId=harness_id,
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
    agentcore_control_client.update_harness(
        harnessId=harness_id,
        systemPrompt=desired,
    )


def create_or_get_harness(
    execution_role_arn: str,
    agent_memory_arn: str,
    s3_files_info: Optional[Dict[str, object]] = None,
) -> Dict[str, str]:
    """Create AgentCore Harness or reuse an existing one by API name."""
    logger.info("[8/9] Creating AgentCore Harness")

    harness_api_name = harness_name_for_api(project_name)
    logger.info(f"  harnessName: {harness_api_name} (from projectName={project_name!r})")

    model_id = DEFAULT_MODEL_ID
    system_prompt = [{"text": BASE_SYSTEM_PROMPT}]
    environment = s3_files_vpc.build_harness_runtime_environment(s3_files_info)

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
                tools=[
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
                ],
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
    harness_arn = wait_for_harness_ready(harness_id)

    return {
        "harness_id": harness_id,
        "harness_arn": harness_arn,
        "harness_name": harness_api_name,
    }


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
    logger.info(f"Config: {CONFIG_PATH}")
    logger.info("=" * 60)

    start_time = time.time()
    s3_bucket_name = None
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
    deployment_success = False

    try:
        s3_bucket_name = create_s3_bucket()
        upload_skills_to_s3(s3_bucket_name)
        execution_role_arn = create_harness_execution_role()
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
        )
        logger.info("[9/12] Creating project S3 CloudFront (file sharing)")
        cloudfront_info = create_cloudfront_distribution(s3_bucket_name)
        ensure_harness_sharing_env(
            harness_info["harness_id"],
            s3_bucket_name,
            f"https://{cloudfront_info.get('domain', '')}",
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
