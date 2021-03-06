# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache License Version 2.0.
# This product includes software developed at Datadog (https://www.datadoghq.com/).
# Copyright 2021 Datadog, Inc.

import logging
import os
import re
import json
import datetime
from random import randint

from collections import defaultdict
from time import time

import boto3
from botocore.exceptions import ClientError

from settings import (
    DD_S3_BUCKET_NAME,
    DD_S3_CACHE_FILENAME,
    DD_TAGS_CACHE_TTL_SECONDS,
    DD_S3_CACHE_LOCK_FILENAME,
    DD_S3_CACHE_LOCK_TTL_SECONDS,
    DD_GET_TAGS_ROLENAME,
    DD_MY_ACCOUNT,
)
from telemetry import (
    DD_FORWARDER_TELEMETRY_NAMESPACE_PREFIX,
    get_forwarder_telemetry_tags,
)

JITTER_MIN = 1
JITTER_MAX = 100

ENHANCED_METRICS_NAMESPACE_PREFIX = "aws.lambda.enhanced"
DD_TAGS_CACHE_TTL_SECONDS = DD_TAGS_CACHE_TTL_SECONDS + randint(JITTER_MIN, JITTER_MAX)

DD_S3_CACHE_LOCK_TTL_SECONDS = DD_S3_CACHE_LOCK_TTL_SECONDS + randint(
    JITTER_MIN, JITTER_MAX
)

# Latest Lambda pricing per https://aws.amazon.com/lambda/pricing/
BASE_LAMBDA_INVOCATION_PRICE = 0.0000002
LAMBDA_PRICE_PER_GB_SECOND = 0.0000166667

ESTIMATED_COST_METRIC_NAME = "estimated_cost"

GET_RESOURCES_LAMBDA_FILTER = "lambda"


# Names to use for metrics and for the named regex groups
REQUEST_ID_FIELD_NAME = "request_id"
DURATION_METRIC_NAME = "duration"
BILLED_DURATION_METRIC_NAME = "billed_duration"
MEMORY_ALLOCATED_FIELD_NAME = "memorysize"
MAX_MEMORY_USED_METRIC_NAME = "max_memory_used"
INIT_DURATION_METRIC_NAME = "init_duration"
TIMEOUTS_METRIC_NAME = "timeouts"
OUT_OF_MEMORY_METRIC_NAME = "out_of_memory"

# Create named groups for each metric and tag so that we can
# access the values from the search result by name
REPORT_LOG_REGEX = re.compile(
    r"REPORT\s+"
    + r"RequestId:\s+(?P<{}>[\w-]+)\s+".format(REQUEST_ID_FIELD_NAME)
    + r"Duration:\s+(?P<{}>[\d\.]+)\s+ms\s+".format(DURATION_METRIC_NAME)
    + r"Billed\s+Duration:\s+(?P<{}>[\d\.]+)\s+ms\s+".format(
        BILLED_DURATION_METRIC_NAME
    )
    + r"Memory\s+Size:\s+(?P<{}>\d+)\s+MB\s+".format(MEMORY_ALLOCATED_FIELD_NAME)
    + r"Max\s+Memory\s+Used:\s+(?P<{}>\d+)\s+MB".format(MAX_MEMORY_USED_METRIC_NAME)
    + r"(\s+Init\s+Duration:\s+(?P<{}>[\d\.]+)\s+ms)?".format(INIT_DURATION_METRIC_NAME)
)

TIMED_OUT_REGEX = re.compile(
    r"Task\stimed\sout\safter\s+(?P<{}>[\d\.]+)\s+seconds".format(TIMEOUTS_METRIC_NAME)
)

OUT_OF_MEMORY_ERROR_STRINGS = [
    "fatal error: runtime: out of memory",  # Go
    "java.lang.OutOfMemoryError",  # Java
    "JavaScript heap out of memory",  # Node
    "MemoryError",  # Python
    "failed to allocate memory (NoMemoryError)",  # Ruby
]

METRICS_TO_PARSE_FROM_REPORT = [
    DURATION_METRIC_NAME,
    BILLED_DURATION_METRIC_NAME,
    MAX_MEMORY_USED_METRIC_NAME,
    INIT_DURATION_METRIC_NAME,
]

# Multiply the duration metrics by 1/1000 to convert ms to seconds
METRIC_ADJUSTMENT_FACTORS = {
    DURATION_METRIC_NAME: 0.001,
    BILLED_DURATION_METRIC_NAME: 0.001,
    INIT_DURATION_METRIC_NAME: 0.001,
}

s3_client = boto3.resource("s3")
resource_tagging_client = boto3.client("resourcegroupstaggingapi")
sts_client=boto3.client('sts')

logger = logging.getLogger()

if DD_GET_TAGS_ROLENAME:
    logger.debug("Using role " + DD_GET_TAGS_ROLENAME + " for reading cross account tags")
else:
    logger.warning("No role specified for reading cross account tags. Cross account tags will not be fetched")

try:
    from datadog_lambda.metric import lambda_stats

    DD_SUBMIT_ENHANCED_METRICS = True
except ImportError:
    logger.debug(
        "Could not import from the Datadog Lambda layer so enhanced metrics won't be submitted. "
        "Add the Datadog Lambda layer to this function to submit enhanced metrics."
    )
    DD_SUBMIT_ENHANCED_METRICS = False


class LambdaTagsCache(object):
    def __init__(self, tags_ttl_seconds=DD_TAGS_CACHE_TTL_SECONDS):
        self.tags_ttl_seconds = tags_ttl_seconds

        self.tags_by_arn = {}
        self.last_tags_fetch_time = 0

    def _refresh(self, resource_arn):
        """Populate the tags in the local cache by getting cache from s3
        If cache not in s3, then cache is built using GetResources
        """
        self.last_tags_fetch_time = time()

        # If the custom tag fetch env var is not set to true do not fetch
        if not should_fetch_custom_tags():
            logger.debug(
                "Not fetching custom tags because the env variable DD_FETCH_LAMBDA_TAGS is not set to true"
            )
            return

        account_number = get_account_number(resource_arn)
        if not account_number:
            logger.debug("Not fetching custom tags because could not get account number from {}".format(resource_arn))
            return

        tags_fetched, last_modified = get_cache_from_s3(account_number)

        # s3 cache fetch succeeded
        if last_modified > -1:
            self.tags_by_arn = tags_fetched

        if self._is_expired(last_modified):
            send_forwarder_internal_metrics("s3_cache_expired")
            logger.debug(
                "S3 cache expired, building cache from Resource Groups Tagging API"
            )
            lock_acquired = acquire_s3_cache_lock(account_number)
            if lock_acquired:
                success, tags_fetched = assume_role_and_build_tags_by_arn_cache(resource_arn)
                if success:
                    self.tags_by_arn = tags_fetched
                    write_cache_to_s3(self.tags_by_arn, account_number)

                release_s3_cache_lock(account_number)

    def _is_expired(self, last_modified=None):
        """Returns bool for whether the fetch TTL has expired"""
        if not last_modified:
            last_modified = self.last_tags_fetch_time

        earliest_time_to_refetch_tags = last_modified + self.tags_ttl_seconds
        return time() > earliest_time_to_refetch_tags

    def get(self, resource_arn):
        """Get the tags for the Lambda function from the cache

        Will refetch the tags if they are out of date, or a lambda arn is encountered
        which isn't in the tag list

        Note: the ARNs in the cache have been lowercased, so resource_arn must be lowercased

        Args:
            resource_arn (str): the arn we're getting tags from the cache for

        Returns:
            lambda_tags (str[]): the list of "key:value" Datadog tag strings
        """
        if self._is_expired():
            send_forwarder_internal_metrics("local_cache_expired")
            logger.debug("Local cache expired, fetching cache from S3")
            self._refresh(resource_arn)

        function_tags = self.tags_by_arn.get(resource_arn, [])
        return function_tags


# Store the cache in the global scope so that it will be reused as long as
# the log forwarder Lambda container is running
account_lambda_tags_cache = dict()


class DatadogMetricPoint(object):
    """Holds a datapoint's data so that it can be prepared for submission to DD

    Properties:
        name (str): metric name, with namespace
        value (int | float): the datapoint's value

    """

    def __init__(self, name, value, timestamp=None, tags=[]):
        self.name = name
        self.value = value
        self.tags = tags
        self.timestamp = timestamp

    def add_tags(self, tags):
        """Add tags to this metric

        Args:
            tags (str[]): list of tags to add to this metric
        """
        self.tags = self.tags + tags

    def set_timestamp(self, timestamp):
        """Set the metric's timestamp

        Args:
            timestamp (int): Unix timestamp of this metric
        """
        self.timestamp = timestamp

    def submit_to_dd(self):
        """Submit this metric to the Datadog API"""
        timestamp = self.timestamp
        if not timestamp:
            timestamp = time()

        logger.debug(
            "Submitting metric {} {} {}".format(self.name, self.value, self.tags)
        )
        lambda_stats.distribution(
            self.name, self.value, timestamp=timestamp, tags=self.tags
        )


def should_fetch_custom_tags():
    """Checks the env var to determine if the customer has opted-in to fetching custom tags"""
    return os.environ.get("DD_FETCH_LAMBDA_TAGS", "false").lower() == "true"


_other_chars = r"\w:\-\.\/"
Sanitize = re.compile(r"[^%s]" % _other_chars, re.UNICODE).sub
Dedupe = re.compile(r"_+", re.UNICODE).sub
FixInit = re.compile(r"^[_\d]*", re.UNICODE).sub


def sanitize_aws_tag_string(tag, remove_colons=False, remove_leading_digits=True):
    """Convert characters banned from DD but allowed in AWS tags to underscores"""
    global Sanitize, Dedupe, FixInit

    # 1. Replace colons with _
    # 2. Convert to all lowercase unicode string
    # 3. Convert bad characters to underscores
    # 4. Dedupe contiguous underscores
    # 5. Remove initial underscores/digits such that the string
    #    starts with an alpha char
    #    FIXME: tag normalization incorrectly supports tags starting
    #    with a ':', but this behavior should be phased out in future
    #    as it results in unqueryable data.  See dogweb/#11193
    # 6. Strip trailing underscores

    if len(tag) == 0:
        # if tag is empty, nothing to do
        return tag

    if remove_colons:
        tag = tag.replace(":", "_")
    tag = Dedupe("_", Sanitize("_", tag.lower()))
    if remove_leading_digits:
        first_char = tag[0]
        if first_char == "_" or "0" <= first_char <= "9":
            tag = FixInit("", tag)
    tag = tag.rstrip("_")
    return tag


def get_dd_tag_string_from_aws_dict(aws_key_value_tag_dict):
    """Converts the AWS dict tag format to the dd key:value string format and truncates to 200 characters

    Args:
        aws_key_value_tag_dict (dict): the dict the GetResources endpoint returns for a tag
            ex: { "Key": "creator", "Value": "swf"}

    Returns:
        key:value colon-separated string built from the dict
            ex: "creator:swf"
    """
    key = sanitize_aws_tag_string(aws_key_value_tag_dict["Key"], remove_colons=True)
    value = sanitize_aws_tag_string(
        aws_key_value_tag_dict.get("Value"), remove_leading_digits=False
    )
    # Value is optional in DD and AWS
    if not value:
        return key
    return f"{key}:{value}"[0:200]


def parse_get_resources_response_for_tags_by_arn(get_resources_page):
    """Parses a page of GetResources response for the mapping from ARN to tags

    Args:
        get_resources_page (dict<str, dict<str, dict | str>[]>): one page of the GetResources response.
            Partial example:
                {"ResourceTagMappingList": [{
                    'ResourceARN': 'arn:aws:lambda:us-east-1:123497598159:function:my-test-lambda',
                    'Tags': [{'Key': 'stage', 'Value': 'dev'}, {'Key': 'team', 'Value': 'serverless'}]
                }]}

    Returns:
        tags_by_arn (dict<str, str[]>): Lambda tag lists keyed by ARN
    """
    tags_by_arn = defaultdict(list)

    aws_resouce_tag_mappings = get_resources_page["ResourceTagMappingList"]
    for aws_resource_tag_mapping in aws_resouce_tag_mappings:
        function_arn = aws_resource_tag_mapping["ResourceARN"]
        lowercase_function_arn = function_arn.lower()

        raw_aws_tags = aws_resource_tag_mapping["Tags"]
        tags = map(get_dd_tag_string_from_aws_dict, raw_aws_tags)

        tags_by_arn[lowercase_function_arn] += tags

    return tags_by_arn


def send_forwarder_internal_metrics(name, additional_tags=[]):
    """Send forwarder's internal metrics to DD"""
    lambda_stats.distribution(
        "{}.{}".format(DD_FORWARDER_TELEMETRY_NAMESPACE_PREFIX, name),
        1,
        tags=get_forwarder_telemetry_tags() + additional_tags,
    )


def get_last_modified_time(s3_file):
    last_modified_str = s3_file["ResponseMetadata"]["HTTPHeaders"]["last-modified"]
    last_modified_date = datetime.datetime.strptime(
        last_modified_str, "%a, %d %b %Y %H:%M:%S %Z"
    )
    last_modified_unix_time = int(last_modified_date.strftime("%s"))
    return last_modified_unix_time


def acquire_s3_cache_lock(account_number):
    """Acquire cache lock"""
    cache_lock_object = s3_client.Object(DD_S3_BUCKET_NAME, get_lock_filename(account_number))
    try:
        file_content = cache_lock_object.get()

        # check lock file expiration
        last_modified_unix_time = get_last_modified_time(file_content)
        if last_modified_unix_time + DD_S3_CACHE_LOCK_TTL_SECONDS >= time():
            return False
    except Exception:
        logger.debug("Unable to get cache lock file")

    # lock file doesn't exist, create file to acquire lock
    try:
        cache_lock_object.put(Body=(bytes("lock".encode("UTF-8"))))
        send_forwarder_internal_metrics("s3_cache_lock_acquired")
        logger.debug("S3 cache lock acquired")
    except ClientError:
        logger.debug("Unable to write S3 cache lock file", exc_info=True)
        return False

    return True

def get_lock_filename(account_number):
    return DD_S3_CACHE_LOCK_FILENAME.replace('{}', account_number)

def get_cache_filename(account_number):
    return DD_S3_CACHE_FILENAME.replace('{}', account_number)

def release_s3_cache_lock(account_number):
    """Release cache lock"""
    try:
        cache_lock_object = s3_client.Object(
            DD_S3_BUCKET_NAME, get_lock_filename(account_number)
        )
        cache_lock_object.delete()
        send_forwarder_internal_metrics("s3_cache_lock_released")
        logger.debug("S3 cache lock released")
    except ClientError:
        send_forwarder_internal_metrics("s3_cache_lock_release_failure")
        logger.debug("Unable to release S3 cache lock", exc_info=True)


def write_cache_to_s3(data, account_number):
    """Writes tags cache to s3"""
    logger.debug("Writing S3 tags cache for {}".format(account_number))
    try:
        s3_object = s3_client.Object(DD_S3_BUCKET_NAME, get_cache_filename(account_number))
        s3_object.put(Body=(bytes(json.dumps(data).encode("UTF-8"))))
    except ClientError:
        send_forwarder_internal_metrics("s3_cache_write_failure")
        logger.debug("Unable to write new cache to S3", exc_info=True)


def get_cache_from_s3(account_number):
    """Retrieves tags cache from s3 and returns the body along with
    the last modified datetime for the cache"""
    logger.debug("Fetching s3 cache for {}".format(account_number))
    cache_object = s3_client.Object(DD_S3_BUCKET_NAME, get_cache_filename(account_number))
    try:
        file_content = cache_object.get()
        tags_cache = json.loads(file_content["Body"].read().decode("utf-8"))
        last_modified_unix_time = get_last_modified_time(file_content)
    except:
        send_forwarder_internal_metrics("s3_cache_fetch_failure")
        logger.debug("Unable to fetch cache from S3", exc_info=True)
        return {}, -1

    return tags_cache, last_modified_unix_time


def build_tags_by_arn_cache():
    """Makes API calls to GetResources to get the live tags of the account's Lambda functions

    Returns an empty dict instead of fetching custom tags if the tag fetch env variable is not set to true

    Returns:
        tags_by_arn_cache (dict<str, str[]>): each Lambda's tags in a dict keyed by ARN
    """
    tags_fetch_success = False
    tags_by_arn_cache = {}

    get_resources_paginator = resource_tagging_client.get_paginator("get_resources")

    try:
        for page in get_resources_paginator.paginate(
            ResourceTypeFilters=[GET_RESOURCES_LAMBDA_FILTER], ResourcesPerPage=100
        ):
            send_forwarder_internal_metrics("get_resources_api_calls")
            page_tags_by_arn = parse_get_resources_response_for_tags_by_arn(page)
            tags_by_arn_cache.update(page_tags_by_arn)
            tags_fetch_success = True

    except ClientError as e:
        logger.exception(
            "Encountered a ClientError when trying to fetch tags. You may need to give "
            "this Lambda's role the 'tag:GetResources' permission"
        )
        additional_tags = [
            f"http_status_code:{e.response['ResponseMetadata']['HTTPStatusCode']}"
        ]
        send_forwarder_internal_metrics("client_error", additional_tags=additional_tags)

    logger.debug(
        "Built this tags cache from GetResources API calls: %s", tags_by_arn_cache
    )

    return tags_fetch_success, tags_by_arn_cache


def parse_and_submit_enhanced_metrics(logs):
    """Parses enhanced metrics from logs and submits them to DD with tags

    Args:
        logs (dict<str, str | dict | int>[]): the logs parsed from the event in the split method
            See docstring below for an example.
    """
    # If the Lambda layer is not present we can't submit enhanced metrics
    if not DD_SUBMIT_ENHANCED_METRICS:
        return

    for log in logs:
        try:
            enhanced_metrics = generate_enhanced_lambda_metrics(log, account_lambda_tags_cache)
            for enhanced_metric in enhanced_metrics:
                enhanced_metric.submit_to_dd()
        except Exception:
            logger.exception(
                "Encountered an error while trying to parse and submit enhanced metrics for log %s",
                log,
            )


def generate_enhanced_lambda_metrics(log, account_tags_cache):
    """Parses a Lambda log for enhanced Lambda metrics and tags

    Args:
        log (dict<str, str | dict | int>): a log parsed from the event in the split method
            Ex: {
                    "id": "34988208851106313984209006125707332605649155257376768001",
                    "timestamp": 1568925546641,
                    "message": "END RequestId: 2f676573-c16b-4207-993a-51fb960d73e2\\n",
                    "aws": {
                        "awslogs": {
                            "logGroup": "/aws/lambda/function_log_generator",
                            "logStream": "2019/09/19/[$LATEST]0225597e48f74a659916f0e482df5b92",
                            "owner": "172597598159"
                        },
                        "function_version": "$LATEST",
                        "invoked_function_arn": "arn:aws:lambda:us-east-1:172597598159:function:collect_logs_datadog_demo"
                    },
                    "lambda": {
                        "arn": "arn:aws:lambda:us-east-1:172597598159:function:function_log_generator"
                    },
                    "ddsourcecategory": "aws",
                    "ddtags": "env:demo,python_version:3.6,role:lambda,forwardername:collect_logs_datadog_demo,memorysize:128,forwarder_version:2.0.0,functionname:function_log_generator,env:none",
                    "ddsource": "lambda",
                    "service": "function_log_generator",
                    "host": "arn:aws:lambda:us-east-1:172597598159:function:function_log_generator"
                }
        tags_cache (LambdaTagsCache): used to apply the Lambda's custom tags to the metrics

    Returns:
        DatadogMetricPoint[], where each metric has all of its tags
    """
    # Note: this arn attribute is always lowercased when it's created
    log_function_arn = log.get("lambda", {}).get("arn")
    account_number = get_account_number(log_function_arn)
    log_message = log.get("message")
    timestamp = log.get("timestamp")

    if not account_number:
        return []

    if account_number not in account_tags_cache:
        account_tags_cache[account_number] = LambdaTagsCache()

    is_lambda_log = all((log_function_arn, log_message, timestamp))
    if not is_lambda_log:
        return []

    # Check if this is a REPORT log
    parsed_metrics = parse_metrics_from_report_log(log_message)

    # Check if this is a timeout
    if not parsed_metrics:
        parsed_metrics = create_timeout_enhanced_metric(log_message)

    # Check if this is an out of memory error
    if not parsed_metrics:
        parsed_metrics = create_out_of_memory_enhanced_metric(log_message)

    # If none of the above, move on
    if not parsed_metrics:
        return []

    # Add the tags from ARN, custom tags cache, and env var
    tags_from_arn = parse_lambda_tags_from_arn(log_function_arn)
    lambda_custom_tags = account_tags_cache[account_number].get(log_function_arn)

    for parsed_metric in parsed_metrics:
        parsed_metric.add_tags(tags_from_arn + lambda_custom_tags)
        # Submit the metric with the timestamp of the log event
        parsed_metric.set_timestamp(int(timestamp))

    return parsed_metrics


def parse_lambda_tags_from_arn(arn):
    """Generate the list of lambda tags based on the data in the arn

    Args:
        arn (str): Lambda ARN.
            ex: arn:aws:lambda:us-east-1:172597598159:function:my-lambda[:optional-version]
    """
    # Cap the number of times to split
    split_arn = arn.split(":")

    # If ARN includes version / alias at the end, drop it
    if len(split_arn) > 7:
        split_arn = split_arn[:7]

    _, _, _, region, account_id, _, function_name = split_arn

    return [
        "region:{}".format(region),
        "account_id:{}".format(account_id),
        # Include the aws_account tag to match the aws.lambda CloudWatch metrics
        "aws_account:{}".format(account_id),
        "functionname:{}".format(function_name),
    ]

def parse_metrics_from_report_log(report_log_line):
    """Parses and returns metrics from the REPORT Lambda log

    Args:
        report_log_line (str): The REPORT log generated by Lambda
        EX: "REPORT RequestId: 814ba7cb-071e-4181-9a09-fa41db5bccad	Duration: 1711.87 ms	\
            Billed Duration: 1800 ms	Memory Size: 128 MB	Max Memory Used: 98 MB	\
            XRAY TraceId: 1-5d83c0ad-b8eb33a0b1de97d804fac890	SegmentId: 31255c3b19bd3637	Sampled: true"

    Returns:
        metrics - DatadogMetricPoint[]
    """

    regex_match = REPORT_LOG_REGEX.search(report_log_line)

    if not regex_match:
        return []

    metrics = []

    tags = ["memorysize:" + regex_match.group(MEMORY_ALLOCATED_FIELD_NAME)]
    if regex_match.group(INIT_DURATION_METRIC_NAME):
        tags.append("cold_start:true")
    else:
        tags.append("cold_start:false")

    for metric_name in METRICS_TO_PARSE_FROM_REPORT:
        # check whether the metric, e.g., init duration, is present in the REPORT log
        if not regex_match.group(metric_name):
            continue

        metric_point_value = float(regex_match.group(metric_name))
        # Multiply the duration metrics by 1/1000 to convert ms to seconds
        if metric_name in METRIC_ADJUSTMENT_FACTORS:
            metric_point_value *= METRIC_ADJUSTMENT_FACTORS[metric_name]

        dd_metric = DatadogMetricPoint(
            "{}.{}".format(ENHANCED_METRICS_NAMESPACE_PREFIX, metric_name),
            metric_point_value,
        )

        dd_metric.add_tags(tags)

        metrics.append(dd_metric)

    estimated_cost_metric_point = DatadogMetricPoint(
        "{}.{}".format(ENHANCED_METRICS_NAMESPACE_PREFIX, ESTIMATED_COST_METRIC_NAME),
        calculate_estimated_cost(
            float(regex_match.group(BILLED_DURATION_METRIC_NAME)),
            float(regex_match.group(MEMORY_ALLOCATED_FIELD_NAME)),
        ),
    )

    estimated_cost_metric_point.add_tags(tags)

    metrics.append(estimated_cost_metric_point)

    return metrics


def calculate_estimated_cost(billed_duration_ms, memory_allocated):
    """Returns the estimated cost in USD of a Lambda invocation

    Args:
        billed_duration (float | int): number of milliseconds this invocation is billed for
        memory_allocated (float | int): amount of memory in MB allocated to the function execution

    See https://aws.amazon.com/lambda/pricing/ for latest pricing
    """
    # Divide milliseconds by 1000 to get seconds
    gb_seconds = (billed_duration_ms / 1000.0) * (memory_allocated / 1024.0)

    return BASE_LAMBDA_INVOCATION_PRICE + gb_seconds * LAMBDA_PRICE_PER_GB_SECOND


def get_enriched_lambda_log_tags(log_event):
    """Retrieves extra tags from lambda, either read from the function arn, or by fetching lambda tags from the function itself.

    Args:
        log (dict<str, str | dict | int>): a log parsed from the event in the split method
    """
    # Note that this arn attribute has been lowercased already
    log_function_arn = log_event.get("lambda", {}).get("arn")

    if not log_function_arn:
        return []
    tags_from_arn = parse_lambda_tags_from_arn(log_function_arn)

    account_number = get_account_number(log_function_arn)
    if not account_number:
        return []
    if account_number not in account_lambda_tags_cache:
        account_lambda_tags_cache[account_number] = LambdaTagsCache()

    lambda_custom_tags = account_lambda_tags_cache[account_number].get(log_function_arn)

    # Combine and dedup tags
    tags = list(set(tags_from_arn + lambda_custom_tags))
    return tags


def create_timeout_enhanced_metric(log_line):
    """Parses and returns a value of 1 if a timeout occurred for the function

    Args:
        log_line (str): The timed out task log
        EX: "2019-07-18T18:58:22.286Z b5264ab7-2056-4f5b-bb0f-a06a70f6205d \
             Task timed out after 30.03 seconds"

    Returns:
        DatadogMetricPoint[]
    """

    regex_match = TIMED_OUT_REGEX.search(log_line)
    if not regex_match:
        return []

    dd_metric = DatadogMetricPoint(
        f"{ENHANCED_METRICS_NAMESPACE_PREFIX}.{TIMEOUTS_METRIC_NAME}",
        1.0,
    )
    return [dd_metric]


def create_out_of_memory_enhanced_metric(log_line):
    """Parses and returns a value of 1 if an out of memory error occurred for the function

    Args:
        log_line (str): The out of memory task log

    Returns:
        DatadogMetricPoint[]
    """

    contains_out_of_memory_error = any(
        s in log_line for s in OUT_OF_MEMORY_ERROR_STRINGS
    )

    if not contains_out_of_memory_error:
        return []

    dd_metric = DatadogMetricPoint(
        f"{ENHANCED_METRICS_NAMESPACE_PREFIX}.{OUT_OF_MEMORY_METRIC_NAME}",
        1.0,
    )
    return [dd_metric]

def get_account_number(arn):
    # Extract the account number
    ## arn:aws:lambda:ap-southeast-2:295788293716:function:test-function
    if not arn:
        return None

    parts = arn.split(":")
    if len(parts) > 4:
        return parts[4]
    logger.warning("Unable to retrieve account number from {}".format(arn))
    return None

## Assume role for the account number in the passed arn
def assume_role_and_build_tags_by_arn_cache(arn):
    global DD_MY_ACCOUNT
    # It probably helps caching to keep these global??
    global resource_tagging_client
    global sts_client

    account = get_account_number(arn)

    if not account:
        return False, {}

    if not DD_GET_TAGS_ROLENAME and account != DD_MY_ACCOUNT:
        logger.debug("No cross account role specified. Not fetching tags for account " + arn)
        return False, {}

    if account == DD_MY_ACCOUNT:
        logger.debug("Using boto client")

    else:
        sts_client=boto3.client('sts')
        roleName = 'arn:aws:iam::' + account + ':role/' + DD_GET_TAGS_ROLENAME
        roleSessionName = "AssumeRoleSessionAccount-" + account

        try:
            logger.debug("Using STS to assume role for client {}".format(account))
            assumed_role_object=sts_client.assume_role(RoleArn=roleName, RoleSessionName=roleSessionName)
            credentials=assumed_role_object['Credentials']
        except ClientError as e:
            logger.error("Unable to assume role '" + roleName + "': " + str(e))
            return False, {}

        resource_tagging_client = boto3.client("resourcegroupstaggingapi"
                                               , aws_access_key_id=credentials['AccessKeyId']
                                               , aws_secret_access_key=credentials['SecretAccessKey']
                                               , aws_session_token=credentials['SessionToken'])

    return build_tags_by_arn_cache()
