"""VPC + Amazon S3 Files provisioning for AgentCore Harness (agentic-work pattern)."""

from __future__ import annotations

import json
import time
from typing import Dict, List, Optional

from botocore.exceptions import ClientError

SESSION_STORAGE_MOUNT_PATH = "/mnt/workspace"
APP_DATA_MOUNT_PATH = "/mnt/app-data"
S3_FILES_SESSION_PREFIX = "agentcore-sessions/"
S3_FILES_APP_DATA_PREFIX = "app-data/"
VPC_CIDR = "10.52.0.0/16"


class S3FilesVpcProvisioner:
    def __init__(
        self,
        *,
        ec2_client,
        s3_client,
        s3files_client,
        iam_client,
        region: str,
        account_id: str,
        project_name: str,
        logger,
    ):
        self.ec2 = ec2_client
        self.s3 = s3_client
        self.s3files = s3files_client
        self.iam = iam_client
        self.region = region
        self.account_id = account_id
        self.project_name = project_name
        self.logger = logger

    def vpc_name(self) -> str:
        return f"vpc-for-{self.project_name}"

    # --- VPC -----------------------------------------------------------------

    def ensure_vpc(self) -> Dict[str, object]:
        """Create or reuse a project VPC with public + private subnets and NAT."""
        self.logger.info(f"Ensuring VPC for Harness: {self.vpc_name()}")
        existing = self._find_vpc_by_name(self.vpc_name())
        if existing:
            vpc_id = existing
            self.logger.info(f"  Reusing VPC: {vpc_id}")
            self._enable_vpc_dns(vpc_id)
            public_subnets, private_subnets = self._classify_subnets(vpc_id)
            if len(private_subnets) < 1:
                raise RuntimeError(
                    f"VPC {vpc_id} has no private subnets; "
                    "create private subnets or delete the VPC and re-run installer."
                )
            return {
                "vpc_id": vpc_id,
                "public_subnets": public_subnets,
                "private_subnets": private_subnets,
            }
        return self._create_vpc()

    def _find_vpc_by_name(self, name: str) -> Optional[str]:
        resp = self.ec2.describe_vpcs(
            Filters=[{"Name": "tag:Name", "Values": [name]}]
        )
        vpcs = resp.get("Vpcs") or []
        return vpcs[0]["VpcId"] if vpcs else None

    def _enable_vpc_dns(self, vpc_id: str) -> None:
        self.ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsSupport={"Value": True})
        self.ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={"Value": True})

    def _classify_subnets(self, vpc_id: str) -> tuple[List[str], List[str]]:
        subnets = self.ec2.describe_subnets(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
        ).get("Subnets", [])
        public_subnets: List[str] = []
        private_subnets: List[str] = []
        for subnet in subnets:
            if subnet.get("State") != "available":
                continue
            name = ""
            for tag in subnet.get("Tags") or []:
                if tag["Key"] == "Name":
                    name = tag["Value"]
                    break
            sid = subnet["SubnetId"]
            if "private" in name.lower():
                private_subnets.append(sid)
            elif "public" in name.lower():
                public_subnets.append(sid)
            else:
                if self._subnet_is_public(sid):
                    public_subnets.append(sid)
                else:
                    private_subnets.append(sid)
        return public_subnets, private_subnets

    def _subnet_is_public(self, subnet_id: str) -> bool:
        rts = self.ec2.describe_route_tables(
            Filters=[{"Name": "association.subnet-id", "Values": [subnet_id]}]
        ).get("RouteTables", [])
        if not rts:
            # Check main route table for VPC
            subnet = self.ec2.describe_subnets(SubnetIds=[subnet_id])["Subnets"][0]
            vpc_id = subnet["VpcId"]
            rts = self.ec2.describe_route_tables(
                Filters=[
                    {"Name": "vpc-id", "Values": [vpc_id]},
                    {"Name": "association.main", "Values": ["true"]},
                ]
            ).get("RouteTables", [])
        for rt in rts:
            for route in rt.get("Routes", []):
                if str(route.get("GatewayId", "")).startswith("igw-"):
                    return True
        return False

    def _create_vpc(self) -> Dict[str, object]:
        azs = [
            z["ZoneName"]
            for z in self.ec2.describe_availability_zones(
                Filters=[{"Name": "state", "Values": ["available"]}]
            )["AvailabilityZones"]
        ][:2]
        if len(azs) < 2:
            raise RuntimeError("Need at least 2 availability zones for Harness VPC")

        vpc_id = self.ec2.create_vpc(
            CidrBlock=VPC_CIDR,
            TagSpecifications=[
                {
                    "ResourceType": "vpc",
                    "Tags": [{"Key": "Name", "Value": self.vpc_name()}],
                }
            ],
        )["Vpc"]["VpcId"]
        self.ec2.get_waiter("vpc_available").wait(VpcIds=[vpc_id])
        self._enable_vpc_dns(vpc_id)
        self.logger.info(f"  Created VPC: {vpc_id}")

        igw_id = self.ec2.create_internet_gateway(
            TagSpecifications=[
                {
                    "ResourceType": "internet-gateway",
                    "Tags": [
                        {"Key": "Name", "Value": f"igw-for-{self.project_name}"}
                    ],
                }
            ]
        )["InternetGateway"]["InternetGatewayId"]
        self.ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)

        public_rt = self.ec2.create_route_table(
            VpcId=vpc_id,
            TagSpecifications=[
                {
                    "ResourceType": "route-table",
                    "Tags": [
                        {"Key": "Name", "Value": f"public-rt-for-{self.project_name}"}
                    ],
                }
            ],
        )["RouteTable"]["RouteTableId"]
        self.ec2.create_route(
            RouteTableId=public_rt,
            DestinationCidrBlock="0.0.0.0/0",
            GatewayId=igw_id,
        )

        public_subnets: List[str] = []
        private_subnets: List[str] = []
        for i, az in enumerate(azs):
            pub = self.ec2.create_subnet(
                VpcId=vpc_id,
                CidrBlock=f"10.52.{i}.0/24",
                AvailabilityZone=az,
                TagSpecifications=[
                    {
                        "ResourceType": "subnet",
                        "Tags": [
                            {
                                "Key": "Name",
                                "Value": f"public-{i}-for-{self.project_name}",
                            }
                        ],
                    }
                ],
            )["Subnet"]["SubnetId"]
            self.ec2.modify_subnet_attribute(
                SubnetId=pub, MapPublicIpOnLaunch={"Value": True}
            )
            self.ec2.associate_route_table(SubnetId=pub, RouteTableId=public_rt)
            public_subnets.append(pub)

            priv = self.ec2.create_subnet(
                VpcId=vpc_id,
                CidrBlock=f"10.52.{10 + i}.0/24",
                AvailabilityZone=az,
                TagSpecifications=[
                    {
                        "ResourceType": "subnet",
                        "Tags": [
                            {
                                "Key": "Name",
                                "Value": f"private-{i}-for-{self.project_name}",
                            }
                        ],
                    }
                ],
            )["Subnet"]["SubnetId"]
            private_subnets.append(priv)

        # One NAT for outbound from private subnets (MCP / Bedrock APIs).
        eip = self.ec2.allocate_address(Domain="vpc")["AllocationId"]
        nat_id = self.ec2.create_nat_gateway(
            SubnetId=public_subnets[0],
            AllocationId=eip,
            TagSpecifications=[
                {
                    "ResourceType": "natgateway",
                    "Tags": [
                        {"Key": "Name", "Value": f"nat-for-{self.project_name}"}
                    ],
                }
            ],
        )["NatGateway"]["NatGatewayId"]
        self.logger.info(f"  Waiting for NAT Gateway: {nat_id}")
        self.ec2.get_waiter("nat_gateway_available").wait(NatGatewayIds=[nat_id])

        private_rt = self.ec2.create_route_table(
            VpcId=vpc_id,
            TagSpecifications=[
                {
                    "ResourceType": "route-table",
                    "Tags": [
                        {
                            "Key": "Name",
                            "Value": f"private-rt-for-{self.project_name}",
                        }
                    ],
                }
            ],
        )["RouteTable"]["RouteTableId"]
        self.ec2.create_route(
            RouteTableId=private_rt,
            DestinationCidrBlock="0.0.0.0/0",
            NatGatewayId=nat_id,
        )
        for subnet_id in private_subnets:
            self.ec2.associate_route_table(SubnetId=subnet_id, RouteTableId=private_rt)

        self.logger.info(
            f"✓ VPC ready: {vpc_id} "
            f"(public={public_subnets}, private={private_subnets})"
        )
        return {
            "vpc_id": vpc_id,
            "public_subnets": public_subnets,
            "private_subnets": private_subnets,
        }

    # --- Security groups -----------------------------------------------------

    def create_security_group(
        self,
        vpc_id: str,
        group_name: str,
        description: str,
        ingress_rules: Optional[List[Dict]] = None,
    ) -> str:
        try:
            sg_id = self.ec2.create_security_group(
                GroupName=group_name,
                Description=description,
                VpcId=vpc_id,
                TagSpecifications=[
                    {
                        "ResourceType": "security-group",
                        "Tags": [{"Key": "Name", "Value": group_name}],
                    }
                ],
            )["GroupId"]
            if ingress_rules:
                try:
                    self.ec2.authorize_security_group_ingress(
                        GroupId=sg_id, IpPermissions=ingress_rules
                    )
                except ClientError as e:
                    if e.response["Error"]["Code"] != "InvalidPermission.Duplicate":
                        self.logger.warning(f"  Could not add SG ingress: {e}")
            return sg_id
        except ClientError as e:
            if e.response["Error"]["Code"] != "InvalidGroup.Duplicate":
                raise
            sgs = self.ec2.describe_security_groups(
                Filters=[
                    {"Name": "group-name", "Values": [group_name]},
                    {"Name": "vpc-id", "Values": [vpc_id]},
                ]
            )
            return sgs["SecurityGroups"][0]["GroupId"]

    def _ensure_nfs_access(self, client_sg_id: str, mount_sg_id: str) -> None:
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
                self.logger.warning(f"  NFS egress failed: {e}")

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
                self.logger.warning(f"  NFS ingress failed: {e}")

    # --- S3 Files ------------------------------------------------------------

    def _wait_status(
        self,
        describe_fn,
        id_key: str,
        resource_id: str,
        ready: str = "available",
        timeout: int = 600,
    ) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            resp = describe_fn(**{id_key: resource_id})
            status = (resp.get("status") or "").lower()
            if status == ready.lower():
                return
            if status in {"error", "deleted"}:
                raise RuntimeError(
                    f"S3 Files {resource_id} status={status}: {resp.get('statusMessage')}"
                )
            time.sleep(10)
        raise TimeoutError(f"Timed out waiting for S3 Files resource {resource_id}")

    def _ensure_bucket_versioning(self, bucket: str) -> None:
        status = self.s3.get_bucket_versioning(Bucket=bucket).get("Status")
        if status == "Enabled":
            return
        self.logger.info(f"  Enabling S3 versioning for S3 Files: {bucket}")
        self.s3.put_bucket_versioning(
            Bucket=bucket,
            VersioningConfiguration={"Status": "Enabled"},
        )

    def _get_or_create_sync_role(self, s3_bucket_arn: str) -> str:
        role_name = f"role-s3files-sync-for-{self.project_name}"
        if len(role_name) > 64:
            role_name = role_name[:64]
        trust = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "AllowS3FilesAssumeRole",
                    "Effect": "Allow",
                    "Principal": {"Service": "elasticfilesystem.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                    "Condition": {
                        "StringEquals": {"aws:SourceAccount": self.account_id},
                        "ArnLike": {
                            "aws:SourceArn": (
                                f"arn:aws:s3files:{self.region}:{self.account_id}:file-system/*"
                            )
                        },
                    },
                }
            ],
        }
        bucket_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": [
                        "s3:ListBucket",
                        "s3:ListBucketVersions",
                        "s3:GetBucketLocation",
                        "s3:GetBucketVersioning",
                        "s3:AbortMultipartUpload",
                        "s3:ListMultipartUploadParts",
                        "s3:GetObject",
                        "s3:GetObjectVersion",
                        "s3:GetObjectTagging",
                        "s3:GetObjectVersionTagging",
                        "s3:PutObject",
                        "s3:PutObjectTagging",
                        "s3:DeleteObject",
                        "s3:DeleteObjectVersion",
                    ],
                    "Resource": [s3_bucket_arn, f"{s3_bucket_arn}/*"],
                    "Condition": {
                        "StringEquals": {"aws:ResourceAccount": self.account_id}
                    },
                }
            ],
        }
        eventbridge_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "EventBridgeManage",
                    "Effect": "Allow",
                    "Action": [
                        "events:PutRule",
                        "events:PutTargets",
                        "events:DeleteRule",
                        "events:DisableRule",
                        "events:EnableRule",
                        "events:RemoveTargets",
                    ],
                    "Resource": "arn:aws:events:*:*:rule/DO-NOT-DELETE-S3-Files*",
                    "Condition": {
                        "StringEquals": {
                            "events:ManagedBy": "elasticfilesystem.amazonaws.com"
                        }
                    },
                },
                {
                    "Sid": "EventBridgeRead",
                    "Effect": "Allow",
                    "Action": [
                        "events:DescribeRule",
                        "events:ListRules",
                        "events:ListRuleNamesByTarget",
                        "events:ListTargetsByRule",
                    ],
                    "Resource": "arn:aws:events:*:*:rule/*",
                },
            ],
        }

        created = False
        try:
            role_arn = self.iam.get_role(RoleName=role_name)["Role"]["Arn"]
            self.iam.update_assume_role_policy(
                RoleName=role_name, PolicyDocument=json.dumps(trust)
            )
            self.logger.info(f"  Reusing S3 Files sync role: {role_arn}")
        except self.iam.exceptions.NoSuchEntityException:
            role_arn = self.iam.create_role(
                RoleName=role_name,
                AssumeRolePolicyDocument=json.dumps(trust),
                Description=f"S3 Files sync role for {self.project_name}",
            )["Role"]["Arn"]
            created = True
            self.logger.info(f"  Created S3 Files sync role: {role_arn}")

        for pname, doc in (
            ("s3-bucket-access", bucket_policy),
            ("eventbridge-sync", eventbridge_policy),
        ):
            self.iam.put_role_policy(
                RoleName=role_name,
                PolicyName=pname,
                PolicyDocument=json.dumps(doc),
            )
        if created:
            self.logger.info("  Waiting 15s for sync role IAM propagation")
            time.sleep(15)
        return role_arn

    def _normalize_prefix(self, prefix: str) -> str:
        if not prefix:
            return ""
        return prefix if prefix.endswith("/") else f"{prefix}/"

    def _file_system_name_tag(self, item: Dict) -> str:
        for tag in item.get("tags") or []:
            if tag.get("key") == "Name" or tag.get("Key") == "Name":
                return tag.get("value") or tag.get("Value") or ""
        return item.get("name") or ""

    def _find_file_system(
        self, s3_bucket_arn: str, prefix: str, name_tag: str
    ) -> Optional[Dict[str, str]]:
        want_prefix = self._normalize_prefix(prefix)
        paginator = self.s3files.get_paginator("list_file_systems")
        for page in paginator.paginate():
            for item in page.get("fileSystems", []):
                if item.get("bucket") != s3_bucket_arn:
                    continue
                item_prefix = self._normalize_prefix(item.get("prefix") or "")
                item_name = self._file_system_name_tag(item)
                if item_prefix == want_prefix or item_name == name_tag:
                    return {
                        "file_system_id": item.get("fileSystemId", ""),
                        "file_system_arn": item.get("fileSystemArn", ""),
                        "prefix": item_prefix,
                    }
        return None

    def _get_or_create_file_system(
        self,
        s3_bucket_arn: str,
        role_arn: str,
        *,
        prefix: str,
        name_tag: str,
    ) -> Dict[str, str]:
        existing = self._find_file_system(s3_bucket_arn, prefix, name_tag)
        if existing and existing.get("file_system_id"):
            self.logger.info(
                f"  Reusing S3 Files file system: {existing['file_system_id']} "
                f"(prefix={existing.get('prefix') or prefix})"
            )
            return existing

        bucket = s3_bucket_arn.removeprefix("arn:aws:s3:::")
        self._ensure_bucket_versioning(bucket)
        resp = self.s3files.create_file_system(
            bucket=s3_bucket_arn,
            prefix=self._normalize_prefix(prefix),
            roleArn=role_arn,
            acceptBucketWarning=True,
            tags=[{"key": "Name", "value": name_tag}],
        )
        fs_id = resp["fileSystemId"]
        self.logger.info(
            f"  Created S3 Files file system: {fs_id} (prefix={prefix})"
        )
        self._wait_status(self.s3files.get_file_system, "fileSystemId", fs_id)
        return {
            "file_system_id": fs_id,
            "file_system_arn": resp.get("fileSystemArn", ""),
            "prefix": self._normalize_prefix(prefix),
        }

    def _ensure_mount_targets(
        self,
        file_system_id: str,
        subnet_ids: List[str],
        security_group_ids: List[str],
    ) -> None:
        existing = set()
        paginator = self.s3files.get_paginator("list_mount_targets")
        for page in paginator.paginate(fileSystemId=file_system_id):
            for item in page.get("mountTargets", []):
                if item.get("subnetId"):
                    existing.add(item["subnetId"])

        for subnet_id in subnet_ids:
            if subnet_id in existing:
                self.logger.info(f"  Reusing mount target in {subnet_id}")
                continue
            resp = self.s3files.create_mount_target(
                fileSystemId=file_system_id,
                subnetId=subnet_id,
                securityGroups=security_group_ids,
            )
            mt_id = resp.get("mountTargetId", subnet_id)
            self.logger.info(f"  Created mount target {mt_id} in {subnet_id}")
            self._wait_status(self.s3files.get_mount_target, "mountTargetId", mt_id)

    def _access_point_name_tag(self, item: Dict) -> str:
        for tag in item.get("tags") or []:
            if tag.get("key") == "Name" or tag.get("Key") == "Name":
                return tag.get("value") or tag.get("Value") or ""
        return item.get("name") or ""

    def _get_or_create_access_point(
        self, file_system_id: str, *, name_tag: str
    ) -> str:
        aps: List[Dict] = []
        paginator = self.s3files.get_paginator("list_access_points")
        for page in paginator.paginate(fileSystemId=file_system_id):
            aps.extend(page.get("accessPoints") or [])

        for item in aps:
            arn = item.get("accessPointArn")
            if not arn:
                continue
            if self._access_point_name_tag(item) == name_tag:
                self.logger.info(
                    f"  Reusing S3 Files access point: {arn} (name={name_tag})"
                )
                return arn

        # Fallback: single-AP FS created before name tagging — reuse only when
        # there is exactly one AP and the requested tag is the session default.
        if len(aps) == 1 and name_tag == f"s3files-ap-for-{self.project_name}":
            arn = aps[0].get("accessPointArn")
            if arn:
                self.logger.info(f"  Reusing legacy S3 Files access point: {arn}")
                return arn

        resp = self.s3files.create_access_point(
            fileSystemId=file_system_id,
            posixUser={"uid": 0, "gid": 0},
            rootDirectory={
                "path": "/",
                "creationPermissions": {
                    "ownerUid": 0,
                    "ownerGid": 0,
                    "permissions": "0777",
                },
            },
            tags=[{"key": "Name", "value": name_tag}],
        )
        arn = resp["accessPointArn"]
        self.logger.info(f"  Created S3 Files access point: {arn} (name={name_tag})")
        self._wait_status(
            self.s3files.get_access_point, "accessPointId", resp["accessPointId"]
        )
        return arn

    def _put_file_system_policy(
        self,
        file_system_id: str,
        access_point_arn: str,
        client_role_arns: List[str],
    ) -> None:
        principals = [a for a in client_role_arns if a]
        if not principals:
            return
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
                            "s3files:AccessPointArn": access_point_arn,
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
            self.logger.info("  Applied S3 Files file system policy")
        except ClientError as e:
            self.logger.warning(f"  Could not apply S3 Files FS policy: {e}")

    def attach_client_policy_to_role(
        self,
        role_name: str,
        file_system_id: str,
        access_point_arn: str,
    ) -> None:
        fs_arn = (
            f"arn:aws:s3files:{self.region}:{self.account_id}:file-system/{file_system_id}"
        )
        policy = {
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
                        "ArnEquals": {
                            "s3files:AccessPointArn": access_point_arn,
                        }
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
        policy_name = f"s3files-harness-policy-for-{self.project_name}"[:128]
        policy_existed = False
        try:
            self.iam.get_role_policy(RoleName=role_name, PolicyName=policy_name)
            policy_existed = True
        except self.iam.exceptions.NoSuchEntityException:
            pass

        self.iam.put_role_policy(
            RoleName=role_name,
            PolicyName=policy_name,
            PolicyDocument=json.dumps(policy),
        )
        self.logger.info(f"  ✓ Attached S3 Files client policy to {role_name}")
        # CreateHarness validates s3files:GetAccessPoint immediately; wait only
        # when the inline policy is brand-new (existing roles are already propagated).
        if policy_existed:
            self.logger.info(
                "  Skipping IAM wait (S3 Files client policy already on role)"
            )
            return
        wait_seconds = 20
        self.logger.info(
            f"  Waiting {wait_seconds}s for execution-role IAM propagation "
            f"(s3files:GetAccessPoint)"
        )
        time.sleep(wait_seconds)

    def create_s3_files_session_storage(
        self,
        vpc_info: Dict[str, object],
        s3_bucket_name: str,
        harness_execution_role_arn: str,
        harness_execution_role_name: str,
    ) -> Dict[str, object]:
        """Provision S3 Files and return harness VPC/mount settings."""
        self.logger.info("Creating S3 Files session storage for Harness")
        vpc_id = str(vpc_info["vpc_id"])
        private_subnets = list(vpc_info.get("private_subnets") or [])
        if not private_subnets:
            raise RuntimeError("At least one private subnet is required for S3 Files")

        s3_bucket_arn = f"arn:aws:s3:::{s3_bucket_name}"
        sync_role_arn = self._get_or_create_sync_role(s3_bucket_arn)
        file_system = self._get_or_create_file_system(
            s3_bucket_arn,
            sync_role_arn,
            prefix=S3_FILES_SESSION_PREFIX,
            name_tag=f"s3files-for-{self.project_name}",
        )
        file_system_id = file_system["file_system_id"]

        harness_sg_id = self.create_security_group(
            vpc_id=vpc_id,
            group_name=f"harness-runtime-sg-for-{self.project_name}",
            description=f"Security group for AgentCore Harness ({self.project_name})",
        )
        # Allow all egress so MCP / Bedrock work from private subnets via NAT.
        try:
            self.ec2.authorize_security_group_egress(
                GroupId=harness_sg_id,
                IpPermissions=[
                    {
                        "IpProtocol": "-1",
                        "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                    }
                ],
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "InvalidPermission.Duplicate":
                self.logger.debug(f"  harness SG egress: {e}")

        mount_sg_id = self.create_security_group(
            vpc_id=vpc_id,
            group_name=f"s3files-mount-sg-for-{self.project_name}",
            description=f"S3 Files mount SG for {self.project_name}",
            ingress_rules=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 2049,
                    "ToPort": 2049,
                    "UserIdGroupPairs": [{"GroupId": harness_sg_id}],
                }
            ],
        )
        self._ensure_nfs_access(harness_sg_id, mount_sg_id)
        self._ensure_mount_targets(file_system_id, private_subnets, [mount_sg_id])
        access_point_arn = self._get_or_create_access_point(
            file_system_id,
            name_tag=f"s3files-ap-for-{self.project_name}",
        )
        self._put_file_system_policy(
            file_system_id,
            access_point_arn,
            [harness_execution_role_arn],
        )
        self.attach_client_policy_to_role(
            harness_execution_role_name,
            file_system_id,
            access_point_arn,
        )

        self.logger.info("✓ S3 Files session storage ready")
        self.logger.info(f"  File system: {file_system_id}")
        self.logger.info(f"  Access point: {access_point_arn}")
        self.logger.info(f"  Mount path: {SESSION_STORAGE_MOUNT_PATH}")
        self.logger.info(f"  Prefix: {S3_FILES_SESSION_PREFIX}")
        self.logger.info(f"  Subnets: {', '.join(private_subnets)}")
        self.logger.info(f"  Security group: {harness_sg_id}")

        return {
            "file_system_id": file_system_id,
            "file_system_arn": file_system.get("file_system_arn", ""),
            "access_point_arn": access_point_arn,
            "mount_path": SESSION_STORAGE_MOUNT_PATH,
            "prefix": S3_FILES_SESSION_PREFIX,
            "vpc_id": vpc_id,
            "subnets": private_subnets,
            "security_groups": [harness_sg_id],
            "mount_sg_id": mount_sg_id,
            "harness_sg_id": harness_sg_id,
        }

    def _copy_s3_prefix_if_missing(
        self, bucket: str, src_prefix: str, dst_prefix: str
    ) -> int:
        """Copy src→dst objects when destination prefix is empty. Returns count."""
        dst_probe = self.s3.list_objects_v2(
            Bucket=bucket, Prefix=dst_prefix, MaxKeys=1
        )
        if dst_probe.get("KeyCount", 0) > 0:
            return 0
        copied = 0
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=src_prefix):
            for obj in page.get("Contents") or []:
                src_key = obj.get("Key") or ""
                if not src_key.startswith(src_prefix):
                    continue
                dst_key = dst_prefix + src_key[len(src_prefix) :]
                self.s3.copy_object(
                    Bucket=bucket,
                    Key=dst_key,
                    CopySource={"Bucket": bucket, "Key": src_key},
                )
                copied += 1
        return copied

    def _migrate_app_data_from_sessions(self, s3_bucket_name: str) -> None:
        """Move ECS-owned data off agentcore-sessions into app-data (once).

        Migrates ``application-database/``, per-user ``graph/``, and
        ``settings.json``. Skills/artifacts stay under agentcore-sessions.
        """
        try:
            n = self._copy_s3_prefix_if_missing(
                s3_bucket_name,
                f"{S3_FILES_SESSION_PREFIX}application-database/",
                f"{S3_FILES_APP_DATA_PREFIX}application-database/",
            )
            if n:
                self.logger.info(
                    f"  Migrated {n} application-database object(s) → app-data/"
                )

            paginator = self.s3.get_paginator("list_objects_v2")
            users: List[str] = []
            for page in paginator.paginate(
                Bucket=s3_bucket_name,
                Prefix=S3_FILES_SESSION_PREFIX,
                Delimiter="/",
            ):
                for entry in page.get("CommonPrefixes") or []:
                    child = (entry.get("Prefix") or "").rstrip("/")
                    name = child.rsplit("/", 1)[-1] if child else ""
                    if name and name != "application-database":
                        users.append(name)

            graph_copied = 0
            settings_copied = 0
            for user in users:
                graph_copied += self._copy_s3_prefix_if_missing(
                    s3_bucket_name,
                    f"{S3_FILES_SESSION_PREFIX}{user}/graph/",
                    f"{S3_FILES_APP_DATA_PREFIX}{user}/graph/",
                )
                src_settings = f"{S3_FILES_SESSION_PREFIX}{user}/settings.json"
                dst_settings = f"{S3_FILES_APP_DATA_PREFIX}{user}/settings.json"
                try:
                    self.s3.head_object(Bucket=s3_bucket_name, Key=dst_settings)
                except ClientError:
                    try:
                        self.s3.head_object(Bucket=s3_bucket_name, Key=src_settings)
                    except ClientError:
                        continue
                    self.s3.copy_object(
                        Bucket=s3_bucket_name,
                        Key=dst_settings,
                        CopySource={
                            "Bucket": s3_bucket_name,
                            "Key": src_settings,
                        },
                    )
                    settings_copied += 1

            if graph_copied:
                self.logger.info(
                    f"  Migrated {graph_copied} graph object(s) → app-data/"
                )
            if settings_copied:
                self.logger.info(
                    f"  Migrated {settings_copied} settings.json → app-data/"
                )
        except ClientError as e:
            self.logger.warning(f"  app-data migration skipped: {e}")

    def create_s3_files_app_data_storage(
        self,
        vpc_info: Dict[str, object],
        s3_bucket_name: str,
        *,
        mount_sg_id: Optional[str] = None,
    ) -> Dict[str, object]:
        """Provision a separate S3 Files FS for ECS (tasks.db / graph / settings).

        Uses ``app-data/`` prefix so Harness session storage
        (``agentcore-sessions/``) cannot see the web app database.
        """
        self.logger.info("Creating S3 Files app-data storage for ECS")
        vpc_id = str(vpc_info["vpc_id"])
        private_subnets = list(vpc_info.get("private_subnets") or [])
        if not private_subnets:
            raise RuntimeError("At least one private subnet is required for S3 Files")

        s3_bucket_arn = f"arn:aws:s3:::{s3_bucket_name}"
        sync_role_arn = self._get_or_create_sync_role(s3_bucket_arn)
        file_system = self._get_or_create_file_system(
            s3_bucket_arn,
            sync_role_arn,
            prefix=S3_FILES_APP_DATA_PREFIX,
            name_tag=f"s3files-app-data-for-{self.project_name}",
        )
        file_system_id = file_system["file_system_id"]

        if not mount_sg_id:
            mount_sg_id = self.create_security_group(
                vpc_id=vpc_id,
                group_name=f"s3files-mount-sg-for-{self.project_name}",
                description=f"S3 Files mount SG for {self.project_name}",
            )

        self._ensure_mount_targets(
            file_system_id, private_subnets, [str(mount_sg_id)]
        )
        access_point_arn = self._get_or_create_access_point(
            file_system_id,
            name_tag=f"s3files-ap-app-data-for-{self.project_name}",
        )
        self._migrate_app_data_from_sessions(s3_bucket_name)

        self.logger.info("✓ S3 Files app-data storage ready")
        self.logger.info(f"  File system: {file_system_id}")
        self.logger.info(f"  Access point: {access_point_arn}")
        self.logger.info(f"  Mount path: {APP_DATA_MOUNT_PATH}")
        self.logger.info(f"  Prefix: {S3_FILES_APP_DATA_PREFIX}")

        return {
            "file_system_id": file_system_id,
            "file_system_arn": file_system.get("file_system_arn", ""),
            "access_point_arn": access_point_arn,
            "mount_path": APP_DATA_MOUNT_PATH,
            "prefix": S3_FILES_APP_DATA_PREFIX,
            "vpc_id": vpc_id,
            "subnets": private_subnets,
            "mount_sg_id": mount_sg_id,
        }


def build_harness_runtime_environment(
    s3_files_info: Optional[Dict[str, object]],
) -> Dict:
    """Build CreateHarness/UpdateHarness environment with VPC + S3 Files mount."""
    lifecycle = {
        "idleRuntimeSessionTimeout": 600,
        "maxLifetime": 14400,
    }
    if not s3_files_info or not s3_files_info.get("access_point_arn"):
        return {
            "agentCoreRuntimeEnvironment": {
                "lifecycleConfiguration": lifecycle,
                "networkConfiguration": {"networkMode": "PUBLIC"},
            }
        }

    return {
        "agentCoreRuntimeEnvironment": {
            "lifecycleConfiguration": lifecycle,
            "networkConfiguration": {
                "networkMode": "VPC",
                "networkModeConfig": {
                    "subnets": list(s3_files_info.get("subnets") or []),
                    "securityGroups": list(s3_files_info.get("security_groups") or []),
                },
            },
            "filesystemConfigurations": [
                {
                    "s3FilesAccessPoint": {
                        "accessPointArn": s3_files_info["access_point_arn"],
                        "mountPath": s3_files_info.get(
                            "mount_path", SESSION_STORAGE_MOUNT_PATH
                        ),
                    }
                }
            ],
        }
    }
