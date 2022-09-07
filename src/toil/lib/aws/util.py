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
import json
import logging
import os
import re

from urllib.request import urlopen
from urllib.error import URLError

from typing import Any, Callable, Dict, Iterable, List, Optional, TypeVar, Union
from toil.lib.aws.ec2 import running_on_ec2
from toil.lib.aws.ecs import running_on_ecs

logger = logging.getLogger(__name__)

CLOUD_KEY_REGEX = re.compile(
    "^"
    "(?P<schema>(?:s3|gs|wasb))"
    "://"
    "(?P<bucket>[^/]+)"
    "/"
    "(?P<key>.+)"
    "$")
AWS_ZONE_REGEX = re.compile(r'^([a-z]{2}-[a-z]+-[1-9][0-9]*)([a-z])$')


def get_aws_zone_from_metadata() -> Optional[str]:
    """
    Get the AWS zone from instance metadata, if on EC2 and the boto module is
    available. Otherwise, gets the AWS zone from ECS task metadata, if on ECS.
    """

    # When running on ECS, we also appear to be running on EC2, but the EC2
    # metadata service doesn't seem to be contactable. So we check ECS first.

    if running_on_ecs():
        # Use the ECS metadata service
        logger.debug("Fetch AZ from ECS metadata")
        try:
            resp = json.load(urlopen(os.environ['ECS_CONTAINER_METADATA_URI_V4'] + '/task', timeout=1))
            logger.debug("ECS metadata: %s", resp)
            if isinstance(resp, dict):
                # We found something. Go with that.
                return resp.get('AvailabilityZone')
        except (json.decoder.JSONDecodeError, KeyError, URLError) as e:
            # We're on ECS but can't get the metadata. That's odd.
            logger.warning("Skipping ECS metadata due to error: %s", e)
    if running_on_ec2():
        # On EC2 alone, or on ECS but we couldn't get ahold of the ECS
        # metadata.
        try:
            # Use the EC2 metadata service
            import boto
            from boto.utils import get_instance_metadata
            logger.debug("Fetch AZ from EC2 metadata")
            return get_instance_metadata()['placement']['availability-zone']
        except ImportError:
            # This is expected to happen a lot
            logger.debug("No boto to fetch ECS metadata")
        except (KeyError, URLError) as e:
            # We're on EC2 but can't get the metadata. That's odd.
            logger.warning("Skipping EC2 metadata due to error: %s", e)
    return None


def get_current_aws_region() -> Optional[str]:
    """
    Return the AWS region that the currently configured AWS zone (see
    get_current_aws_zone()) is in.
    """
    # Try to derive it from the zone.
    aws_zone = get_current_aws_zone()
    return zone_to_region(aws_zone) if aws_zone else None


def get_aws_zone_from_environment() -> Optional[str]:
    """
    Get the AWS zone from TOIL_AWS_ZONE if set.
    """
    return os.environ.get('TOIL_AWS_ZONE', None)


def get_aws_zone_from_boto() -> Optional[str]:
    """
    Get the AWS zone from the Boto config file, if it is configured and the
    boto module is available.
    """
    try:
        import boto
        zone = boto.config.get('Boto', 'ec2_region_name')
        if zone is not None:
            zone += 'a'  # derive an availability zone in the region
        return zone
    except ImportError:
        pass
    return None


def get_aws_zone_from_environment_region() -> Optional[str]:
    """
    Pick an AWS zone in the region defined by TOIL_AWS_REGION, if it is set.
    """
    aws_region = os.environ.get('TOIL_AWS_REGION')
    if aws_region is not None:
        # If a region is specified, use the first zone in the region.
        return aws_region + 'a'
    # Otherwise, don't pick a region and let us fall back on the next method.
    return None


def get_current_aws_zone() -> Optional[str]:
    """
    Get the currently configured or occupied AWS zone to use.

    Reports the TOIL_AWS_ZONE environment variable if set.

    Otherwise, if we have boto and are running on EC2, or if we are on ECS,
    reports the zone we are running in.

    Otherwise, if we have the TOIL_AWS_REGION variable set, chooses a zone in
    that region.

    Finally, if we have boto2, and a default region is configured in Boto 2,
    chooses a zone in that region.

    Returns None if no method can produce a zone to use.
    """
    return get_aws_zone_from_environment() or \
        get_aws_zone_from_metadata() or \
        get_aws_zone_from_environment_region() or \
        get_aws_zone_from_boto()


def zone_to_region(zone: str) -> str:
    """Get a region (e.g. us-west-2) from a zone (e.g. us-west-1c)."""
    # re.compile() caches the regex internally so we don't have to
    availability_zone_regex = re.compile(r'^([a-z]{2}-[a-z]+-[1-9][0-9]*)([a-z])$')
    availability_zone = availability_zone_regex.match(zone)
    if not availability_zone:
        raise ValueError(f"Can't extract region from availability zone '{zone}'")
    return availability_zone.group(1)