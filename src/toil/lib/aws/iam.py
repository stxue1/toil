# Copyright (C) 2015-2021 Regents of the University of California
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import logging
import boto3
import fnmatch
import json

from functools import lru_cache
from typing import Optional, List, Dict, Set, cast
from mypy_boto3_iam import IAMClient
from mypy_boto3_sts import STSClient
from mypy_boto3_iam.type_defs import GetRolePolicyResponseTypeDef
from collections import defaultdict

from toil.lib.retry import retry
from toil.lib.aws.session import client, resource

try:
    from boto.exception import BotoServerError
except ImportError:
    # AWS/boto extra is not installed
    BotoServerError = None  # type: ignore


logger = logging.getLogger(__name__)

#TODO Make this comprehensive
CLUSTER_LAUNCHING_PERMISSIONS = {"iam:CreateRole",
                                  "iam:CreateInstanceProfile",
                                  "iam:TagInstanceProfile",
                                  "iam:DeleteRole",
                                  "iam:DeleteRoleProfile",
                                  "iam:ListAttatchedRolePolicies",
                                  "iam:ListPolicies",
                                  "iam:ListRoleTags",
                                  "iam:PassRole",
                                  "iam:PutRolePolicy",
                                  "iam:RemoveRoleFromInstanceProfile",
                                  "iam:TagRole",
                                  "ec2:AuthorizeSecurityGroupIngress",
                                  "ec2:CancelSpotInstanceRequests",
                                  "ec2:CreateSecurityGroup",
                                  "ec2:CreateTags",
                                  "ec2:DeleteSecurityGroup",
                                  "ec2:DescribeAvailabilityZones",
                                  "ec2:DescribeImages",
                                  "ec2:DescribeInstances",
                                  "ec2:DescribeInstanceStatus",
                                  "ec2:DescribeKeyPairs",
                                  "ec2:DescribeSecurityGroups",
                                  "ec2:DescribeSpotInstanceRequests",
                                  "ec2:DescribeSpotPriceHistory",
                                  "ec2:DescribeVolumes",
                                  "ec2:ModifyInstanceAttribute",
                                  "ec2:RequestSpotInstances",
                                  "ec2:RunInstances",
                                  "ec2:StartInstances",
                                  "ec2:StopInstances",
                                  "ec2:TerminateInstances",
                                  }

@retry(errors=[BotoServerError])
def delete_iam_role(role_name: str, region: Optional[str] = None, display_type='print') -> None:
    display = print if display_type == 'print' else logger.debug
    from boto.iam.connection import IAMConnection
    iam_client = client('iam', region_name=region)
    iam_resource = resource('iam', region_name=region)
    boto_iam_connection = IAMConnection()
    role = iam_resource.Role(role_name)
    # normal policies
    for attached_policy in role.attached_policies.all():
        display(f'Now dissociating policy: {attached_policy.name} from role {role.name}')
        role.detach_policy(PolicyName=attached_policy.name)
    # inline policies
    for attached_policy in role.policies.all():
        display(f'Deleting inline policy: {attached_policy.name} from role {role.name}')
        # couldn't find an easy way to remove inline policies with boto3; use boto
        boto_iam_connection.delete_role_policy(role.name, attached_policy.name)
    iam_client.delete_role(RoleName=role_name)
    display(f'Role {role_name} successfully deleted.')


@retry(errors=[BotoServerError])
def delete_iam_instance_profile(instance_profile_name: str, region: Optional[str] = None, display_type='print') -> None:
    display = print if display_type == 'print' else logger.debug
    iam_resource = resource('iam', region_name=region)
    instance_profile = iam_resource.InstanceProfile(instance_profile_name)
    for role in instance_profile.roles:
        display(f'Now dissociating role: {role.name} from instance profile {instance_profile_name}')
        instance_profile.remove_role(RoleName=role.name)
    instance_profile.delete()
    display(f'Instance profile "{instance_profile_name}" successfully deleted.')


def policy_permissions_allow(given_permissions: Set[str] = set(), required_permissions: Set[str] = set()) -> bool:
    """
    Check whether given set of actions are a subset of another given set of actions, returns true if they are
    otherwise false and prints a warning.

    :param required_permissions: Dictionary containing actions required, keyed by resource
    :param given_permissions: Set of actions that are granted to a user or role
    """

    # We only check actions explicitly allowed on all resources here,
    #TODO: Add a resource parameter to check for actions allowed by resource

    missing_perms = [x for x in required_permissions if not check_permission_allowed(x, list(given_permissions))]

    if missing_perms:
        for perm in missing_perms:
            logger.warning('Permission %s is missing', perm)
        return False

    return True


def check_permission_allowed(perm: str, list_perms: List[str]) -> bool:
    """
    Takes a permission and checks whether it's contained within a list of given permissions
    Returns True if it is otherwise False

    :param perm: Permission to check in string form
    :param list_perms: Permission list to check against
    """

    for allowed in list_perms:
        if fnmatch.fnmatch(perm, allowed):
            return True
    return False

def allowed_actions_roles(iam: IAMClient, policy_names: List[str], role_name: str) -> Dict[str, List[str]]:
    """
    Returns a dictionary containing a list of all aws actions allowed for a given role.
    This dictionary is keyed by resource and gives a list of policies allowed on that resource.

    :param iam: IAM client to use
    :param policy_names: Name of policy document associated with a role
    :param role_name: Name of role to get associated policies
    """
    allowed_actions: Dict[str, List[str]] = defaultdict(list)

    for policy_name in policy_names:
        role_policy = iam.get_role_policy(
            RoleName=role_name,
            PolicyName=policy_name
        )
        role_policy_document = json.loads(role_policy["PolicyDocument"])
        for statement in role_policy_document["Statement"]:

            if statement["effect"] == "Allow":

                for resource in statement["Resource"]:
                    for action in statement["Action"]:
                        allowed_actions[resource].append(action)

    return allowed_actions

def allowed_actions_users(iam: IAMClient, policy_names: List[str], user_name: str) -> Dict[str, List[str]]:
    """
    Gets all allowed actions for a user given by user_name, returns a dictionary, keyed by resource,
    with a list of permissions allowed for each given resource.

    :param iam: IAM client to use
    :param policy_names: Name of policy document associated with a user
    :param user_name: Name of user to get associated policies
    """
    allowed_actions: Dict[str, List[str]] = defaultdict(list)

    for policy_name in policy_names:
        user_policy = iam.get_user_policy(
            UserName=user_name,
            PolicyName=policy_name
        )

        user_policy_document = json.loads(user_policy["PolicyDocument"])

        #Policy document structured like so https://boto3.amazonaws.com/v1/documentation/api/latest/guide/iam-example-policies.html#example
        for statement in user_policy_document["Statement"]:
            if statement["effect"] == "Allow":
                for resource in statement["Resource"]:
                    for action in statement["Action"]:
                        allowed_actions[resource].append(action)

    return allowed_actions

def get_policy_permissions(region: str) -> Dict[str, List[str]]:
    """
    Returns a dictionary containing lists of all permission grant patterns keyed by resource
    that they are allowed upon. Requires AWS credentials to be associated with a user or assumed role.

    :param zone: AWS zone to connect to
    """

    iam: IAMClient = cast(IAMClient, client('iam', region))
    sts: STSClient = cast(STSClient, client('sts', region))
    allowed_actions: Dict[str, List[str]] = {}
    try:
        # If successful then we assume we are operating as a user, and grab the associated permissions
        user = iam.get_user()
        list_policies = iam.list_user_policies(UserName=user['User']['UserName'])
        allowed_actions = allowed_actions_users(iam, list_policies['PolicyNames'], user['User']['UserName'])

    except:
        # If not successful, we check the role associated with an instance profile
        # and grab the role's associated permissions
        role = sts.get_caller_identity()
        # Splits a role arn of format 'arn:aws:sts::123456789012:assumed-role/my-role-name/my-role-session-name'
        # on "/" and takes the second element to get the role name to list policies
        try:
            role_name = role["Arn"].split("/")[1]
            list_policies = iam.list_role_policies(RoleName=role_name)
            allowed_actions = allowed_actions_roles(iam, list_policies['PolicyNames'], role_name)
        except:
            logger.exception("Exception when trying to get role policies")

    return allowed_actions

@lru_cache()
def get_aws_account_num() -> Optional[str]:
    """
    Returns AWS account num
    """
    return boto3.client('sts').get_caller_identity().get('Account')
