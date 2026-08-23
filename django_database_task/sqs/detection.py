"""
Environment detection for Amazon SQS.

AWS does not publish the application's own public URL the way App Engine
and Cloud Run do, so only the region is detected here. A push style setup
has to be given TASK_HANDLER_URL explicitly.
"""

import os


def detect_aws_region():
    """
    Detect the AWS region from the environment.

    AWS_REGION is set by Lambda and by ECS with the awslogs driver;
    AWS_DEFAULT_REGION is what the CLI and most local setups use.

    Returns:
        Region string or None if not detected.
    """
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or None


def is_lambda():
    """Whether the process runs on AWS Lambda."""
    return bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))


def is_ecs():
    """Whether the process runs on ECS or Fargate."""
    return bool(
        os.environ.get("ECS_CONTAINER_METADATA_URI_V4")
        or os.environ.get("ECS_CONTAINER_METADATA_URI")
    )
