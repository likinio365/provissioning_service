import os
import requests
import json
import logging
from time import sleep

# --- Configuration from environment variables ---
RABBITMQ_HOST = os.getenv("RABBITMQ_HOST")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")

if not RABBITMQ_HOST:
    raise RuntimeError("RABBITMQ_HOST environment variable is missing!")

if not ADMIN_USERNAME or not ADMIN_PASSWORD:
    raise RuntimeError("RabbitMQ admin credentials are missing! "
                       "Set ADMIN_USERNAME and ADMIN_PASSWORD environment variables.")

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Utility Functions for API Interaction ---

def _make_api_call(method, endpoint, json_data=None):
    """Generic function to handle RabbitMQ API calls with exponential backoff."""
    url = f"{RABBITMQ_HOST}{endpoint}"
    auth = (ADMIN_USERNAME, ADMIN_PASSWORD)

    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = requests.request(method, url, auth=auth, json=json_data, timeout=10)
            response.raise_for_status()
            return response
        except requests.exceptions.HTTPError as e:
            logger.error(
                f"HTTP Error on {method} {endpoint} (Attempt {attempt+1}): {e} - Response: {response.text}"
            )

            if response.status_code in [401, 403]:
                logger.error("Authentication check failed. Verify ADMIN_USERNAME and ADMIN_PASSWORD.")
                return None

            if attempt == max_retries - 1:
                return None

        except requests.exceptions.RequestException as e:
            logger.error(f"Connection Error on {method} {endpoint} (Attempt {attempt+1}): {e}")
            if attempt == max_retries - 1:
                return None

        sleep(2 ** attempt)

    return None


def check_developer_config_rights(requester_username, target_vhost):
    """
    Checks if the developer has config rights on the target vhost.
    """
    logger.info(f"Checking config rights for {requester_username} on vhost /{target_vhost}...")

    endpoint = f"/api/users/{requester_username}/permissions"
    response = _make_api_call("GET", endpoint)

    if response is None:
        logger.error(f"Failed to retrieve permissions for {requester_username}.")
        return False

    try:
        permissions = response.json()
    except json.JSONDecodeError:
        logger.error(f"Failed to decode permissions response for {requester_username}.")
        return False

    vhost_permission = next((p for p in permissions if p['vhost'] == target_vhost), None)

    if not vhost_permission:
        logger.warning(f"Developer {requester_username} has no permissions on vhost /{target_vhost}.")
        return False

    config_regex = vhost_permission.get('configure')

    if config_regex and config_regex.strip():
        logger.info(f"Developer {requester_username} authorized (configure regex: '{config_regex}').")
        return True
    else:
        logger.warning(f"Developer {requester_username} NOT authorized. Empty configure regex.")
        return False


def _authenticate_requester(requester_username, requester_password):
    """
    Uses /api/whoami to verify that the requester provided the correct password.
    """
    logger.info(f"Authenticating requester {requester_username}...")

    try:
        response = requests.get(
            f"{RABBITMQ_HOST}/api/whoami",
            auth=(requester_username, requester_password),
            timeout=10
        )
    except Exception as e:
        logger.error(f"AUTHENTICATION FAILED: Error contacting RabbitMQ for requester auth: {e}")
        return False

    if response.status_code in [200]:
        logger.info("Requester authentication successful.")
        return True

    logger.error(
        f"AUTHENTICATION FAILED: Invalid password for user '{requester_username}'. Status {response.status_code}"
    )
    return False


def provision_user(request_data: dict):
    """
    Main provisioning workflow.
    """
    try:
        requester_username = request_data['requester_username']
        requester_password = request_data['requester_password']  # NEW
        target_vhost = request_data['target_host']
        new_app_username = request_data['username']
        new_password = request_data['password']

        permissions_data = request_data['permissions']
        configure_regex = permissions_data['configure']
        read_regex = permissions_data['read']
        write_regex = permissions_data['write']

        new_queue_name = request_data['new_queue_name']

        user_tags = request_data.get("tags", "management")

    except KeyError as e:
        error_msg = f"MALFORMED REQUEST: Missing mandatory field: {e}"
        logger.error(error_msg)
        return False, error_msg

    logger.info(f"--- Provisioning Request for VHost: /{target_vhost} ---")
    logger.info(f"Requester: {requester_username}, New User: {new_app_username}, Queue: {new_queue_name}")

    # 0. Authenticate requester password
    if not _authenticate_requester(requester_username, requester_password):
        error_msg = f"AUTHENTICATION FAILED: Invalid password for requester '{requester_username}'."
        return False, error_msg

    # 1. Check config rights
    if not check_developer_config_rights(requester_username, target_vhost):
        error_msg = f"AUTHORIZATION FAILED: {requester_username} cannot provision vhost /{target_vhost}."
        logger.error(error_msg)
        return False, error_msg

    # 2. Create application user
    logger.info(f"Creating user {new_app_username}...")
    user_endpoint = f"/api/users/{new_app_username}"
    user_data = {"password": new_password, "tags": user_tags}

    user_response = _make_api_call("PUT", user_endpoint, user_data)

    if user_response is None:
        return False, f"Failed to create user {new_app_username}. See logs."

    logger.info(f"User {new_app_username} created or already exists.")

    # 3. Set permissions
    logger.info(f"Setting permissions for {new_app_username} on vhost /{target_vhost}...")
    perms_endpoint = f"/api/permissions/{target_vhost}/{new_app_username}"

    perms_data = {
        "configure": configure_regex,
        "write": write_regex,
        "read": read_regex
    }

    perms_response = _make_api_call("PUT", perms_endpoint, perms_data)

    if perms_response is None:
        return False, (
            f"Failed to set permissions for {new_app_username} on vhost /{target_vhost}. "
            "Cleanup may be required."
        )

    logger.info(f"Permissions applied: configure='{configure_regex}', read='{read_regex}', write='{write_regex}'.")

    return True, (
        f"SUCCESS: User {new_app_username} created with permissions on vhost /{target_vhost} "
        f"for queue '{new_queue_name}'."
    )

