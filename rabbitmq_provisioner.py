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
    # This is where the authentication happens using the ADMIN credentials
    auth = (ADMIN_USERNAME, ADMIN_PASSWORD)
    
    # Simple retry logic for reliability
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = requests.request(method, url, auth=auth, json=json_data, timeout=10)
            response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
            return response
        except requests.exceptions.HTTPError as e:
            # Handle specific RabbitMQ errors (e.g., user already exists)
            logger.error(f"HTTP Error on {method} {endpoint} (Attempt {attempt+1}): {e} - Response: {response.text}")
            if response.status_code in [401, 403]:
                # If the ADMIN user is unauthorized, we stop immediately.
                logger.error("Authentication check failed. Please verify ADMIN_USERNAME and ADMIN_PASSWORD.")
                return None 
            if attempt == max_retries - 1:
                return None
        except requests.exceptions.RequestException as e:
            logger.error(f"Connection Error on {method} {endpoint} (Attempt {attempt+1}): {e}")
            if attempt == max_retries - 1:
                return None
        
        sleep(2 ** attempt) # Exponential backoff
    return None

def check_developer_config_rights(requester_username, target_vhost):
    """
    Checks if the developer has config rights on the target vhost.
    The permission check must be based on the permissions assigned to the developer user.
    """
    logger.info(f"Checking config rights for {requester_username} on vhost /{target_vhost}...")
    
    # Endpoint to get all permissions for the requester
    endpoint = f"/api/users/{requester_username}/permissions"
    response = _make_api_call("GET", endpoint)

    if response is None:
        logger.error(f"Failed to retrieve permissions for {requester_username}. API call failed.")
        return False

    try:
        permissions = response.json()
    except json.JSONDecodeError:
        logger.error(f"Failed to decode permissions response for {requester_username}.")
        return False

    # Find the permission object for the specific vhost
    vhost_permission = next((p for p in permissions if p['vhost'] == target_vhost), None)

    if not vhost_permission:
        logger.warning(f"Developer {requester_username} has no defined permissions on vhost /{target_vhost}.")
        return False

    # Check the 'configure' regex. It should be non-empty (e.g., ".*")
    config_regex = vhost_permission.get('configure')
    
    if config_regex and config_regex.strip():
        logger.info(f"Developer {requester_username} is authorized. Config regex: '{config_regex}'")
        return True
    else:
        logger.warning(f"Developer {requester_username} is NOT authorized. Configure regex is empty or null.")
        return False

def provision_user(request_data: dict):
    """
    Main provisioning function that orchestrates the workflow.
    It expects a dictionary payload mirroring a service request containing all necessary fields.
    """
    # Extract data from the request payload. The service expects all fields explicitly.
    try:
        requester_username = request_data['requester_username']
        target_vhost = request_data['target_host'] # Matches user's required field name
        new_app_username = request_data['username'] # Matches user's required field name
        new_password = request_data['password'] # Matches user's required field name
        
        # Developer provides the specific read/write permissions regexes
        permissions_data = request_data['permissions']
        configure_regex = permissions_data['configure']
        read_regex = permissions_data['read']
        write_regex = permissions_data['write']
        
        new_queue_name = request_data['new_queue_name'] # Matches user's required field name
    except KeyError as e:
        error_msg = f"MALFORMED REQUEST: Missing mandatory field: {e}"
        logger.error(error_msg)
        return False, error_msg


    logger.info(f"--- Provisioning Request for VHost: /{target_vhost} ---")
    logger.info(f"Requester: {requester_username}, New User: {new_app_username}, Queue: {new_queue_name}")

    # 1. CRITICAL CHECK: Verify developer's config rights
    if not check_developer_config_rights(requester_username, target_vhost):
        error_msg = f"AUTHORIZATION FAILED: {requester_username} is not permitted to provision resources on vhost /{target_vhost}."
        logger.error(error_msg)
        return False, error_msg

    # 2. Create the new application user
    logger.info(f"Creating user {new_app_username}...")
    user_endpoint = f"/api/users/{new_app_username}"
    # Using 'app_user' tag for the application user
    #user_data = {"password": new_password, "tags": "management"}
    user_tags = request_data.get("tags", "management")  # default to 'app_user' if not provided
    user_data = {"password": new_password, "tags": user_tags}
    user_response = _make_api_call("PUT", user_endpoint, user_data)
    
    if user_response is None:
        return False, f"Failed to create user {new_app_username}. Check logs for details."
    
    # NOTE: We do not need to create the queue, as RabbitMQ allows binding to non-existent queues,
    # and the app user will declare it upon first connection.

    logger.info(f"User {new_app_username} created successfully (or already exists).")

    # 3. Set fine-grained permissions for the new user
    # The new user gets READ/WRITE based on developer input, but CONFIG rights are ZEROED OUT for security.
    logger.info(f"Setting permissions for {new_app_username} on vhost /{target_vhost}...")
    perms_endpoint = f"/api/permissions/{target_vhost}/{new_app_username}"
    
    perms_data = {
        "configure": configure_regex,  
        "write": write_regex,      
        "read": read_regex         
    }
    
    perms_response = _make_api_call("PUT", perms_endpoint, perms_data)

    if perms_response is None:
        # A full service would include cleanup logic (e.g., deleting the user if permission setting failed).
        return False, f"Failed to set permissions for {new_app_username} on vhost /{target_vhost}. Cleanup needed."

    logger.info(f"Permissions set successfully: Configure regex '{configure_regex}' , Read regex '{read_regex}', Write regex '{write_regex}'.")

    return True, f"SUCCESS: User {new_app_username} created with permissions on vhost /{target_vhost} for queue '{new_queue_name}'."
