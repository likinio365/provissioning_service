from flask import Flask, request, jsonify
import json
import logging
import os
# Assuming rabbitmq_provisioner.py is in the same directory
from rabbitmq_provisioner import provision_user 

# --- Service Configuration ---
# Use environment variables for sensitive data in a real deployment
# Fallback to defaults (which must be updated in rabbitmq_provisioner.py)
HOST = os.getenv("SERVICE_HOST", "0.0.0.0")
PORT = os.getenv("SERVICE_PORT", 8080)

# Initialize Flask app
app = Flask(__name__)
# Set up logging for the service
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

@app.route('/provision', methods=['POST'])
def handle_provisioning_request():
    """
    Handles POST requests from developers to provision a new RabbitMQ user and permissions.
    The request body must be JSON and match the required input structure.
    """
    if not request.is_json:
        return jsonify({"status": "Failed", "message": "Request must be JSON"}), 400

    request_data = request.get_json()
    logger.info(f"Received provisioning request: {json.dumps(request_data)}")

    # Call the core provisioning logic from the imported module
    success, message = provision_user(request_data)

    if success:
        logger.info(f"Provisioning SUCCESS: {message}")
        return jsonify({"status": "Success", "message": message}), 201
    else:
        logger.error(f"Provisioning FAILED: {message}")
        
        # Determine appropriate HTTP status code based on the failure reason
        if "AUTHORIZATION FAILED" in message:
            # 403 Forbidden: Developer is unauthorized to act on that vhost
            status_code = 403
        elif "MALFORMED REQUEST" in message:
            # 400 Bad Request: Missing mandatory fields
            status_code = 400
        else:
            # 500 Internal Server Error: General API or internal failure
            status_code = 500
            
        return jsonify({"status": "Failed", "message": message}), status_code

if __name__ == '__main__':
    logger.info(f"Starting RabbitMQ Provisioning Service on {HOST}:{PORT}")
    try:
        # Running the Flask app starts the continuous service loop
        app.run(host=HOST, port=PORT, debug=False)
    except Exception as e:
        logger.critical(f"Failed to start service: {e}")
