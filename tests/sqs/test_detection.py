"""Tests for AWS environment detection."""

from django_database_task.sqs import detect_aws_region, is_ecs, is_lambda


class TestDetectAwsRegion:
    def test_reads_aws_region(self, monkeypatch):
        monkeypatch.setenv("AWS_REGION", "ap-northeast-1")

        assert detect_aws_region() == "ap-northeast-1"

    def test_falls_back_to_aws_default_region(self, monkeypatch):
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")

        assert detect_aws_region() == "us-east-1"

    def test_aws_region_wins(self, monkeypatch):
        monkeypatch.setenv("AWS_REGION", "ap-northeast-1")
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")

        assert detect_aws_region() == "ap-northeast-1"

    def test_returns_none_when_undetectable(self, monkeypatch):
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)

        assert detect_aws_region() is None


class TestPlatformDetection:
    def test_detects_lambda(self, monkeypatch):
        monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "my-function")

        assert is_lambda() is True

    def test_not_lambda_without_the_variable(self, monkeypatch):
        monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)

        assert is_lambda() is False

    def test_detects_ecs(self, monkeypatch):
        monkeypatch.setenv("ECS_CONTAINER_METADATA_URI_V4", "http://169.254.170.2/v4")

        assert is_ecs() is True

    def test_not_ecs_without_the_variable(self, monkeypatch):
        monkeypatch.delenv("ECS_CONTAINER_METADATA_URI_V4", raising=False)
        monkeypatch.delenv("ECS_CONTAINER_METADATA_URI", raising=False)

        assert is_ecs() is False
