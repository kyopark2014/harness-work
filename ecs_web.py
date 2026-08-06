"""ECS Fargate + ALB + ECR + CloudFront deployment for Harness Web UI.

Modeled after strands-work installer ECS patterns, simplified for harness-work:
- ALB-only CloudFront (separate from project S3 sharing CF)
- No Cognito / signed cookies
- APP_CONFIG_JSON injected into the container via docker-entrypoint.sh
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import boto3
from botocore.exceptions import ClientError

CUSTOM_HEADER_NAME = "X-Custom-Header"
SSE_ORIGIN_READ_TIMEOUT_SECONDS = 60
ALB_IDLE_TIMEOUT_SECONDS = 600
# Managed CloudFront cache/origin policies
CF_CACHE_POLICY_DISABLED = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
CF_ORIGIN_REQUEST_ALL_VIEWER = "216adef6-5c7f-47e4-b989-5492eafa07d3"

ECS_SERVICE_LINKED_ROLE_NAME = "AWSServiceRoleForECS"
DOCKER_MIN_FREE_MB = 2048


class EcsWebDeployer:
    def __init__(
        self,
        *,
        project_name: str,
        region: str,
        account_id: str,
        logger,
        bucket_name: str,
    ):
        self.project_name = project_name
        self.region = region
        self.account_id = account_id
        self.logger = logger
        self.bucket_name = bucket_name
        self.project_root = os.path.dirname(os.path.abspath(__file__))

        self.ec2 = boto3.client("ec2", region_name=region)
        self.elbv2 = boto3.client("elbv2", region_name=region)
        self.ecs = boto3.client("ecs", region_name=region)
        self.ecr = boto3.client("ecr", region_name=region)
        self.logs = boto3.client("logs", region_name=region)
        self.iam = boto3.client("iam", region_name=region)
        self.secrets = boto3.client("secretsmanager", region_name=region)
        self.cloudfront = boto3.client("cloudfront", region_name=region)
        self.s3files = boto3.client("s3files", region_name=region)

        self.origin_secret_name = f"{project_name}/cloudfront-alb-origin-header"

    # ------------------------------------------------------------------ IAM
    def _create_iam_role(
        self,
        role_name: str,
        assume_role_policy: Dict,
        managed_policies: Optional[List[str]] = None,
        description: str = "",
    ) -> str:
        try:
            resp = self.iam.create_role(
                RoleName=role_name,
                AssumeRolePolicyDocument=json.dumps(assume_role_policy),
                Description=description or role_name,
            )
            arn = resp["Role"]["Arn"]
            self.logger.info(f"  ✓ Created IAM role: {role_name}")
        except ClientError as e:
            if e.response["Error"]["Code"] != "EntityAlreadyExists":
                raise
            arn = self.iam.get_role(RoleName=role_name)["Role"]["Arn"]
            self.logger.info(f"  Reusing IAM role: {role_name}")

        for policy_arn in managed_policies or []:
            try:
                self.iam.attach_role_policy(RoleName=role_name, PolicyArn=policy_arn)
            except ClientError as e:
                self.logger.warning(f"  Could not attach {policy_arn}: {e}")
        return arn

    def _attach_inline_policy(
        self, role_name: str, policy_name: str, document: Dict
    ) -> None:
        self.iam.put_role_policy(
            RoleName=role_name,
            PolicyName=policy_name[:128],
            PolicyDocument=json.dumps(document),
        )

    def create_ecs_roles(self) -> Dict[str, str]:
        self.logger.info("Creating ECS IAM roles for Web UI")
        task_role_name = f"role-ecs-task-for-{self.project_name}-{self.region}"
        execution_role_name = f"role-ecs-execution-for-{self.project_name}-{self.region}"
        if len(task_role_name) > 64:
            task_role_name = task_role_name[:64]
        if len(execution_role_name) > 64:
            execution_role_name = execution_role_name[:64]

        assume = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }
            ],
        }
        task_role_arn = self._create_iam_role(
            task_role_name, assume, description="ECS task role for Harness Web UI"
        )
        execution_role_arn = self._create_iam_role(
            execution_role_name,
            assume,
            managed_policies=[
                "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
            ],
            description="ECS execution role for Harness Web UI",
        )

        bucket_arn = f"arn:aws:s3:::{self.bucket_name}"
        self._attach_inline_policy(
            task_role_name,
            f"ecs-task-bedrock-policy-for-{self.project_name}",
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "InvokeBedrockModels",
                        "Effect": "Allow",
                        "Action": [
                            "bedrock:InvokeModel",
                            "bedrock:InvokeModelWithResponseStream",
                            "bedrock:GetInferenceProfile",
                            "bedrock:GetFoundationModel",
                        ],
                        "Resource": [
                            "arn:aws:bedrock:*::foundation-model/*",
                            f"arn:aws:bedrock:{self.region}:{self.account_id}:inference-profile/*",
                            f"arn:aws:bedrock:*:{self.account_id}:inference-profile/*",
                        ],
                    }
                ],
            },
        )
        self._attach_inline_policy(
            task_role_name,
            f"ecs-task-agentcore-policy-for-{self.project_name}",
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "InvokeHarness",
                        "Effect": "Allow",
                        "Action": [
                            "bedrock-agentcore:InvokeHarness",
                            "bedrock-agentcore:InvokeAgentRuntime",
                            "bedrock-agentcore:StopRuntimeSession",
                            "bedrock-agentcore:GetWorkloadAccessToken",
                            "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
                            "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
                        ],
                        "Resource": ["*"],
                    },
                    {
                        "Sid": "ListGetHarness",
                        "Effect": "Allow",
                        "Action": [
                            "bedrock-agentcore:ListHarnesses",
                            "bedrock-agentcore:GetHarness",
                            "bedrock-agentcore-control:ListHarnesses",
                            "bedrock-agentcore-control:GetHarness",
                        ],
                        "Resource": ["*"],
                    },
                ],
            },
        )
        self._attach_inline_policy(
            task_role_name,
            f"ecs-task-s3-policy-for-{self.project_name}",
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "ListProjectBucket",
                        "Effect": "Allow",
                        "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
                        "Resource": [bucket_arn],
                    },
                    {
                        "Sid": "ReadWriteProjectObjects",
                        "Effect": "Allow",
                        "Action": [
                            "s3:GetObject",
                            "s3:PutObject",
                            "s3:DeleteObject",
                        ],
                        "Resource": [f"{bucket_arn}/*"],
                    },
                ],
            },
        )
        self.logger.info(f"✓ ECS roles ready: task={task_role_name}")
        return {
            "task_role_arn": task_role_arn,
            "execution_role_arn": execution_role_arn,
            "task_role_name": task_role_name,
            "execution_role_name": execution_role_name,
        }

    # --------------------------------------------------------------- network
    def _cloudfront_prefix_list_id(self) -> str:
        response = self.ec2.describe_managed_prefix_lists(
            Filters=[
                {
                    "Name": "prefix-list-name",
                    "Values": ["com.amazonaws.global.cloudfront.origin-facing"],
                }
            ]
        )
        items = response.get("PrefixLists") or []
        if not items:
            raise RuntimeError(
                "CloudFront managed prefix list "
                "'com.amazonaws.global.cloudfront.origin-facing' not found"
            )
        return items[0]["PrefixListId"]

    def _find_sg_by_name(self, vpc_id: str, group_name: str) -> Optional[str]:
        sgs = self.ec2.describe_security_groups(
            Filters=[
                {"Name": "vpc-id", "Values": [vpc_id]},
                {"Name": "group-name", "Values": [group_name]},
            ]
        ).get("SecurityGroups") or []
        if sgs:
            return sgs[0]["GroupId"]
        # Also match by Name tag
        sgs = self.ec2.describe_security_groups(
            Filters=[
                {"Name": "vpc-id", "Values": [vpc_id]},
                {"Name": "tag:Name", "Values": [group_name]},
            ]
        ).get("SecurityGroups") or []
        return sgs[0]["GroupId"] if sgs else None

    def _create_security_group(
        self,
        vpc_id: str,
        group_name: str,
        description: str,
        ingress_rules: Optional[List[Dict]] = None,
    ) -> str:
        existing = self._find_sg_by_name(vpc_id, group_name)
        if existing:
            self.logger.info(f"  Reusing SG {group_name}: {existing}")
            sg_id = existing
        else:
            resp = self.ec2.create_security_group(
                GroupName=group_name,
                Description=description,
                VpcId=vpc_id,
                TagSpecifications=[
                    {
                        "ResourceType": "security-group",
                        "Tags": [{"Key": "Name", "Value": group_name}],
                    }
                ],
            )
            sg_id = resp["GroupId"]
            self.logger.info(f"  ✓ Created SG {group_name}: {sg_id}")

        for rule in ingress_rules or []:
            try:
                self.ec2.authorize_security_group_ingress(
                    GroupId=sg_id, IpPermissions=[rule]
                )
            except ClientError as e:
                if e.response["Error"]["Code"] != "InvalidPermission.Duplicate":
                    raise
        return sg_id

    def ensure_web_security_groups(self, vpc_info: Dict) -> Dict:
        vpc_id = str(vpc_info["vpc_id"])
        prefix_list_id = self._cloudfront_prefix_list_id()
        alb_sg_name = f"alb-sg-for-{self.project_name}"
        ecs_sg_name = f"ecs-sg-for-{self.project_name}"

        alb_sg_id = self._create_security_group(
            vpc_id,
            alb_sg_name,
            f"ALB SG for {self.project_name}",
            ingress_rules=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 80,
                    "ToPort": 80,
                    "PrefixListIds": [{"PrefixListId": prefix_list_id}],
                }
            ],
        )
        # Remove open-world ingress if present
        try:
            sg = self.ec2.describe_security_groups(GroupIds=[alb_sg_id])[
                "SecurityGroups"
            ][0]
            for perm in sg.get("IpPermissions") or []:
                if perm.get("FromPort") != 80:
                    continue
                open_cidrs = [
                    r
                    for r in perm.get("IpRanges") or []
                    if r.get("CidrIp") == "0.0.0.0/0"
                ]
                if open_cidrs:
                    self.ec2.revoke_security_group_ingress(
                        GroupId=alb_sg_id,
                        IpPermissions=[
                            {
                                "IpProtocol": "tcp",
                                "FromPort": 80,
                                "ToPort": 80,
                                "IpRanges": open_cidrs,
                            }
                        ],
                    )
        except ClientError as e:
            self.logger.debug(f"  ALB SG cleanup: {e}")

        ecs_sg_id = self._create_security_group(
            vpc_id,
            ecs_sg_name,
            f"ECS SG for {self.project_name}",
            ingress_rules=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 8501,
                    "ToPort": 8501,
                    "UserIdGroupPairs": [{"GroupId": alb_sg_id}],
                }
            ],
        )
        try:
            self.ec2.authorize_security_group_egress(
                GroupId=ecs_sg_id,
                IpPermissions=[
                    {"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}
                ],
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "InvalidPermission.Duplicate":
                self.logger.debug(f"  ECS SG egress: {e}")

        vpc_info = dict(vpc_info)
        vpc_info["alb_sg_id"] = alb_sg_id
        vpc_info["ecs_sg_id"] = ecs_sg_id
        return vpc_info

    def create_alb(self, vpc_info: Dict) -> Dict[str, str]:
        self.logger.info("Creating Application Load Balancer for Web UI")
        alb_name = f"alb-for-{self.project_name}"
        # ALB name max 32 chars
        if len(alb_name) > 32:
            alb_name = alb_name[:32]

        try:
            albs = self.elbv2.describe_load_balancers(Names=[alb_name])
            if albs["LoadBalancers"]:
                alb = albs["LoadBalancers"][0]
                self.logger.info(f"  Reusing ALB: {alb['DNSName']}")
                self._ensure_alb_idle_timeout(alb["LoadBalancerArn"])
                return {"arn": alb["LoadBalancerArn"], "dns": alb["DNSName"]}
        except ClientError as e:
            if e.response["Error"]["Code"] != "LoadBalancerNotFound":
                raise

        public_subnets = list(vpc_info.get("public_subnets") or [])
        if len(public_subnets) < 2:
            raise RuntimeError(
                f"ALB requires >=2 public subnets; found {len(public_subnets)}"
            )
        alb_sg_id = vpc_info.get("alb_sg_id")
        if not alb_sg_id:
            raise RuntimeError("alb_sg_id missing; call ensure_web_security_groups first")

        resp = self.elbv2.create_load_balancer(
            Name=alb_name,
            Subnets=public_subnets,
            SecurityGroups=[alb_sg_id],
            Scheme="internet-facing",
            Type="application",
            Tags=[{"Key": "Name", "Value": alb_name}],
        )
        alb_arn = resp["LoadBalancers"][0]["LoadBalancerArn"]
        alb_dns = resp["LoadBalancers"][0]["DNSName"]
        self._ensure_alb_idle_timeout(alb_arn)
        self.logger.info(f"✓ ALB created: {alb_dns}")
        return {"arn": alb_arn, "dns": alb_dns}

    def _ensure_alb_idle_timeout(self, alb_arn: str) -> None:
        try:
            self.elbv2.modify_load_balancer_attributes(
                LoadBalancerArn=alb_arn,
                Attributes=[
                    {
                        "Key": "idle_timeout.timeout_seconds",
                        "Value": str(ALB_IDLE_TIMEOUT_SECONDS),
                    }
                ],
            )
        except ClientError as e:
            self.logger.warning(f"  Could not set ALB idle timeout: {e}")

    # -------------------------------------------------------------- secrets
    def get_or_create_alb_origin_header(self) -> str:
        secret_name = self.origin_secret_name
        try:
            existing = self.secrets.get_secret_value(SecretId=secret_name)
            current = (existing.get("SecretString") or "").strip()
            if current:
                self.logger.info(f"  Reusing ALB origin header secret: {secret_name}")
                return current
        except ClientError as e:
            if e.response["Error"]["Code"] not in {
                "ResourceNotFoundException",
                "ResourceNotFound",
            }:
                raise

        new_value = secrets.token_urlsafe(32)
        try:
            self.secrets.create_secret(
                Name=secret_name,
                Description=(
                    f"CloudFront→ALB origin header ({CUSTOM_HEADER_NAME}) "
                    f"for {self.project_name}"
                ),
                SecretString=new_value,
                Tags=[{"Key": "Name", "Value": secret_name}],
            )
            self.logger.info(f"  ✓ Created ALB origin header secret: {secret_name}")
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceExistsException":
                self.secrets.put_secret_value(
                    SecretId=secret_name, SecretString=new_value
                )
            else:
                raise
        return new_value

    # ----------------------------------------------------------- CloudFront
    def _ui_cloudfront_comment(self) -> str:
        return f"CloudFront-for-{self.project_name}"

    def _find_ui_cloudfront(self) -> Optional[Dict]:
        comment = self._ui_cloudfront_comment()
        marker = None
        while True:
            kwargs: Dict = {}
            if marker:
                kwargs["Marker"] = marker
            resp = self.cloudfront.list_distributions(**kwargs)
            listing = resp.get("DistributionList") or {}
            for item in listing.get("Items") or []:
                if item.get("Comment") == comment:
                    return item
            if not listing.get("IsTruncated"):
                return None
            marker = listing.get("NextMarker")

    def create_ui_cloudfront(
        self, alb_info: Dict, origin_header_value: str
    ) -> Dict[str, str]:
        self.logger.info("Creating CloudFront distribution for Web UI (ALB origin)")
        existing = self._find_ui_cloudfront()
        if existing:
            dist_id = existing["Id"]
            domain = existing["DomainName"]
            self.logger.info(f"  Reusing UI CloudFront: {domain} ({dist_id})")
            self._ensure_cf_alb_origin(dist_id, alb_info["dns"], origin_header_value)
            return {
                "id": dist_id,
                "domain": domain,
                "arn": existing.get("ARN", ""),
            }

        caller = f"{self.project_name}-ui-{int(time.time())}"
        config = {
            "CallerReference": caller,
            "Comment": self._ui_cloudfront_comment(),
            "Enabled": True,
            "DefaultRootObject": "",
            "Origins": {
                "Quantity": 1,
                "Items": [
                    {
                        "Id": f"alb-{self.project_name}",
                        "DomainName": alb_info["dns"],
                        "OriginPath": "",
                        "CustomHeaders": {
                            "Quantity": 1,
                            "Items": [
                                {
                                    "HeaderName": CUSTOM_HEADER_NAME,
                                    "HeaderValue": origin_header_value,
                                }
                            ],
                        },
                        "CustomOriginConfig": {
                            "HTTPPort": 80,
                            "HTTPSPort": 443,
                            "OriginProtocolPolicy": "http-only",
                            "OriginSslProtocols": {
                                "Quantity": 1,
                                "Items": ["TLSv1.2"],
                            },
                            "OriginReadTimeout": SSE_ORIGIN_READ_TIMEOUT_SECONDS,
                            "OriginKeepaliveTimeout": 60,
                        },
                    }
                ],
            },
            "DefaultCacheBehavior": {
                "TargetOriginId": f"alb-{self.project_name}",
                "ViewerProtocolPolicy": "redirect-to-https",
                "AllowedMethods": {
                    "Quantity": 7,
                    "Items": [
                        "GET",
                        "HEAD",
                        "OPTIONS",
                        "PUT",
                        "POST",
                        "PATCH",
                        "DELETE",
                    ],
                    "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]},
                },
                "Compress": True,
                "CachePolicyId": CF_CACHE_POLICY_DISABLED,
                "OriginRequestPolicyId": CF_ORIGIN_REQUEST_ALL_VIEWER,
            },
            "PriceClass": "PriceClass_All",
            "ViewerCertificate": {"CloudFrontDefaultCertificate": True},
            "HttpVersion": "http2",
            "IsIPV6Enabled": True,
        }
        resp = self.cloudfront.create_distribution(DistributionConfig=config)
        dist = resp["Distribution"]
        self.logger.info(f"✓ UI CloudFront created: {dist['DomainName']}")
        return {
            "id": dist["Id"],
            "domain": dist["DomainName"],
            "arn": dist.get("ARN", ""),
        }

    def _ensure_cf_alb_origin(
        self, dist_id: str, alb_dns: str, origin_header_value: str
    ) -> None:
        try:
            cfg_resp = self.cloudfront.get_distribution_config(Id=dist_id)
            etag = cfg_resp["ETag"]
            cfg = cfg_resp["DistributionConfig"]
            origin_id = f"alb-{self.project_name}"
            origins = cfg.get("Origins", {}).get("Items") or []
            updated = False
            for origin in origins:
                if origin.get("Id") != origin_id and origin.get("DomainName") != alb_dns:
                    continue
                origin["DomainName"] = alb_dns
                headers = origin.setdefault("CustomHeaders", {"Quantity": 0, "Items": []})
                items = headers.get("Items") or []
                found = False
                for h in items:
                    if h.get("HeaderName") == CUSTOM_HEADER_NAME:
                        if h.get("HeaderValue") != origin_header_value:
                            h["HeaderValue"] = origin_header_value
                            updated = True
                        found = True
                        break
                if not found:
                    items.append(
                        {
                            "HeaderName": CUSTOM_HEADER_NAME,
                            "HeaderValue": origin_header_value,
                        }
                    )
                    updated = True
                headers["Items"] = items
                headers["Quantity"] = len(items)
                coc = origin.setdefault("CustomOriginConfig", {})
                if coc.get("OriginReadTimeout") != SSE_ORIGIN_READ_TIMEOUT_SECONDS:
                    coc["OriginReadTimeout"] = SSE_ORIGIN_READ_TIMEOUT_SECONDS
                    updated = True
            if updated:
                self.cloudfront.update_distribution(
                    Id=dist_id, IfMatch=etag, DistributionConfig=cfg
                )
                self.logger.info("  ✓ Updated UI CloudFront ALB origin / header")
        except ClientError as e:
            self.logger.warning(f"  Could not refresh UI CloudFront origin: {e}")

    # ------------------------------------------------------------------- ECR
    def create_ecr_repository(self) -> str:
        self.logger.info("Creating ECR repository for Web UI")
        name = f"ecr-for-{self.project_name}"
        try:
            resp = self.ecr.create_repository(
                repositoryName=name,
                imageScanningConfiguration={"scanOnPush": True},
                imageTagMutability="MUTABLE",
            )
            uri = resp["repository"]["repositoryUri"]
            self.logger.info(f"  ✓ Created ECR: {uri}")
            return uri
        except ClientError as e:
            if e.response["Error"]["Code"] != "RepositoryAlreadyExistsException":
                raise
            resp = self.ecr.describe_repositories(repositoryNames=[name])
            uri = resp["repositories"][0]["repositoryUri"]
            self.logger.info(f"  Reusing ECR: {uri}")
            return uri

    def resolve_ecr_image_uri(
        self, repository_uri: str, image_tag: Optional[str] = None
    ) -> str:
        if image_tag:
            return f"{repository_uri}:{image_tag}"
        config_path = os.path.join(self.project_root, "application", "config.json")
        try:
            with open(config_path, encoding="utf-8") as f:
                data = json.load(f)
            saved = data.get("latest_image_tag") or data.get("build_number")
            if saved:
                self.logger.info(f"  Using saved image tag: {saved}")
                return f"{repository_uri}:{saved}"
        except (OSError, json.JSONDecodeError):
            pass
        repo_name = repository_uri.rsplit("/", 1)[-1]
        try:
            resp = self.ecr.describe_images(
                repositoryName=repo_name, filter={"tagStatus": "TAGGED"}
            )
            images = resp.get("imageDetails") or []
            if images:
                latest = sorted(images, key=lambda x: x["imagePushedAt"], reverse=True)[
                    0
                ]
                tags = latest.get("imageTags") or []
                if tags:
                    return f"{repository_uri}:{tags[0]}"
        except ClientError:
            pass
        return f"{repository_uri}:latest"

    def _require_arm64(self) -> None:
        machine = os.uname().machine.lower()
        if machine not in ("aarch64", "arm64"):
            raise RuntimeError(
                "ECS image build requires linux/arm64 (native ARM host).\n"
                f"  Current architecture: {os.uname().machine}\n"
                "  Build on an ARM64 EC2 (e.g. t4g) or use --skip-docker-build."
            )

    def _docker_daemon_ok(self) -> bool:
        try:
            r = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            return r.returncode == 0
        except Exception:
            return False

    def build_and_push_docker_image(
        self, repository_uri: str
    ) -> Tuple[str, str]:
        self.logger.info("Building and pushing Docker image to ECR")
        if shutil.which("docker") is None:
            raise RuntimeError("Docker CLI is required to build the Web UI image")
        if not self._docker_daemon_ok():
            raise RuntimeError(
                "Docker daemon is not available. Start Docker and retry, "
                "or pass --skip-docker-build if the image is already in ECR."
            )
        self._require_arm64()

        image_tag = datetime.now().strftime("%Y%m%d%H%M%S")
        image_uri = f"{repository_uri}:{image_tag}"
        registry = repository_uri.split("/")[0]

        login = subprocess.run(
            ["aws", "ecr", "get-login-password", "--region", self.region],
            capture_output=True,
            text=True,
            check=False,
        )
        if login.returncode != 0:
            raise RuntimeError(f"ECR login password failed: {login.stderr.strip()}")
        docker_login = subprocess.run(
            ["docker", "login", "--username", "AWS", "--password-stdin", registry],
            input=login.stdout,
            capture_output=True,
            text=True,
            check=False,
        )
        if docker_login.returncode != 0:
            raise RuntimeError(f"docker login failed: {docker_login.stderr.strip()}")

        self.logger.info(f"  Building {image_uri} (linux/arm64)...")
        env = {**os.environ, "DOCKER_BUILDKIT": "1", "BUILDKIT_PROGRESS": "plain"}
        use_buildx = (
            subprocess.run(
                ["docker", "buildx", "version"],
                capture_output=True,
                check=False,
            ).returncode
            == 0
        )
        if use_buildx:
            cmd = [
                "docker",
                "buildx",
                "build",
                "--platform",
                "linux/arm64",
                "--provenance=false",
                "-t",
                image_uri,
                "--push",
                ".",
            ]
        else:
            cmd = [
                "docker",
                "build",
                "--platform",
                "linux/arm64",
                "-t",
                image_uri,
                ".",
            ]

        process = subprocess.Popen(
            cmd,
            cwd=self.project_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        assert process.stdout is not None
        for line in iter(process.stdout.readline, ""):
            stripped = line.rstrip("\r\n")
            if stripped:
                self.logger.info(f"  | {stripped}")
        if process.wait() != 0:
            raise RuntimeError(f"Docker build failed: {' '.join(cmd)}")

        if not use_buildx:
            push = subprocess.run(
                ["docker", "push", image_uri],
                cwd=self.project_root,
                capture_output=True,
                text=True,
                check=False,
            )
            if push.returncode != 0:
                raise RuntimeError(f"docker push failed: {push.stderr.strip()}")

        self._promote_ecr_tag(repository_uri, image_tag, "latest")
        self.logger.info(f"✓ Pushed image: {image_uri}")
        return image_uri, image_tag

    def _promote_ecr_tag(
        self, repository_uri: str, source_tag: str, dest_tag: str
    ) -> None:
        repo = repository_uri.rsplit("/", 1)[-1]
        try:
            images = self.ecr.batch_get_image(
                repositoryName=repo, imageIds=[{"imageTag": source_tag}]
            )
            if not images.get("images"):
                return
            manifest = images["images"][0]["imageManifest"]
            try:
                self.ecr.put_image(
                    repositoryName=repo,
                    imageManifest=manifest,
                    imageTag=dest_tag,
                )
            except ClientError as e:
                if e.response["Error"]["Code"] != "ImageAlreadyExistsException":
                    raise
        except ClientError as e:
            self.logger.warning(f"  Could not promote tag to {dest_tag}: {e}")

    # ----------------------------------------------------------------- ECS
    def create_ecs_log_group(self) -> str:
        name = f"/ecs/app-for-{self.project_name}"
        try:
            self.logs.create_log_group(logGroupName=name)
            self.logger.info(f"  ✓ Created log group: {name}")
        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceAlreadyExistsException":
                raise
            self.logger.info(f"  Reusing log group: {name}")
        return name

    def ensure_ecs_service_linked_role(self) -> None:
        try:
            self.iam.get_role(RoleName=ECS_SERVICE_LINKED_ROLE_NAME)
            return
        except ClientError as e:
            if e.response["Error"]["Code"] != "NoSuchEntity":
                raise
        try:
            self.iam.create_service_linked_role(AWSServiceName="ecs.amazonaws.com")
            self.logger.info("  ✓ Created ECS service-linked role")
            time.sleep(10)
        except ClientError as e:
            if e.response["Error"]["Code"] != "InvalidInput":
                raise

    def prepare_s3files_for_ecs(
        self,
        vpc_info: Dict,
        s3_files_info: Dict,
        ecs_task_role_arn: str,
        ecs_task_role_name: str,
        harness_execution_role_arn: str,
    ) -> None:
        file_system_id = str(s3_files_info.get("file_system_id") or "")
        access_point_arn = str(s3_files_info.get("access_point_arn") or "")
        if not file_system_id or not access_point_arn:
            self.logger.warning("  Skipping S3 Files ECS prep: missing FS/AP")
            return

        ecs_sg_id = vpc_info.get("ecs_sg_id")
        mount_sg_id = s3_files_info.get("mount_sg_id")
        if not mount_sg_id:
            mount_sg_id = self._find_sg_by_name(
                str(vpc_info["vpc_id"]),
                f"s3files-mount-sg-for-{self.project_name}",
            )
        if ecs_sg_id and mount_sg_id:
            self._ensure_nfs(str(ecs_sg_id), str(mount_sg_id))

        principals = [
            a for a in [harness_execution_role_arn, ecs_task_role_arn] if a
        ]
        if principals:
            policy = {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {
                            "AWS": principals if len(principals) > 1 else principals[0]
                        },
                        "Action": [
                            "s3files:ClientMount",
                            "s3files:ClientWrite",
                            "s3files:ClientRootAccess",
                        ],
                        "Condition": {
                            "StringEquals": {
                                "s3files:AccessPointArn": access_point_arn
                            }
                        },
                    }
                ],
            }
            try:
                self.s3files.put_file_system_policy(
                    fileSystemId=file_system_id,
                    policy=json.dumps(policy),
                )
                self.logger.info("  ✓ Updated S3 Files FS policy for ECS + Harness")
            except ClientError as e:
                self.logger.warning(f"  S3 Files FS policy update failed: {e}")

        fs_arn = (
            s3_files_info.get("file_system_arn")
            or f"arn:aws:s3files:{self.region}:{self.account_id}:file-system/{file_system_id}"
        )
        iam_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "S3FilesClientAccess",
                    "Effect": "Allow",
                    "Action": [
                        "s3files:ClientMount",
                        "s3files:ClientWrite",
                        "s3files:ClientRootAccess",
                    ],
                    "Resource": fs_arn,
                    "Condition": {
                        "ArnEquals": {"s3files:AccessPointArn": access_point_arn}
                    },
                },
                {
                    "Sid": "S3FilesGetAccessPoint",
                    "Effect": "Allow",
                    "Action": ["s3files:GetAccessPoint"],
                    "Resource": access_point_arn,
                },
                {
                    "Sid": "S3FilesListMountTargets",
                    "Effect": "Allow",
                    "Action": ["s3files:ListMountTargets"],
                    "Resource": fs_arn,
                },
            ],
        }
        self._attach_inline_policy(
            ecs_task_role_name,
            f"s3files-ecs-task-policy-for-{self.project_name}",
            iam_policy,
        )
        self.logger.info(f"  ✓ Attached S3 Files policy to {ecs_task_role_name}")

    def _ensure_nfs(self, client_sg_id: str, mount_sg_id: str) -> None:
        try:
            self.ec2.authorize_security_group_egress(
                GroupId=client_sg_id,
                IpPermissions=[
                    {
                        "IpProtocol": "tcp",
                        "FromPort": 2049,
                        "ToPort": 2049,
                        "UserIdGroupPairs": [{"GroupId": mount_sg_id}],
                    }
                ],
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "InvalidPermission.Duplicate":
                self.logger.warning(f"  NFS egress: {e}")
        try:
            self.ec2.authorize_security_group_ingress(
                GroupId=mount_sg_id,
                IpPermissions=[
                    {
                        "IpProtocol": "tcp",
                        "FromPort": 2049,
                        "ToPort": 2049,
                        "UserIdGroupPairs": [{"GroupId": client_sg_id}],
                    }
                ],
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "InvalidPermission.Duplicate":
                self.logger.warning(f"  NFS ingress: {e}")

    def _create_target_group(self, vpc_info: Dict) -> str:
        name = f"TG-for-{self.project_name}"
        if len(name) > 32:
            name = name[:32]
        try:
            tgs = self.elbv2.describe_target_groups(Names=[name])
            if tgs["TargetGroups"]:
                tg = tgs["TargetGroups"][0]
                if tg.get("TargetType") != "ip":
                    raise ValueError(
                        f"Target group {name} TargetType={tg.get('TargetType')}; "
                        "delete it before ECS deploy"
                    )
                tg_arn = tg["TargetGroupArn"]
                self.logger.info(f"  Reusing target group: {tg_arn}")
            else:
                tg_arn = ""
        except ClientError as e:
            if e.response["Error"]["Code"] != "TargetGroupNotFound":
                raise
            tg_arn = ""

        if not tg_arn:
            resp = self.elbv2.create_target_group(
                Name=name,
                Protocol="HTTP",
                Port=8501,
                VpcId=vpc_info["vpc_id"],
                TargetType="ip",
                HealthCheckProtocol="HTTP",
                HealthCheckPath="/api/health",
                HealthCheckIntervalSeconds=30,
                HealthCheckTimeoutSeconds=5,
                HealthyThresholdCount=2,
                UnhealthyThresholdCount=3,
            )
            tg_arn = resp["TargetGroups"][0]["TargetGroupArn"]
            self.logger.info(f"  ✓ Created target group: {tg_arn}")

        try:
            self.elbv2.modify_target_group_attributes(
                TargetGroupArn=tg_arn,
                Attributes=[
                    {"Key": "stickiness.enabled", "Value": "true"},
                    {"Key": "stickiness.type", "Value": "app_cookie"},
                    {"Key": "stickiness.app_cookie.cookie_name", "Value": "agent_user_id"},
                    {"Key": "stickiness.app_cookie.duration_seconds", "Value": "86400"},
                ],
            )
        except ClientError as e:
            self.logger.warning(f"  Stickiness setup failed: {e}")
        return tg_arn

    def _ensure_listener(
        self, alb_arn: str, tg_arn: str, header_value: str
    ) -> str:
        listener_arn = None
        existing = None
        try:
            for listener in self.elbv2.describe_listeners(LoadBalancerArn=alb_arn).get(
                "Listeners", []
            ):
                if listener["Port"] == 80 and listener["Protocol"] == "HTTP":
                    listener_arn = listener["ListenerArn"]
                    existing = listener
                    break
        except ClientError:
            pass

        forbidden = {
            "Type": "fixed-response",
            "FixedResponseConfig": {
                "StatusCode": "403",
                "ContentType": "text/plain",
                "MessageBody": "Forbidden",
            },
        }
        if not listener_arn:
            resp = self.elbv2.create_listener(
                LoadBalancerArn=alb_arn,
                Protocol="HTTP",
                Port=80,
                DefaultActions=[forbidden],
            )
            listener_arn = resp["Listeners"][0]["ListenerArn"]
            self.logger.info(f"  ✓ Created ALB listener (default 403)")
        else:
            actions = (existing or {}).get("DefaultActions") or []
            if not any(a.get("Type") == "fixed-response" for a in actions):
                self.elbv2.modify_listener(
                    ListenerArn=listener_arn, DefaultActions=[forbidden]
                )

        header_condition = [
            {
                "Field": "http-header",
                "HttpHeaderConfig": {
                    "HttpHeaderName": CUSTOM_HEADER_NAME,
                    "Values": [header_value],
                },
            }
        ]
        forward = [{"Type": "forward", "TargetGroupArn": tg_arn}]

        rules = self.elbv2.describe_rules(ListenerArn=listener_arn).get("Rules") or []
        header_rule_arn = None
        for rule in rules:
            if rule.get("Priority") == "default":
                continue
            for cond in rule.get("Conditions") or []:
                if cond.get("Field") != "http-header":
                    continue
                cfg = cond.get("HttpHeaderConfig") or {}
                if cfg.get("HttpHeaderName") == CUSTOM_HEADER_NAME:
                    header_rule_arn = rule["RuleArn"]
                    break
            if header_rule_arn:
                break

        if header_rule_arn:
            self.elbv2.modify_rule(
                RuleArn=header_rule_arn,
                Conditions=header_condition,
                Actions=forward,
            )
        else:
            try:
                self.elbv2.create_rule(
                    ListenerArn=listener_arn,
                    Priority=10,
                    Conditions=header_condition,
                    Actions=forward,
                )
            except ClientError as e:
                if e.response["Error"]["Code"] not in {
                    "PriorityInUse",
                    "RuleAlreadyExists",
                }:
                    raise
                for rule in self.elbv2.describe_rules(ListenerArn=listener_arn).get(
                    "Rules", []
                ):
                    if rule.get("Priority") == "10":
                        self.elbv2.modify_rule(
                            RuleArn=rule["RuleArn"],
                            Conditions=header_condition,
                            Actions=forward,
                        )
                        break
        return listener_arn

    def create_ecs_cluster(self) -> str:
        name = f"cluster-for-{self.project_name}"
        try:
            resp = self.ecs.create_cluster(
                clusterName=name, tags=[{"key": "Name", "value": name}]
            )
            arn = resp["cluster"]["clusterArn"]
            self.logger.info(f"  ✓ Created ECS cluster: {name}")
            return arn
        except ClientError:
            pass
        resp = self.ecs.describe_clusters(clusters=[name])
        clusters = resp.get("clusters") or []
        if clusters and clusters[0].get("status") == "ACTIVE":
            self.logger.info(f"  Reusing ECS cluster: {name}")
            return clusters[0]["clusterArn"]
        resp = self.ecs.create_cluster(
            clusterName=name, tags=[{"key": "Name", "value": name}]
        )
        return resp["cluster"]["clusterArn"]

    def deploy_ecs_service(
        self,
        vpc_info: Dict,
        alb_info: Dict,
        ecs_roles: Dict,
        image_uri: str,
        app_environment: Dict,
        log_group_name: str,
        s3_files_info: Optional[Dict] = None,
        origin_header_value: str = "",
    ) -> Dict[str, str]:
        self.logger.info("Deploying ECS Fargate service for Web UI")
        if not origin_header_value:
            origin_header_value = self.get_or_create_alb_origin_header()
        self.ensure_ecs_service_linked_role()

        private_subnets = list(vpc_info.get("private_subnets") or [])
        if not private_subnets:
            raise ValueError("ECS requires private subnets")
        ecs_sg_id = vpc_info.get("ecs_sg_id")
        if not ecs_sg_id:
            raise ValueError("ecs_sg_id missing")

        cluster_arn = self.create_ecs_cluster()
        tg_arn = self._create_target_group(vpc_info)
        listener_arn = self._ensure_listener(
            alb_info["arn"], tg_arn, origin_header_value
        )

        task_family = f"task-for-{self.project_name}"
        service_name = f"service-for-{self.project_name}"
        container_name = "app"
        app_data_mount = "/mnt/app-data"

        environment = [
            {"name": "APP_CONFIG_JSON", "value": json.dumps(app_environment)},
        ]
        container: Dict[str, object] = {
            "name": container_name,
            "image": image_uri,
            "essential": True,
            "portMappings": [{"containerPort": 8501, "protocol": "tcp"}],
            "environment": environment,
            "logConfiguration": {
                "logDriver": "awslogs",
                "options": {
                    "awslogs-group": log_group_name,
                    "awslogs-region": self.region,
                    "awslogs-stream-prefix": "ecs",
                },
            },
            "healthCheck": {
                "command": [
                    "CMD-SHELL",
                    "curl -f http://localhost:8501/api/health || exit 1",
                ],
                "interval": 30,
                "timeout": 5,
                "retries": 3,
                "startPeriod": 60,
            },
        }

        volumes: List[Dict[str, object]] = []
        s3_files_info = s3_files_info or {}
        file_system_id = s3_files_info.get("file_system_id")
        access_point_arn = s3_files_info.get("access_point_arn")
        if file_system_id and access_point_arn:
            file_system_arn = (
                s3_files_info.get("file_system_arn")
                or f"arn:aws:s3files:{self.region}:{self.account_id}:file-system/{file_system_id}"
            )
            volumes.append(
                {
                    "name": "app-data",
                    "s3filesVolumeConfiguration": {
                        "fileSystemArn": file_system_arn,
                        "rootDirectory": "/",
                        "accessPointArn": access_point_arn,
                    },
                }
            )
            container["mountPoints"] = [
                {
                    "sourceVolume": "app-data",
                    "containerPath": app_data_mount,
                    "readOnly": False,
                }
            ]
            environment.extend(
                [
                    {"name": "TASK_DB_MOUNT", "value": app_data_mount},
                    {"name": "TASK_DB_PROJECT", "value": self.project_name},
                ]
            )
            self.logger.info(f"  ECS will mount S3 Files at {app_data_mount}")

        task_kwargs: Dict[str, object] = {
            "family": task_family,
            "networkMode": "awsvpc",
            "requiresCompatibilities": ["FARGATE"],
            "cpu": "1024",
            "memory": "2048",
            "runtimePlatform": {
                "cpuArchitecture": "ARM64",
                "operatingSystemFamily": "LINUX",
            },
            "executionRoleArn": ecs_roles["execution_role_arn"],
            "taskRoleArn": ecs_roles["task_role_arn"],
            "containerDefinitions": [container],
        }
        if volumes:
            task_kwargs["volumes"] = volumes

        task_def = self.ecs.register_task_definition(**task_kwargs)
        task_definition_arn = task_def["taskDefinition"]["taskDefinitionArn"]
        self.logger.info(f"  ✓ Registered task definition: {task_definition_arn}")

        cluster_name = cluster_arn.split("/")[-1]
        services = self.ecs.describe_services(
            cluster=cluster_name, services=[service_name]
        )
        service_list = services.get("services") or []
        service_arn = None
        if service_list and service_list[0].get("status") != "INACTIVE":
            service_arn = service_list[0]["serviceArn"]
            self.logger.info(f"  Updating existing ECS service: {service_name}")
            self.ecs.update_service(
                cluster=cluster_name,
                service=service_name,
                taskDefinition=task_definition_arn,
                forceNewDeployment=True,
                deploymentConfiguration={
                    "minimumHealthyPercent": 0,
                    "maximumPercent": 100,
                },
            )
        else:
            resp = self.ecs.create_service(
                cluster=cluster_name,
                serviceName=service_name,
                taskDefinition=task_definition_arn,
                desiredCount=1,
                launchType="FARGATE",
                deploymentConfiguration={
                    "minimumHealthyPercent": 0,
                    "maximumPercent": 100,
                },
                networkConfiguration={
                    "awsvpcConfiguration": {
                        "subnets": private_subnets,
                        "securityGroups": [ecs_sg_id],
                        "assignPublicIp": "DISABLED",
                    }
                },
                loadBalancers=[
                    {
                        "targetGroupArn": tg_arn,
                        "containerName": container_name,
                        "containerPort": 8501,
                    }
                ],
                healthCheckGracePeriodSeconds=120,
                tags=[{"key": "Name", "value": service_name}],
            )
            service_arn = resp["service"]["serviceArn"]
            self.logger.info(f"  ✓ Created ECS service: {service_name}")

        self._wait_for_service(cluster_name, service_name)
        return {
            "cluster_arn": cluster_arn,
            "service_arn": service_arn,
            "service_name": service_name,
            "task_definition_arn": task_definition_arn,
            "target_group_arn": tg_arn,
            "listener_arn": listener_arn,
        }

    def _wait_for_service(
        self, cluster_name: str, service_name: str, timeout: int = 1200
    ) -> None:
        self.logger.info("  Waiting for ECS service to become stable...")
        deadline = time.time() + timeout
        last_log = 0.0
        while time.time() < deadline:
            services = self.ecs.describe_services(
                cluster=cluster_name, services=[service_name]
            )
            svc = (services.get("services") or [None])[0]
            if not svc:
                raise RuntimeError(f"ECS service not found: {service_name}")
            deployments = svc.get("deployments") or []
            primary = next(
                (d for d in deployments if d.get("status") == "PRIMARY"), None
            )
            desired = (primary or {}).get("desiredCount", 0)
            running = (primary or {}).get("runningCount", 0)
            pending = (primary or {}).get("pendingCount", 0)
            now = time.time()
            if now - last_log > 30:
                self.logger.info(
                    f"  ... service status={svc.get('status')} "
                    f"running={running}/{desired} pending={pending}"
                )
                last_log = now
            if (
                svc.get("status") == "ACTIVE"
                and primary
                and desired > 0
                and running == desired
                and pending == 0
            ):
                self.logger.info("✓ ECS service is stable")
                return
            time.sleep(15)
        self.logger.warning(
            "ECS service did not become fully stable before timeout; continuing"
        )

    def check_application_ready(
        self, domain: str, max_attempts: int = 120, wait_seconds: int = 10
    ) -> None:
        self.logger.info(f"Checking application readiness at https://{domain}")
        url = f"https://{domain}"
        for attempt in range(1, max_attempts + 1):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "harness-installer"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    if resp.getcode() == 200:
                        self.logger.info(
                            f"✓ Application ready (attempt {attempt}/{max_attempts})"
                        )
                        return
            except urllib.error.HTTPError as e:
                if e.code not in (502, 503, 504):
                    self.logger.info(f"Application responded HTTP {e.code}; treating as ready")
                    return
                if attempt == 1 or attempt % 3 == 0:
                    self.logger.info(
                        f"  Waiting... [{attempt}/{max_attempts}] HTTP {e.code}"
                    )
            except Exception:
                if attempt == 1 or attempt % 3 == 0:
                    self.logger.info(
                        f"  Waiting... [{attempt}/{max_attempts}] connecting"
                    )
            if attempt < max_attempts:
                time.sleep(wait_seconds)
        self.logger.warning(
            "Readiness check timed out; CloudFront/ECS may still be propagating"
        )


# ===================================================================== cleanup
def delete_ecs_resources(project_name: str, region: str, logger) -> None:
    ecs = boto3.client("ecs", region_name=region)
    ecr = boto3.client("ecr", region_name=region)
    elbv2 = boto3.client("elbv2", region_name=region)
    logs = boto3.client("logs", region_name=region)

    cluster_name = f"cluster-for-{project_name}"
    service_name = f"service-for-{project_name}"
    task_family = f"task-for-{project_name}"
    tg_name = f"TG-for-{project_name}"
    if len(tg_name) > 32:
        tg_name = tg_name[:32]
    repo_name = f"ecr-for-{project_name}"
    log_group = f"/ecs/app-for-{project_name}"

    logger.info("Deleting ECS resources")
    try:
        services = ecs.describe_services(cluster=cluster_name, services=[service_name])
        if services.get("services") and services["services"][0].get("status") != "INACTIVE":
            ecs.update_service(
                cluster=cluster_name, service=service_name, desiredCount=0
            )
            ecs.delete_service(
                cluster=cluster_name, service=service_name, force=True
            )
            logger.info(f"  ✓ Deleted ECS service: {service_name}")
            waiter = ecs.get_waiter("services_inactive")
            try:
                waiter.wait(
                    cluster=cluster_name,
                    services=[service_name],
                    WaiterConfig={"Delay": 15, "MaxAttempts": 40},
                )
            except Exception:
                pass
    except ClientError as e:
        logger.info(f"  ECS service skip: {e}")

    try:
        ecs.delete_cluster(cluster=cluster_name)
        logger.info(f"  ✓ Deleted ECS cluster: {cluster_name}")
    except ClientError as e:
        logger.info(f"  ECS cluster skip: {e}")

    try:
        task_defs = ecs.list_task_definitions(familyPrefix=task_family, sort="DESC")
        for arn in task_defs.get("taskDefinitionArns") or []:
            ecs.deregister_task_definition(taskDefinition=arn)
        logger.info(f"  ✓ Deregistered task definitions for {task_family}")
    except ClientError as e:
        logger.info(f"  Task def skip: {e}")

    try:
        tgs = elbv2.describe_target_groups(Names=[tg_name])
        for tg in tgs.get("TargetGroups") or []:
            elbv2.delete_target_group(TargetGroupArn=tg["TargetGroupArn"])
            logger.info(f"  ✓ Deleted target group: {tg_name}")
    except ClientError as e:
        logger.info(f"  Target group skip: {e}")

    try:
        ecr.delete_repository(repositoryName=repo_name, force=True)
        logger.info(f"  ✓ Deleted ECR repository: {repo_name}")
    except ClientError as e:
        logger.info(f"  ECR skip: {e}")

    try:
        logs.delete_log_group(logGroupName=log_group)
        logger.info(f"  ✓ Deleted log group: {log_group}")
    except ClientError as e:
        logger.info(f"  Log group skip: {e}")


def delete_alb_resources(project_name: str, region: str, logger) -> None:
    elbv2 = boto3.client("elbv2", region_name=region)
    alb_name = f"alb-for-{project_name}"
    if len(alb_name) > 32:
        alb_name = alb_name[:32]
    logger.info("Deleting ALB resources")
    try:
        albs = elbv2.describe_load_balancers(Names=[alb_name])
        for alb in albs.get("LoadBalancers") or []:
            alb_arn = alb["LoadBalancerArn"]
            listeners = elbv2.describe_listeners(LoadBalancerArn=alb_arn)
            for listener in listeners.get("Listeners") or []:
                elbv2.delete_listener(ListenerArn=listener["ListenerArn"])
            elbv2.delete_load_balancer(LoadBalancerArn=alb_arn)
            logger.info(f"  ✓ Deleted ALB: {alb_name}")
            time.sleep(30)
    except ClientError as e:
        logger.info(f"  ALB skip: {e}")


def delete_ui_cloudfront(project_name: str, region: str, logger) -> None:
    """Disable UI CloudFront (comment CloudFront-for-{project_name})."""
    del region  # cloudfront is global
    cf = boto3.client("cloudfront")
    comment = f"CloudFront-for-{project_name}"
    logger.info(f"Disabling UI CloudFront ({comment})")
    marker = None
    while True:
        kwargs: Dict = {}
        if marker:
            kwargs["Marker"] = marker
        resp = cf.list_distributions(**kwargs)
        listing = resp.get("DistributionList") or {}
        for item in listing.get("Items") or []:
            if item.get("Comment") != comment:
                continue
            dist_id = item["Id"]
            if not item.get("Enabled", True):
                logger.info(f"  Already disabled: {dist_id}")
                continue
            cfg_resp = cf.get_distribution_config(Id=dist_id)
            cfg = cfg_resp["DistributionConfig"]
            cfg["Enabled"] = False
            cf.update_distribution(
                Id=dist_id, IfMatch=cfg_resp["ETag"], DistributionConfig=cfg
            )
            logger.info(f"  ✓ Disabled UI CloudFront: {dist_id}")
        if not listing.get("IsTruncated"):
            break
        marker = listing.get("NextMarker")


def delete_disabled_ui_cloudfront(project_name: str, region: str, logger) -> None:
    del region
    cf = boto3.client("cloudfront")
    comment = f"CloudFront-for-{project_name}"
    logger.info(f"Deleting disabled UI CloudFront ({comment})")
    marker = None
    while True:
        kwargs: Dict = {}
        if marker:
            kwargs["Marker"] = marker
        resp = cf.list_distributions(**kwargs)
        listing = resp.get("DistributionList") or {}
        for item in listing.get("Items") or []:
            if item.get("Comment") != comment:
                continue
            if item.get("Enabled", True):
                continue
            dist_id = item["Id"]
            try:
                cfg_resp = cf.get_distribution_config(Id=dist_id)
                cf.delete_distribution(Id=dist_id, IfMatch=cfg_resp["ETag"])
                logger.info(f"  ✓ Deleted UI CloudFront: {dist_id}")
            except ClientError as e:
                logger.warning(f"  Could not delete {dist_id} yet: {e}")
        if not listing.get("IsTruncated"):
            break
        marker = listing.get("NextMarker")


def delete_alb_origin_header_secret(
    project_name: str, region: str, logger
) -> None:
    sm = boto3.client("secretsmanager", region_name=region)
    name = f"{project_name}/cloudfront-alb-origin-header"
    try:
        sm.delete_secret(SecretId=name, ForceDeleteWithoutRecovery=True)
        logger.info(f"  ✓ Deleted secret: {name}")
    except ClientError as e:
        logger.info(f"  Secret skip: {e}")


def delete_ecs_iam_roles(project_name: str, region: str, logger) -> None:
    iam = boto3.client("iam", region_name=region)
    for role_name in (
        f"role-ecs-task-for-{project_name}-{region}",
        f"role-ecs-execution-for-{project_name}-{region}",
    ):
        if len(role_name) > 64:
            role_name = role_name[:64]
        try:
            attached = iam.list_attached_role_policies(RoleName=role_name)
            for p in attached.get("AttachedPolicies") or []:
                iam.detach_role_policy(
                    RoleName=role_name, PolicyArn=p["PolicyArn"]
                )
            inlines = iam.list_role_policies(RoleName=role_name)
            for pname in inlines.get("PolicyNames") or []:
                iam.delete_role_policy(RoleName=role_name, PolicyName=pname)
            iam.delete_role(RoleName=role_name)
            logger.info(f"  ✓ Deleted IAM role: {role_name}")
        except ClientError as e:
            logger.info(f"  IAM role skip ({role_name}): {e}")
