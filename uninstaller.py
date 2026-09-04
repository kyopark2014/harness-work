#!/usr/bin/env python3
"""
AWS Infrastructure Uninstaller for harness-work.

Deletes resources created by installer.py:
  Cognito, session signing key, ECS Web UI, ALB, UI CloudFront, Harness,
  online evaluation, Memory, Knowledge Base, S3 Vectors, S3 Files, VPC/NAT, project S3 /
  S3 CloudFront, IAM roles.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import uuid
from typing import Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

from ecs_web import (
    delete_alb_origin_header_secret,
    delete_alb_resources,
    delete_disabled_ui_cloudfront,
    delete_ecs_iam_roles,
    delete_ecs_resources,
    delete_ui_cloudfront,
)
# Configuration (must match installer.py / s3_files_vpc.py)
project_name = "harness-work"
region = "us-west-2"

# Cognito Web UI auth (must match installer.py)
COGNITO_CLIENT_NAME = f"{project_name}-web-ui"
SESSION_SIGNING_KEY_SECRET_NAME = f"{project_name}/session-signing-key"

WORKING_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(WORKING_DIR, "application", "config.json")

DELETE_WAIT_TIMEOUT_SEC = int(os.environ.get("AGENTCORE_DELETE_WAIT_TIMEOUT_SEC", "600"))
DELETE_POLL_INTERVAL_SEC = float(os.environ.get("AGENTCORE_DELETE_POLL_INTERVAL_SEC", "5"))

sts_client = boto3.client("sts", region_name=region)
account_id = str(sts_client.get_caller_identity()["Account"])

vector_index_name = project_name
vector_bucket_name = f"{project_name}-{account_id}"

s3_client = boto3.client("s3", region_name=region)
iam_client = boto3.client("iam", region_name=region)
ec2_client = boto3.client("ec2", region_name=region)
s3files_client = boto3.client("s3files", region_name=region)
s3vectors_client = boto3.client("s3vectors", region_name=region)
cloudfront_client = boto3.client("cloudfront", region_name=region)
cognito_idp_client = boto3.client("cognito-idp", region_name=region)
secretsmanager_client = boto3.client("secretsmanager", region_name=region)
bedrock_agent_client = boto3.client("bedrock-agent", region_name=region)
agentcore_control_client = boto3.client(
    "bedrock-agentcore-control",
    region_name=region,
)


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    return logging.getLogger(__name__)


logger = setup_logging()


def _bucket_name() -> str:
    return f"storage-for-{project_name}-{account_id}-{region}"


def _cloudfront_comment() -> str:
    # S3 sharing CF (installer); UI CF is CloudFront-for-{project} in ecs_web.
    return f"CloudFront-S3-for-{project_name}"


def _oai_comment() -> str:
    return f"OAI for {project_name}"


def _vpc_name() -> str:
    return f"vpc-for-{project_name}"


def load_config() -> Dict:
    global project_name, region, account_id
    global vector_index_name, vector_bucket_name
    global s3_client, iam_client, ec2_client, s3files_client, s3vectors_client
    global cloudfront_client, bedrock_agent_client, agentcore_control_client

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        logger.warning(f"Could not load {CONFIG_PATH}: {e}")
        cfg = {}

    # Script constant is authoritative — do not let a copied config.json rename the project.
    region = cfg.get("region") or region
    raw = cfg.get("accountId")
    if raw is not None and str(raw).strip():
        account_id = str(raw).strip()
    else:
        account_id = str(sts_client.get_caller_identity()["Account"])
    cfg["projectName"] = project_name

    vector_index_name = project_name
    vector_bucket_name = f"{project_name}-{account_id}"

    s3_client = boto3.client("s3", region_name=region)
    iam_client = boto3.client("iam", region_name=region)
    ec2_client = boto3.client("ec2", region_name=region)
    s3files_client = boto3.client("s3files", region_name=region)
    s3vectors_client = boto3.client("s3vectors", region_name=region)
    cloudfront_client = boto3.client("cloudfront", region_name=region)
    bedrock_agent_client = boto3.client("bedrock-agent", region_name=region)
    agentcore_control_client = boto3.client(
        "bedrock-agentcore-control",
        region_name=region,
    )
    return cfg


def prompt_yes_no(question: str, default: bool = False) -> bool:
    suffix = " [Y/n]: " if default else " [y/N]: "
    try:
        answer = input(question + suffix).strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer in {"y", "yes"}


# --- CloudFront --------------------------------------------------------------

def _matches_cloudfront(dist: dict) -> bool:
    return _cloudfront_comment() in dist.get("Comment", "")


def disable_cloudfront_distributions():
    logger.info("[1/8] Disabling CloudFront distributions")
    try:
        distributions = cloudfront_client.list_distributions()
        for dist in distributions.get("DistributionList", {}).get("Items", []):
            if not _matches_cloudfront(dist):
                continue
            if not dist.get("Enabled", True):
                logger.info(f"  Already disabled: {dist['Id']}")
                continue
            dist_id = dist["Id"]
            logger.info(f"  Disabling: {dist_id}")
            cfg_resp = cloudfront_client.get_distribution_config(Id=dist_id)
            cfg = cfg_resp["DistributionConfig"]
            cfg["Enabled"] = False
            cloudfront_client.update_distribution(
                Id=dist_id,
                DistributionConfig=cfg,
                IfMatch=cfg_resp["ETag"],
            )
        logger.info("✓ CloudFront disable requested")
    except Exception as e:
        logger.error(f"Error disabling CloudFront: {e}")


def wait_for_cloudfront_disabled(max_wait: int = 900, poll_interval: int = 30) -> bool:
    logger.info("  Waiting for CloudFront to become disabled...")
    waited = 0
    while waited < max_wait:
        still = []
        distributions = cloudfront_client.list_distributions()
        for dist in distributions.get("DistributionList", {}).get("Items", []):
            if _matches_cloudfront(dist) and dist.get("Enabled", True):
                still.append(dist["Id"])
        if not still:
            logger.info("  ✓ Matching CloudFront distributions disabled")
            return True
        logger.info(f"  Still enabled: {still} ({waited}s/{max_wait}s)")
        time.sleep(poll_interval)
        waited += poll_interval
    logger.warning("  Timed out waiting for CloudFront disable")
    return False


def delete_cloudfront_distributions():
    logger.info("[7/8] Deleting CloudFront distributions")
    try:
        distributions = cloudfront_client.list_distributions()
        for dist in distributions.get("DistributionList", {}).get("Items", []):
            if not _matches_cloudfront(dist):
                continue
            if dist.get("Enabled", True):
                logger.info(f"  Skipping enabled distribution: {dist['Id']}")
                continue
            dist_id = dist["Id"]
            try:
                cfg_resp = cloudfront_client.get_distribution_config(Id=dist_id)
                cloudfront_client.delete_distribution(
                    Id=dist_id, IfMatch=cfg_resp["ETag"]
                )
                logger.info(f"  ✓ Deleted distribution: {dist_id}")
            except ClientError as e:
                code = e.response["Error"]["Code"]
                if code in {"DistributionNotDisabled", "NoSuchDistribution"}:
                    logger.info(f"  Skip {dist_id}: {code}")
                else:
                    logger.warning(f"  Could not delete {dist_id}: {e}")
        logger.info("✓ CloudFront distributions processed")
    except Exception as e:
        logger.error(f"Error deleting CloudFront: {e}")


def delete_cloudfront_oai():
    logger.info("  Deleting CloudFront Origin Access Identities")
    try:
        oai_list = cloudfront_client.list_cloud_front_origin_access_identities()
        for oai in oai_list.get("CloudFrontOriginAccessIdentityList", {}).get(
            "Items", []
        ):
            if _oai_comment() not in oai.get("Comment", ""):
                continue
            oai_id = oai["Id"]
            try:
                cfg = cloudfront_client.get_cloud_front_origin_access_identity_config(
                    Id=oai_id
                )
                cloudfront_client.delete_cloud_front_origin_access_identity(
                    Id=oai_id, IfMatch=cfg["ETag"]
                )
                logger.info(f"  ✓ Deleted OAI: {oai_id}")
            except ClientError as e:
                if e.response["Error"]["Code"] != "NoSuchCloudFrontOriginAccessIdentity":
                    logger.warning(f"  Could not delete OAI {oai_id}: {e}")
    except Exception as e:
        logger.warning(f"  Error deleting OAI: {e}")


# --- Harness / Memory --------------------------------------------------------

def _paginate_list_harnesses() -> list:
    items = []
    token = None
    while True:
        kw = {"maxResults": 50}
        if token:
            kw["nextToken"] = token
        resp = agentcore_control_client.list_harnesses(**kw)
        items.extend(resp.get("harnesses") or [])
        token = resp.get("nextToken")
        if not token:
            break
    return items


def harness_name_for_api(name: str) -> str:
    """Same as installer: projectName → harnessName ('-' → '_')."""
    return (name or "").replace("-", "_")


def resolve_harness_id(cfg: dict) -> Optional[str]:
    if cfg.get("harnessId"):
        return cfg["harnessId"]
    arn = cfg.get("HARNESS_ARN") or ""
    if "harness/" in arn:
        return arn.split("harness/", 1)[-1].strip()

    # Fallback: match CreateHarness harnessName (installer harness_name_for_api)
    api_name = harness_name_for_api(cfg.get("projectName") or project_name)
    for h in _paginate_list_harnesses():
        if h.get("harnessName") == api_name:
            return h.get("harnessId")
    return None


def resolve_memory_id(cfg: dict) -> Optional[str]:
    if cfg.get("memory_id"):
        return cfg["memory_id"]
    if cfg.get("memoryId"):
        return cfg["memoryId"]
    arn = cfg.get("agent_memory_arn") or ""
    for marker in ("memory/", ":memory/", "/memory/"):
        if marker in arn:
            return arn.split(marker, 1)[-1].strip()
    return None


def delete_harness(harness_id: str) -> bool:
    logger.info(f"[2/8] Deleting Harness: {harness_id}")
    try:
        agentcore_control_client.delete_harness(
            harnessId=harness_id,
            clientToken=str(uuid.uuid4()),
        )
        logger.info(f"  DeleteHarness accepted: {harness_id}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            logger.info(f"  Harness already gone: {harness_id}")
            return True
        logger.error(f"  DeleteHarness failed: {e}")
        return False

    deadline = time.monotonic() + DELETE_WAIT_TIMEOUT_SEC
    while time.monotonic() < deadline:
        try:
            h = agentcore_control_client.get_harness(harnessId=harness_id)["harness"]
            status = h.get("status")
            if status == "DELETE_FAILED":
                logger.error(
                    f"  Harness DELETE_FAILED: {h.get('failureReason')!r}"
                )
                return False
            logger.info(f"  Waiting… status={status!r}")
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceNotFoundException":
                logger.info(f"✓ Harness deleted: {harness_id}")
                return True
            raise
        time.sleep(DELETE_POLL_INTERVAL_SEC)
    logger.error("  Timed out waiting for harness deletion")
    return False


def delete_memory(memory_id: str) -> bool:
    logger.info(f"[5/8] Deleting AgentCore Memory: {memory_id}")
    try:
        agentcore_control_client.delete_memory(
            memoryId=memory_id,
            clientToken=str(uuid.uuid4()),
        )
        logger.info(f"  DeleteMemory accepted: {memory_id}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            logger.info(f"  Memory already gone: {memory_id}")
            return True
        logger.error(f"  DeleteMemory failed: {e}")
        return False

    deadline = time.monotonic() + DELETE_WAIT_TIMEOUT_SEC
    while time.monotonic() < deadline:
        try:
            m = agentcore_control_client.get_memory(memoryId=memory_id)["memory"]
            status = m.get("status")
            if status == "DELETE_FAILED":
                logger.error(
                    f"  Memory DELETE_FAILED: {m.get('failureReason')!r}"
                )
                return False
            logger.info(f"  Waiting… status={status!r}")
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceNotFoundException":
                logger.info(f"✓ Memory deleted: {memory_id}")
                return True
            raise
        time.sleep(DELETE_POLL_INTERVAL_SEC)
    logger.error("  Timed out waiting for memory deletion")
    return False


# --- S3 Files ----------------------------------------------------------------

def _is_s3files_not_found(error: ClientError) -> bool:
    code = error.response["Error"]["Code"]
    return code in {
        "ResourceNotFoundException",
        "FileSystemNotFound",
        "AccessPointNotFound",
        "MountTargetNotFound",
        "NotFound",
        "404",
    }


def _wait_s3files_gone(describe_fn, id_key: str, resource_id: str, timeout: int = 600):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = describe_fn(**{id_key: resource_id})
            status = (resp.get("status") or "").lower()
            if status in {"deleted", "deleting"}:
                time.sleep(5)
                continue
            time.sleep(8)
        except ClientError as e:
            if _is_s3files_not_found(e):
                return
            raise
    raise TimeoutError(f"Timed out waiting for S3 Files {resource_id} deletion")


def _find_s3files_fs_ids(cfg: dict) -> List[str]:
    """Return session + app-data FS ids (config first, then all FS on project bucket)."""
    ids: List[str] = []
    for key in (
        "s3_files_file_system_id",
        "s3_files_app_data_file_system_id",
    ):
        fs_id = (cfg.get(key) or "").strip()
        if fs_id and fs_id not in ids:
            ids.append(fs_id)

    bucket_arn = f"arn:aws:s3:::{_bucket_name()}"
    try:
        paginator = s3files_client.get_paginator("list_file_systems")
        for page in paginator.paginate():
            for item in page.get("fileSystems", []):
                if item.get("bucket") != bucket_arn:
                    continue
                fs_id = item.get("fileSystemId") or ""
                if fs_id and fs_id not in ids:
                    ids.append(fs_id)
    except ClientError as e:
        logger.warning(f"  Could not list S3 Files file systems: {e}")
    return ids


def delete_s3files_sync_role():
    role_name = f"role-s3files-sync-for-{project_name}"
    if len(role_name) > 64:
        role_name = role_name[:64]
    try:
        for pname in iam_client.list_role_policies(RoleName=role_name).get(
            "PolicyNames", []
        ):
            iam_client.delete_role_policy(RoleName=role_name, PolicyName=pname)
        iam_client.delete_role(RoleName=role_name)
        logger.info(f"  ✓ Deleted S3 Files sync role: {role_name}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            logger.warning(f"  Could not delete sync role {role_name}: {e}")


def _delete_one_s3files_filesystem(fs_id: str) -> None:
    logger.info(f"  File system: {fs_id}")

    try:
        s3files_client.delete_file_system_policy(fileSystemId=fs_id)
        logger.info("  ✓ Deleted file system policy")
    except ClientError as e:
        if not _is_s3files_not_found(e):
            logger.warning(f"  Could not delete FS policy: {e}")

    access_point_ids: List[str] = []
    try:
        paginator = s3files_client.get_paginator("list_access_points")
        for page in paginator.paginate(fileSystemId=fs_id):
            for item in page.get("accessPoints", []):
                if item.get("accessPointId"):
                    access_point_ids.append(item["accessPointId"])
    except ClientError as e:
        logger.warning(f"  Could not list access points: {e}")

    for ap_id in access_point_ids:
        try:
            s3files_client.delete_access_point(accessPointId=ap_id)
            _wait_s3files_gone(s3files_client.get_access_point, "accessPointId", ap_id)
            logger.info(f"  ✓ Deleted access point: {ap_id}")
        except ClientError as e:
            if not _is_s3files_not_found(e):
                logger.warning(f"  Could not delete access point {ap_id}: {e}")

    mount_ids: List[str] = []
    try:
        paginator = s3files_client.get_paginator("list_mount_targets")
        for page in paginator.paginate(fileSystemId=fs_id):
            for item in page.get("mountTargets", []):
                if item.get("mountTargetId"):
                    mount_ids.append(item["mountTargetId"])
    except ClientError as e:
        logger.warning(f"  Could not list mount targets: {e}")

    for mt_id in mount_ids:
        try:
            s3files_client.delete_mount_target(mountTargetId=mt_id)
            _wait_s3files_gone(s3files_client.get_mount_target, "mountTargetId", mt_id)
            logger.info(f"  ✓ Deleted mount target: {mt_id}")
        except ClientError as e:
            if not _is_s3files_not_found(e):
                logger.warning(f"  Could not delete mount target {mt_id}: {e}")

    try:
        s3files_client.delete_file_system(fileSystemId=fs_id, forceDelete=True)
        _wait_s3files_gone(s3files_client.get_file_system, "fileSystemId", fs_id)
        logger.info(f"  ✓ Deleted file system: {fs_id}")
    except ClientError as e:
        if not _is_s3files_not_found(e):
            logger.warning(f"  Could not delete file system {fs_id}: {e}")


def delete_s3_files_session_storage(cfg: dict):
    logger.info("[3/8] Deleting S3 Files storage (session + app-data)")
    fs_ids = _find_s3files_fs_ids(cfg)
    if not fs_ids:
        logger.info("  No S3 Files file system found")
        delete_s3files_sync_role()
        return

    for fs_id in fs_ids:
        _delete_one_s3files_filesystem(fs_id)

    delete_s3files_sync_role()
    logger.info("✓ S3 Files storage deleted")


# --- VPC ---------------------------------------------------------------------

def _resolve_vpc_id(cfg: dict) -> Optional[str]:
    if cfg.get("vpc_id"):
        return cfg["vpc_id"]
    resp = ec2_client.describe_vpcs(
        Filters=[{"Name": "tag:Name", "Values": [_vpc_name()]}]
    )
    vpcs = resp.get("Vpcs") or []
    return vpcs[0]["VpcId"] if vpcs else None


def delete_vpc(cfg: dict):
    logger.info("[4/8] Deleting VPC resources")
    vpc_id = _resolve_vpc_id(cfg)
    if not vpc_id:
        logger.info("  No project VPC found")
        return

    logger.info(f"  VPC: {vpc_id}")

    # Detach/delete ENIs that are available
    try:
        enis = ec2_client.describe_network_interfaces(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
        ).get("NetworkInterfaces", [])
        for eni in enis:
            if eni.get("Status") == "available":
                try:
                    ec2_client.delete_network_interface(
                        NetworkInterfaceId=eni["NetworkInterfaceId"]
                    )
                    logger.info(f"  ✓ Deleted ENI: {eni['NetworkInterfaceId']}")
                except ClientError as e:
                    logger.warning(f"  Could not delete ENI: {e}")
    except ClientError as e:
        logger.warning(f"  ENI cleanup: {e}")

    # NAT gateways + routes — capture EIP allocation IDs before delete
    nat_ids = []
    eip_alloc_ids = set()
    try:
        nats = ec2_client.describe_nat_gateways(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
        ).get("NatGateways", [])
        for nat in nats:
            for addr in nat.get("NatGatewayAddresses", []):
                if addr.get("AllocationId"):
                    eip_alloc_ids.add(addr["AllocationId"])
            if nat["State"] in {"deleted", "deleting"}:
                continue
            nat_id = nat["NatGatewayId"]
            rts = ec2_client.describe_route_tables(
                Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
            ).get("RouteTables", [])
            for rt in rts:
                for route in rt.get("Routes", []):
                    if route.get("NatGatewayId") == nat_id:
                        try:
                            ec2_client.delete_route(
                                RouteTableId=rt["RouteTableId"],
                                DestinationCidrBlock=route["DestinationCidrBlock"],
                            )
                        except ClientError:
                            pass
            try:
                ec2_client.delete_nat_gateway(NatGatewayId=nat_id)
                nat_ids.append(nat_id)
                logger.info(f"  ✓ Delete NAT requested: {nat_id}")
            except ClientError as e:
                logger.warning(f"  Could not delete NAT {nat_id}: {e}")
    except ClientError as e:
        logger.warning(f"  NAT cleanup: {e}")

    if nat_ids:
        logger.info("  Waiting for NAT gateways to delete...")
        deadline = time.time() + 600
        while time.time() < deadline:
            remaining = []
            for nid in nat_ids:
                st = ec2_client.describe_nat_gateways(NatGatewayIds=[nid])[
                    "NatGateways"
                ][0]["State"]
                if st != "deleted":
                    remaining.append(nid)
            if not remaining:
                break
            time.sleep(15)

    for alloc_id in eip_alloc_ids:
        try:
            ec2_client.release_address(AllocationId=alloc_id)
            logger.info(f"  ✓ Released EIP: {alloc_id}")
        except ClientError as e:
            logger.debug(f"  EIP release {alloc_id}: {e}")

    # Detach/delete IGWs
    try:
        igws = ec2_client.describe_internet_gateways(
            Filters=[{"Name": "attachment.vpc-id", "Values": [vpc_id]}]
        ).get("InternetGateways", [])
        for igw in igws:
            igw_id = igw["InternetGatewayId"]
            try:
                ec2_client.detach_internet_gateway(
                    InternetGatewayId=igw_id, VpcId=vpc_id
                )
            except ClientError:
                pass
            try:
                ec2_client.delete_internet_gateway(InternetGatewayId=igw_id)
                logger.info(f"  ✓ Deleted IGW: {igw_id}")
            except ClientError as e:
                logger.warning(f"  Could not delete IGW {igw_id}: {e}")
    except ClientError as e:
        logger.warning(f"  IGW cleanup: {e}")

    # Subnets
    try:
        subnets = ec2_client.describe_subnets(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
        ).get("Subnets", [])
        for sn in subnets:
            try:
                ec2_client.delete_subnet(SubnetId=sn["SubnetId"])
                logger.info(f"  ✓ Deleted subnet: {sn['SubnetId']}")
            except ClientError as e:
                logger.warning(f"  Could not delete subnet {sn['SubnetId']}: {e}")
    except ClientError as e:
        logger.warning(f"  Subnet cleanup: {e}")

    # Route tables (non-main)
    try:
        rts = ec2_client.describe_route_tables(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
        ).get("RouteTables", [])
        for rt in rts:
            is_main = any(a.get("Main") for a in rt.get("Associations", []))
            if is_main:
                continue
            for assoc in rt.get("Associations", []):
                if assoc.get("RouteTableAssociationId") and not assoc.get("Main"):
                    try:
                        ec2_client.disassociate_route_table(
                            AssociationId=assoc["RouteTableAssociationId"]
                        )
                    except ClientError:
                        pass
            try:
                ec2_client.delete_route_table(RouteTableId=rt["RouteTableId"])
                logger.info(f"  ✓ Deleted route table: {rt['RouteTableId']}")
            except ClientError as e:
                logger.warning(f"  Could not delete RT {rt['RouteTableId']}: {e}")
    except ClientError as e:
        logger.warning(f"  Route table cleanup: {e}")

    # Security groups (non-default) — revoke cross refs then delete
    try:
        sgs = ec2_client.describe_security_groups(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
        ).get("SecurityGroups", [])
        for sg in sgs:
            if sg.get("GroupName") == "default":
                continue
            try:
                if sg.get("IpPermissions"):
                    ec2_client.revoke_security_group_ingress(
                        GroupId=sg["GroupId"], IpPermissions=sg["IpPermissions"]
                    )
                if sg.get("IpPermissionsEgress"):
                    ec2_client.revoke_security_group_egress(
                        GroupId=sg["GroupId"],
                        IpPermissions=sg["IpPermissionsEgress"],
                    )
            except ClientError:
                pass
        for sg in sgs:
            if sg.get("GroupName") == "default":
                continue
            try:
                ec2_client.delete_security_group(GroupId=sg["GroupId"])
                logger.info(f"  ✓ Deleted SG: {sg['GroupId']} ({sg.get('GroupName')})")
            except ClientError as e:
                logger.warning(f"  Could not delete SG {sg['GroupId']}: {e}")
    except ClientError as e:
        logger.warning(f"  SG cleanup: {e}")

    try:
        ec2_client.delete_vpc(VpcId=vpc_id)
        logger.info(f"✓ Deleted VPC: {vpc_id}")
    except ClientError as e:
        logger.warning(f"  Could not delete VPC {vpc_id}: {e}")


# --- S3 / IAM / config -------------------------------------------------------

def _empty_s3_bucket(bucket: str):
    delete_keys = []
    try:
        paginator = s3_client.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=bucket):
            for obj in page.get("Versions", []):
                delete_keys.append({"Key": obj["Key"], "VersionId": obj["VersionId"]})
            for obj in page.get("DeleteMarkers", []):
                delete_keys.append({"Key": obj["Key"], "VersionId": obj["VersionId"]})
    except ClientError:
        paginator = s3_client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket):
            for obj in page.get("Contents", []):
                delete_keys.append({"Key": obj["Key"]})

    if not delete_keys:
        return
    for i in range(0, len(delete_keys), 1000):
        batch = delete_keys[i : i + 1000]
        s3_client.delete_objects(Bucket=bucket, Delete={"Objects": batch})
    logger.info(f"  ✓ Emptied {len(delete_keys)} object(s) from {bucket}")


def delete_s3_bucket():
    logger.info("[6/8] Deleting S3 bucket")
    bucket = _bucket_name()
    try:
        s3_client.head_bucket(Bucket=bucket)
    except ClientError:
        logger.info(f"  Bucket not found: {bucket}")
        return
    try:
        try:
            s3_client.delete_bucket_policy(Bucket=bucket)
        except ClientError:
            pass
        _empty_s3_bucket(bucket)
        s3_client.delete_bucket(Bucket=bucket)
        logger.info(f"✓ Deleted S3 bucket: {bucket}")
    except ClientError as e:
        logger.warning(f"  Could not delete bucket {bucket}: {e}")


def delete_iam_role(role_name: str):
    try:
        for p in iam_client.list_attached_role_policies(RoleName=role_name).get(
            "AttachedPolicies", []
        ):
            iam_client.detach_role_policy(
                RoleName=role_name, PolicyArn=p["PolicyArn"]
            )
        for pname in iam_client.list_role_policies(RoleName=role_name).get(
            "PolicyNames", []
        ):
            iam_client.delete_role_policy(RoleName=role_name, PolicyName=pname)
        iam_client.delete_role(RoleName=role_name)
        logger.info(f"  ✓ Deleted IAM role: {role_name}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchEntity":
            logger.info(f"  IAM role not found: {role_name}")
        else:
            logger.warning(f"  Could not delete role {role_name}: {e}")


def delete_iam_roles():
    logger.info("  Deleting IAM roles")
    harness_role = f"role-harness-for-{project_name}-{region}"
    memory_role = f"role-agentcore-memory-for-{project_name}-{region}"
    kb_role = f"role-knowledge-base-for-{project_name}-{region}"
    kb_mcp_role = f"role-kb-mcp-for-{project_name}-{region}"
    s3_mcp_role = f"role-artifact-share-mcp-for-{project_name}-{region}"
    gateway_role = f"role-agentcore-gateway-for-{project_name}-{region}"
    delete_iam_role(harness_role)
    delete_iam_role(memory_role)
    delete_iam_role(kb_role)
    delete_iam_role(kb_mcp_role)
    delete_iam_role(s3_mcp_role)
    delete_iam_role(f"role-s3-mcp-for-{project_name}-{region}")  # legacy
    delete_iam_role(gateway_role)
    # legacy KB-only gateway role name
    delete_iam_role(f"role-kb-mcp-gw-for-{project_name}-{region}")
    delete_s3files_sync_role()
    logger.info("✓ IAM roles processed")


def _guardrail_name(project: str) -> str:
    return f"guardrail-for-{project.replace('_', '-').lower()}"


def _find_guardrail_by_name(bedrock_client, name: str) -> Optional[dict]:
    next_token = None
    while True:
        kwargs: Dict = {"maxResults": 100}
        if next_token:
            kwargs["nextToken"] = next_token
        response = bedrock_client.list_guardrails(**kwargs)
        for guardrail in response.get("guardrails", []):
            if guardrail.get("name") == name:
                return guardrail
        next_token = response.get("nextToken")
        if not next_token:
            break
    return None


def _online_evaluation_config_name(project_name: str) -> str:
    return f"{project_name.replace('-', '_')}_harness_online_eval"


def _evaluation_role_name(project_name: str) -> str:
    return f"AmazonBedrockAgentCoreEvaluationRoleFor{project_name}"


def _find_online_evaluation_config(client, config_name: str) -> Optional[dict]:
    next_token = None
    while True:
        params: Dict = {}
        if next_token:
            params["nextToken"] = next_token
        response = client.list_online_evaluation_configs(**params)
        for item in response.get("onlineEvaluationConfigs", []):
            if item.get("onlineEvaluationConfigName") == config_name:
                return item
        next_token = response.get("nextToken")
        if not next_token:
            return None


def delete_online_evaluation(cfg: Dict) -> None:
    """Delete online evaluation config and its evaluation IAM role/policy."""
    logger.info("Deleting AgentCore online evaluation")
    project = cfg.get("projectName") or project_name
    config_name = (
        cfg.get("online_evaluation_config_name")
        or _online_evaluation_config_name(project)
    )
    config_id = cfg.get("online_evaluation_config_id")
    account = cfg.get("accountId") or account_id

    client = boto3.client("bedrock-agentcore-control", region_name=region)
    if not config_id:
        existing = _find_online_evaluation_config(client, config_name)
        if existing:
            config_id = existing.get("onlineEvaluationConfigId")

    if config_id:
        try:
            client.delete_online_evaluation_config(onlineEvaluationConfigId=config_id)
            logger.info(f"  ✓ Deleted online evaluation config: {config_name} ({config_id})")
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("ResourceNotFoundException", "ResourceNotFound"):
                logger.info(
                    f"  Online evaluation config not found (already deleted): {config_name}"
                )
            else:
                logger.warning(f"  Failed to delete online evaluation config: {e}")
    else:
        logger.info(f"  Online evaluation config not found (already deleted): {config_name}")

    role_name = _evaluation_role_name(project)
    policy_name = f"{role_name}Policy"
    iam_client = boto3.client("iam")
    try:
        attached = iam_client.list_attached_role_policies(RoleName=role_name)
        for policy in attached.get("AttachedPolicies", []):
            try:
                iam_client.detach_role_policy(
                    RoleName=role_name,
                    PolicyArn=policy["PolicyArn"],
                )
                logger.info(f"  ✓ Detached policy from evaluation role: {policy['PolicyArn']}")
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code", "")
                if code not in ("NoSuchEntity", "NoSuchEntityException"):
                    logger.warning(f"  Failed to detach {policy['PolicyArn']}: {e}")

        inline = iam_client.list_role_policies(RoleName=role_name)
        for inline_name in inline.get("PolicyNames", []):
            try:
                iam_client.delete_role_policy(RoleName=role_name, PolicyName=inline_name)
                logger.info(f"  ✓ Deleted inline policy: {inline_name}")
            except Exception as e:
                logger.warning(f"  Failed to delete inline policy {inline_name}: {e}")

        iam_client.delete_role(RoleName=role_name)
        logger.info(f"  ✓ Deleted evaluation IAM role: {role_name}")
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchEntity", "NoSuchEntityException"):
            logger.info(f"  Evaluation IAM role not found (already deleted): {role_name}")
        else:
            logger.warning(f"  Failed to delete evaluation IAM role: {e}")

    if account:
        policy_arn = f"arn:aws:iam::{account}:policy/{policy_name}"
        try:
            versions = iam_client.list_policy_versions(PolicyArn=policy_arn)["Versions"]
            for version in versions:
                if not version["IsDefaultVersion"]:
                    iam_client.delete_policy_version(
                        PolicyArn=policy_arn,
                        VersionId=version["VersionId"],
                    )
            iam_client.delete_policy(PolicyArn=policy_arn)
            logger.info(f"  ✓ Deleted IAM policy: {policy_name}")
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchEntity", "NoSuchEntityException"):
                logger.info(f"  IAM policy not found (already deleted): {policy_name}")
            else:
                logger.warning(f"  Failed to delete IAM policy {policy_name}: {e}")


def delete_bedrock_guardrail(cfg: Dict) -> None:
    """Delete Bedrock Guardrail created by the harness installer."""
    logger.info("Deleting Bedrock Guardrail")
    name = cfg.get("guardrail_name") or _guardrail_name(project_name)
    guardrail_id = cfg.get("guardrail_id")
    bedrock_client = boto3.client("bedrock", region_name=region)
    try:
        if not guardrail_id:
            existing = _find_guardrail_by_name(bedrock_client, name)
            if existing:
                guardrail_id = existing.get("id")
        if not guardrail_id:
            logger.info(f"  Guardrail not found (may already be deleted): {name}")
            return
        bedrock_client.delete_guardrail(guardrailIdentifier=guardrail_id)
        logger.info(f"  ✓ Deleted Bedrock Guardrail: {name} ({guardrail_id})")
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("ResourceNotFoundException", "ResourceNotFound"):
            logger.info(f"  Guardrail not found (already deleted): {name}")
        else:
            logger.warning(f"  Failed to delete guardrail: {e}")
    except Exception as e:
        logger.warning(f"  Failed to delete guardrail: {e}")


def delete_cloudwatch_monitoring_dashboard(cfg: Dict) -> None:
    """Delete project CloudWatch monitoring dashboard (keeps shared Bedrock usage dash)."""
    logger.info("Deleting CloudWatch monitoring dashboard")
    name = cfg.get("cloudwatch_dashboard_name")
    if not name:
        name = f"{project_name.replace(' ', '-')}-monitoring"
    try:
        cw = boto3.client("cloudwatch", region_name=region)
        cw.delete_dashboards(DashboardNames=[name])
        logger.info(f"  ✓ Deleted CloudWatch dashboard: {name}")
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("ResourceNotFound", "ResourceNotFoundException"):
            logger.info(f"  Dashboard not found (already deleted): {name}")
        else:
            logger.warning(f"  Failed to delete dashboard {name}: {e}")
    except Exception as e:
        logger.warning(f"  Failed to delete dashboard {name}: {e}")


def _kb_mcp_runtime_name() -> str:
    return f"knowledge_base_of_{project_name}".replace("-", "_")


def _agentcore_gateway_name() -> str:
    return project_name[:48]


def delete_agentcore_gateway(cfg: Dict):
    """Delete the shared project AgentCore Gateway and its targets."""
    logger.info("  Deleting project AgentCore Gateway")
    gateway_name = _agentcore_gateway_name()
    gateway_id = (
        cfg.get("agentcore_gateway_id")
        or cfg.get("knowledge_base_mcp_gateway_id")
        or ""
    )

    try:
        if not gateway_id:
            next_token = None
            while True:
                kwargs = {}
                if next_token:
                    kwargs["nextToken"] = next_token
                resp = agentcore_control_client.list_gateways(**kwargs)
                for item in resp.get("items") or []:
                    if item.get("name") == gateway_name:
                        gateway_id = item["gatewayId"]
                        break
                if gateway_id:
                    break
                next_token = resp.get("nextToken")
                if not next_token:
                    break

        if not gateway_id:
            logger.info(f"  No AgentCore Gateway named {gateway_name}")
            return

        try:
            targets = agentcore_control_client.list_gateway_targets(
                gatewayIdentifier=gateway_id
            ).get("items") or []
            for target in targets:
                tid = target.get("targetId")
                if not tid:
                    continue
                try:
                    agentcore_control_client.delete_gateway_target(
                        gatewayIdentifier=gateway_id,
                        targetId=tid,
                    )
                    logger.info(f"  ✓ Deleted gateway target: {tid}")
                except ClientError as e:
                    logger.warning(f"  Could not delete gateway target {tid}: {e}")
        except ClientError as e:
            logger.warning(f"  Could not list/delete gateway targets: {e}")

        agentcore_control_client.delete_gateway(gatewayIdentifier=gateway_id)
        logger.info(f"  ✓ Deleted AgentCore Gateway: {gateway_id}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            logger.info(f"  Gateway already gone: {gateway_id or gateway_name}")
        else:
            logger.warning(f"  Could not delete AgentCore Gateway: {e}")
    except Exception as e:
        logger.warning(f"  Could not delete AgentCore Gateway: {e}")


def delete_knowledge_base_mcp_runtime(cfg: Dict):
    """Delete Knowledge Base MCP AgentCore Runtime and its ECR repository."""
    logger.info("  Deleting Knowledge Base MCP Runtime")
    runtime_name = _kb_mcp_runtime_name()
    arn = cfg.get("knowledge_base_mcp_runtime_arn") or ""

    try:
        next_token = None
        runtime_id = None
        while True:
            kwargs = {}
            if next_token:
                kwargs["nextToken"] = next_token
            response = agentcore_control_client.list_agent_runtimes(**kwargs)
            for item in response.get("agentRuntimes", []):
                if item.get("agentRuntimeName") == runtime_name or (
                    arn and item.get("agentRuntimeArn") == arn
                ):
                    runtime_id = item.get("agentRuntimeId")
                    break
            if runtime_id:
                break
            next_token = response.get("nextToken")
            if not next_token:
                break

        if runtime_id:
            logger.info(f"  Deleting agent runtime: {runtime_name} ({runtime_id})")
            try:
                agentcore_control_client.delete_agent_runtime(agentRuntimeId=runtime_id)
            except ClientError as e:
                if e.response["Error"]["Code"] != "ResourceNotFoundException":
                    logger.warning(f"  Could not delete agent runtime: {e}")
            deadline = time.time() + 300
            while time.time() < deadline:
                try:
                    agentcore_control_client.get_agent_runtime(agentRuntimeId=runtime_id)
                    time.sleep(5)
                except ClientError as e:
                    if e.response["Error"]["Code"] == "ResourceNotFoundException":
                        break
                    raise
            logger.info(f"  ✓ Deleted Knowledge Base MCP Runtime: {runtime_name}")
        else:
            logger.info(f"  No Knowledge Base MCP Runtime named {runtime_name}")
    except Exception as e:
        logger.warning(f"  Could not delete Knowledge Base MCP Runtime: {e}")

    repo = cfg.get("knowledge_base_mcp_ecr_repository") or runtime_name
    try:
        ecr = boto3.client("ecr", region_name=region)
        ecr.delete_repository(repositoryName=repo, force=True)
        logger.info(f"  ✓ Deleted ECR repository: {repo}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "RepositoryNotFoundException":
            logger.info(f"  ECR repository not found: {repo}")
        else:
            logger.warning(f"  Could not delete ECR repository {repo}: {e}")
    logger.info("✓ Knowledge Base MCP Runtime cleanup done")


def _artifact_share_mcp_runtime_name() -> str:
    return f"artifact_share_of_{project_name}".replace("-", "_")


def delete_artifact_share_mcp_runtime(cfg: Dict):
    """Delete Artifact Share MCP AgentCore Runtime and its ECR repository."""
    logger.info("  Deleting Artifact Share MCP Runtime")
    runtime_names = [
        _artifact_share_mcp_runtime_name(),
    ]
    arn = cfg.get("artifact_share_mcp_runtime_arn") or ""

    try:
        next_token = None
        runtime_ids: List[str] = []
        while True:
            kwargs = {}
            if next_token:
                kwargs["nextToken"] = next_token
            response = agentcore_control_client.list_agent_runtimes(**kwargs)
            for item in response.get("agentRuntimes", []):
                name = item.get("agentRuntimeName")
                if name in runtime_names or (
                    arn and item.get("agentRuntimeArn") == arn
                ):
                    rid = item.get("agentRuntimeId")
                    if rid and rid not in runtime_ids:
                        runtime_ids.append(rid)
            next_token = response.get("nextToken")
            if not next_token:
                break

        for runtime_id in runtime_ids:
            logger.info(f"  Deleting agent runtime: {runtime_id}")
            try:
                agentcore_control_client.delete_agent_runtime(agentRuntimeId=runtime_id)
            except ClientError as e:
                if e.response["Error"]["Code"] != "ResourceNotFoundException":
                    logger.warning(f"  Could not delete agent runtime: {e}")
            deadline = time.time() + 300
            while time.time() < deadline:
                try:
                    agentcore_control_client.get_agent_runtime(agentRuntimeId=runtime_id)
                    time.sleep(5)
                except ClientError as e:
                    if e.response["Error"]["Code"] == "ResourceNotFoundException":
                        break
                    raise
            logger.info(f"  ✓ Deleted Artifact Share MCP Runtime: {runtime_id}")
        if not runtime_ids:
            logger.info("  No Artifact Share MCP Runtime found")
    except Exception as e:
        logger.warning(f"  Could not delete Artifact Share MCP Runtime: {e}")

    repos = {
        cfg.get("artifact_share_mcp_ecr_repository") or "",
        _artifact_share_mcp_runtime_name(),
    }
    ecr = boto3.client("ecr", region_name=region)
    for repo in {r for r in repos if r}:
        try:
            ecr.delete_repository(repositoryName=repo, force=True)
            logger.info(f"  ✓ Deleted ECR repository: {repo}")
        except ClientError as e:
            if e.response["Error"]["Code"] == "RepositoryNotFoundException":
                logger.info(f"  ECR repository not found: {repo}")
            else:
                logger.warning(f"  Could not delete ECR repository {repo}: {e}")
    logger.info("✓ Artifact Share MCP Runtime cleanup done")


def delete_knowledge_bases():
    """Delete Knowledge Bases and their data sources."""
    logger.info("  Deleting Knowledge Bases")

    try:
        kb_list = bedrock_agent_client.list_knowledge_bases()
        knowledge_bases = kb_list.get("knowledgeBaseSummaries", [])

        kb_to_delete = []
        for kb in knowledge_bases:
            if kb["name"] == project_name:
                kb_to_delete.append(kb["knowledgeBaseId"])
                logger.info(f"  Knowledge Base found: {kb['knowledgeBaseId']}")

        if not kb_to_delete:
            logger.info(f"  No Knowledge Base found with name: {project_name}")
            return

        for kb_id in kb_to_delete:
            try:
                logger.info(f"  Deleting Knowledge Base: {kb_id}")

                try:
                    data_sources = bedrock_agent_client.list_data_sources(
                        knowledgeBaseId=kb_id,
                        maxResults=100,
                    )
                    for ds in data_sources.get("dataSourceSummaries", []):
                        try:
                            bedrock_agent_client.delete_data_source(
                                knowledgeBaseId=kb_id,
                                dataSourceId=ds["dataSourceId"],
                            )
                            logger.info(f"    ✓ Deleted data source: {ds['dataSourceId']}")
                        except Exception as e:
                            logger.warning(
                                f"    Could not delete data source {ds['dataSourceId']}: {e}"
                            )
                except Exception as e:
                    logger.debug(f"    Error listing/deleting data sources: {e}")

                bedrock_agent_client.delete_knowledge_base(knowledgeBaseId=kb_id)
                logger.info(f"  ✓ Deleted Knowledge Base: {kb_id}")

                max_wait = 60
                waited = 0
                while waited < max_wait:
                    try:
                        kb_response = bedrock_agent_client.get_knowledge_base(
                            knowledgeBaseId=kb_id
                        )
                        status = kb_response["knowledgeBase"]["status"]
                        if status == "DELETED":
                            break
                        time.sleep(5)
                        waited += 5
                    except ClientError as e:
                        if e.response["Error"]["Code"] == "ResourceNotFoundException":
                            break
                        raise

            except ClientError as e:
                if e.response["Error"]["Code"] == "ResourceNotFoundException":
                    logger.debug(f"  Knowledge Base {kb_id} already deleted")
                else:
                    logger.warning(f"  Could not delete Knowledge Base {kb_id}: {e}")
            except Exception as e:
                logger.warning(f"  Error deleting Knowledge Base {kb_id}: {e}")

        logger.info("✓ Knowledge Bases deleted")
    except Exception as e:
        logger.warning(f"  Could not list/delete Knowledge Bases: {e}")


def delete_s3_vectors_store():
    """Delete S3 Vectors index and vector bucket created by installer.py."""
    logger.info("  Deleting S3 Vectors store")

    def _delete_vector_index() -> bool:
        max_wait = 120
        waited = 0
        while waited <= max_wait:
            try:
                s3vectors_client.delete_index(
                    vectorBucketName=vector_bucket_name,
                    indexName=vector_index_name,
                )
                logger.info(f"  ✓ Deleted vector index: {vector_index_name}")
                return True
            except ClientError as e:
                code = e.response["Error"]["Code"]
                if code == "NotFoundException":
                    logger.info(f"  Vector index not found: {vector_index_name}")
                    return True
                if code == "ConflictException" and waited < max_wait:
                    logger.info(
                        "  Vector index still in use; waiting for Knowledge Base cleanup..."
                    )
                    time.sleep(10)
                    waited += 10
                    continue
                logger.warning(f"  Could not delete vector index {vector_index_name}: {e}")
                return False
        return False

    try:
        _delete_vector_index()

        try:
            s3vectors_client.delete_vector_bucket(
                vectorBucketName=vector_bucket_name,
            )
            logger.info(f"  ✓ Deleted vector bucket: {vector_bucket_name}")
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code == "NotFoundException":
                logger.info(f"  Vector bucket not found: {vector_bucket_name}")
            else:
                logger.warning(
                    f"  Could not delete vector bucket {vector_bucket_name}: {e}"
                )

        logger.info("✓ S3 Vectors store deleted")
    except Exception as e:
        logger.error(f"Error deleting S3 Vectors store: {e}")


INSTALLER_CONFIG_KEYS = [
    "executionRoleArn",
    "agentcore_memory_role",
    "agent_memory_arn",
    "memory_id",
    "memoryId",
    "harnessId",
    "HARNESS_ARN",
    "harness_runtime_arn",
    "evaluation_execution_role_arn",
    "online_evaluation_config_name",
    "online_evaluation_config_id",
    "evaluation_service_name",
    "evaluation_log_group",
    "evaluation_session_timeout_minutes",
    "s3_bucket",
    "s3_arn",
    "sharing_url",
    "app_url",
    "ui_cloudfront_domain",
    "ui_cloudfront_id",
    "vpc_id",
    "s3_files_file_system_id",
    "s3_files_access_point_arn",
    "s3_files_mount_path",
    "s3_files_app_data_file_system_id",
    "s3_files_app_data_access_point_arn",
    "s3_files_app_data_mount_path",
    "agent_runtime_vpc_subnets",
    "agent_runtime_security_groups",
    "ecs_cluster_arn",
    "ecs_service_name",
    "ecs_task_definition_arn",
    "ecr_image_uri",
    "latest_image_tag",
    "build_number",
    "knowledge_base_id",
    "data_source_id",
    "knowledge_base_role",
    "knowledge_base_mcp_runtime_arn",
    "knowledge_base_mcp_url",
    "knowledge_base_mcp_role",
    "knowledge_base_mcp_ecr_repository",
    "knowledge_base_mcp_image_tag",
    "knowledge_base_mcp_gateway_target_id",
    "knowledge_base_mcp_gateway_arn",  # legacy
    "knowledge_base_mcp_gateway_id",  # legacy
    "knowledge_base_mcp_gateway_role",  # legacy
    "artifact_share_mcp_runtime_arn",
    "artifact_share_mcp_url",
    "artifact_share_mcp_role",
    "artifact_share_mcp_ecr_repository",
    "artifact_share_mcp_image_tag",
    "artifact_share_mcp_gateway_target_id",
    "agentcore_gateway_arn",
    "agentcore_gateway_id",
    "agentcore_gateway_role",
    "cognito_user_pool_id",
    "cognito_user_pool_name",
    "cognito_client_id",
    "cognito_client_name",
    "cognito_admin_username",
    "cognito_region",
    "vector_bucket_name",
    "vector_bucket_arn",
    "vector_index_name",
    "vector_index_arn",
]


def _find_cognito_user_pool_id(pool_name: str):
    next_token = None
    while True:
        kwargs = {"MaxResults": 60}
        if next_token:
            kwargs["NextToken"] = next_token
        response = cognito_idp_client.list_user_pools(**kwargs)
        for pool in response.get("UserPools", []):
            if pool.get("Name") == pool_name:
                return pool["Id"]
        next_token = response.get("NextToken")
        if not next_token:
            return None


def delete_session_signing_key_secret() -> None:
    """Delete HMAC session cookie signing key from Secrets Manager."""
    logger.info("Deleting session signing key secret")
    secret_name = SESSION_SIGNING_KEY_SECRET_NAME
    try:
        secretsmanager_client.delete_secret(
            SecretId=secret_name,
            ForceDeleteWithoutRecovery=True,
        )
        logger.info(f"  ✓ Deleted Secrets Manager secret: {secret_name}")
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("ResourceNotFoundException", "ResourceNotFound"):
            logger.info(f"  Secret not found: {secret_name}")
        else:
            logger.warning(f"  Could not delete secret {secret_name}: {e}")


def delete_cognito_user_pool() -> None:
    """Delete Cognito User Pool created for Web UI authentication."""
    logger.info("Deleting Cognito User Pool")
    pool_name = project_name
    user_pool_id = None

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)
        user_pool_id = (config.get("cognito_user_pool_id") or "").strip() or None
    except (OSError, json.JSONDecodeError, TypeError):
        pass

    if not user_pool_id:
        try:
            user_pool_id = _find_cognito_user_pool_id(pool_name)
        except ClientError as error:
            logger.warning(f"  Could not list Cognito User Pools: {error}")
            return

    if not user_pool_id:
        logger.info(f"  Cognito User Pool not found (name={pool_name})")
        return

    try:
        clients = cognito_idp_client.list_user_pool_clients(
            UserPoolId=user_pool_id, MaxResults=60
        )
        for client in clients.get("UserPoolClients", []):
            client_id = client["ClientId"]
            try:
                cognito_idp_client.delete_user_pool_client(
                    UserPoolId=user_pool_id, ClientId=client_id
                )
                logger.info(f"  ✓ Deleted Cognito App Client: {client_id}")
            except ClientError as error:
                logger.warning(
                    f"  Could not delete Cognito App Client {client_id}: {error}"
                )

        cognito_idp_client.delete_user_pool(UserPoolId=user_pool_id)
        logger.info(f"  ✓ Deleted Cognito User Pool: {user_pool_id} (name={pool_name})")
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code", "")
        if code == "ResourceNotFoundException":
            logger.info(f"  Cognito User Pool already deleted: {user_pool_id}")
        else:
            logger.warning(
                f"  Could not delete Cognito User Pool {user_pool_id}: {error}"
            )


def clear_config_json():
    logger.info("[8/8] Clearing installer fields from config.json")
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        logger.warning(f"  Could not read config: {e}")
        return

    for key in INSTALLER_CONFIG_KEYS:
        cfg.pop(key, None)

    # Keep projectName / region / accountId for reinstall
    cfg["projectName"] = project_name
    cfg["region"] = region
    cfg["accountId"] = account_id

    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        logger.info(f"✓ Updated {CONFIG_PATH}")
    except Exception as e:
        logger.warning(f"  Could not write config: {e}")


def main():
    parser = argparse.ArgumentParser(
        description="Uninstall AgentCore Harness infrastructure from installer.py"
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip confirmation prompts",
    )
    parser.add_argument(
        "--keep-s3-bucket",
        action="store_true",
        help="Retain the project S3 bucket (default: delete)",
    )
    parser.add_argument(
        "--keep-cloudfront",
        action="store_true",
        help="Retain the project S3 CloudFront / OAI (default: delete)",
    )
    args = parser.parse_args()

    cfg = load_config()

    logger.info("=" * 60)
    logger.info("Starting AgentCore Harness Infrastructure Uninstall")
    logger.info("=" * 60)
    logger.info(f"Project: {project_name}")
    logger.info(f"Region: {region}")
    logger.info(f"Account ID: {account_id}")
    logger.info(f"S3 Bucket: {_bucket_name()}")
    logger.info(f"S3 CloudFront: {_cloudfront_comment()}")
    logger.info(f"Config: {CONFIG_PATH}")
    logger.info("=" * 60)

    if not args.yes:
        if not prompt_yes_no(
            "Delete installer-managed harness resources for this project?",
            default=False,
        ):
            logger.info("Aborted.")
            sys.exit(0)
        delete_s3_bucket_flag = prompt_yes_no(
            f"Delete S3 bucket ({_bucket_name()})?",
            default=True,
        )
        delete_cloudfront_flag = prompt_yes_no(
            f"Delete S3 CloudFront ({_cloudfront_comment()})?",
            default=True,
        )
    else:
        delete_s3_bucket_flag = not args.keep_s3_bucket
        delete_cloudfront_flag = not args.keep_cloudfront

    start = time.time()
    try:
        # Web UI stack first (depends on VPC SGs / ALB)
        delete_ui_cloudfront(project_name, region, logger)
        delete_ecs_resources(project_name, region, logger)
        delete_alb_resources(project_name, region, logger)

        if delete_cloudfront_flag:
            disable_cloudfront_distributions()

        harness_id = resolve_harness_id(cfg)
        if harness_id:
            if not delete_harness(harness_id):
                logger.warning(
                    "Harness delete incomplete; continuing with remaining cleanup"
                )
        else:
            logger.info("No harness id found; skipping DeleteHarness")

        delete_s3_files_session_storage(cfg)
        delete_vpc(cfg)

        memory_id = resolve_memory_id(cfg)
        if memory_id:
            delete_memory(memory_id)
        else:
            logger.info("No memory id found; skipping DeleteMemory")

        delete_agentcore_gateway(cfg)
        delete_artifact_share_mcp_runtime(cfg)
        delete_knowledge_base_mcp_runtime(cfg)
        delete_knowledge_bases()
        delete_s3_vectors_store()

        delete_bedrock_guardrail(cfg)
        delete_cloudwatch_monitoring_dashboard(cfg)
        delete_online_evaluation(cfg)

        delete_ecs_iam_roles(project_name, region, logger)
        delete_alb_origin_header_secret(project_name, region, logger)
        delete_iam_roles()

        delete_cognito_user_pool()
        delete_session_signing_key_secret()

        if delete_s3_bucket_flag:
            delete_s3_bucket()
        else:
            logger.info(f"S3 bucket retained: {_bucket_name()}")

        if delete_cloudfront_flag:
            wait_for_cloudfront_disabled(max_wait=600, poll_interval=20)
            delete_cloudfront_distributions()
            delete_cloudfront_oai()
        else:
            logger.info(f"S3 CloudFront retained: {_cloudfront_comment()}")

        # UI CF may still be disabling; best-effort delete if already disabled
        delete_disabled_ui_cloudfront(project_name, region, logger)

        clear_config_json()

        elapsed = time.time() - start
        logger.info("")
        logger.info("=" * 60)
        logger.info("Uninstall completed")
        logger.info(f"Total time: {elapsed / 60:.2f} minutes")
        logger.info(
            "Note: if UI CloudFront delete failed (Deploying), re-run uninstaller later"
        )
        logger.info("=" * 60)
    except Exception as e:
        logger.error(f"Uninstall failed: {e}")
        import traceback

        logger.error(traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
