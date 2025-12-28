"""
Environment detection functions for GAE/Cloud Run.

These functions detect GCP environment variables to automatically configure
Cloud Tasks settings without requiring explicit configuration.
"""

import os


def detect_gcp_project():
    """
    Detect GCP project ID from environment variables.

    Returns:
        Project ID string or None if not detected.
    """
    # GOOGLE_CLOUD_PROJECT is set in both GAE and Cloud Run
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if project:
        return project

    # GAE also provides GAE_APPLICATION
    gae_app = os.environ.get("GAE_APPLICATION")
    if gae_app:
        # Remove "s~" prefix if present
        return gae_app.lstrip("s~")

    return None


def detect_gcp_location():
    """
    Detect Cloud Tasks location (region) from environment variables.

    Returns:
        Region string or None if not detected.
    """
    # Cloud Run: CLOUD_RUN_REGION environment variable
    region = os.environ.get("CLOUD_RUN_REGION")
    if region:
        return region

    # GAE: Check for region environment variables
    region = os.environ.get("GAE_REGION") or os.environ.get("GOOGLE_CLOUD_REGION")
    if region:
        return region

    # Note: GAE may not provide region via environment variables
    # In that case, explicit configuration is required
    return None


def detect_default_service_account():
    """
    Detect the default service account email.

    Returns:
        Service account email string or None if not detected.
    """
    project = detect_gcp_project()
    if not project:
        return None

    # GAE: {project-id}@appspot.gserviceaccount.com
    gae_service = os.environ.get("GAE_SERVICE")
    if gae_service:
        return f"{project}@appspot.gserviceaccount.com"

    # Cloud Run uses Compute Engine default service account
    # which requires project number, so we return None here
    # and let the caller handle OIDC configuration explicitly or skip it
    return None


def detect_task_handler_host():
    """
    Detect the task handler host URL from GAE/Cloud Run environment variables.

    For Blue/Green deployments, this returns a version/revision-specific URL
    so that tasks are executed on the same version that enqueued them.

    Returns:
        Host URL string (e.g., "https://v1-dot-myapp.appspot.com") or None.
    """
    gcp_project = detect_gcp_project()

    # Cloud Run environment detection
    k_service = os.environ.get("K_SERVICE")
    cloud_run_region = os.environ.get("CLOUD_RUN_REGION")

    if k_service and cloud_run_region and gcp_project:
        # Cloud Run service URL
        # Format: https://{service}-{project-id}.{region}.run.app
        return f"https://{k_service}-{gcp_project}.{cloud_run_region}.run.app"

    # App Engine environment detection
    gae_service = os.environ.get("GAE_SERVICE")
    gae_version = os.environ.get("GAE_VERSION")

    if gae_service and gae_version and gcp_project:
        if gae_service == "default":
            # Default service
            # Format: https://{version}-dot-{project}.appspot.com
            return f"https://{gae_version}-dot-{gcp_project}.appspot.com"
        else:
            # Non-default service
            # Format: https://{version}-dot-{service}-dot-{project}.appspot.com
            return (
                f"https://{gae_version}-dot-{gae_service}-dot-{gcp_project}.appspot.com"
            )

    return None


def is_cloud_run():
    """Check if running on Cloud Run."""
    return os.environ.get("K_SERVICE") is not None


def is_app_engine():
    """Check if running on App Engine."""
    return os.environ.get("GAE_SERVICE") is not None
