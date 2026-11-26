
from tracking.shipment_workflow import track_and_save_shipment


def track_india_post(consignment_number, user_id=None):
    """
    Track an India Post consignment and save its shipment and checkpoints.

    Args:
        consignment_number (str): India Post consignment number.
        user_id (int, optional): Trusted internal database user ID.
            Must be supplied by the application, never by the AI model.

    Returns:
        dict: Tracking result using the existing success/data structure.
    """

    # Validate the consignment number.
    if not isinstance(consignment_number, str):
        return {
            "success": False,
            "error": "Consignment number must be provided as text."
        }

    consignment_number = consignment_number.strip().upper()

    if not consignment_number:
        return {
            "success": False,
            "error": "Consignment number is required."
        }

    if len(consignment_number) != 13 or not consignment_number.isalnum():
        return {
            "success": False,
            "error": (
                "Invalid consignment number. Please provide a valid "
                "13-character India Post consignment number."
            )
        }

    # Validate optional trusted application identity.
    if user_id is not None:
        if (
            not isinstance(user_id, int)
            or isinstance(user_id, bool)
            or user_id <= 0
        ):
            return {
                "success": False,
                "error": "Invalid application user context."
            }

    try:
        # Preserve the existing guest call interface.
        if user_id is None:
            workflow_result = track_and_save_shipment(
                consignment_number
            )
        else:
            # Registered-user identity comes from trusted application code.
            workflow_result = track_and_save_shipment(
                consignment_number,
                user_id=user_id
            )

        if not workflow_result:
            return {
                "success": False,
                "error": (
                    "Unable to retrieve or save shipment tracking "
                    "information."
                )
            }

        tracking_data = workflow_result.get("tracking_data")

        if not tracking_data:
            return {
                "success": False,
                "error": "Tracking information was not available."
            }

        # Do not expose internal database IDs to the AI model.
        return {
            "success": True,
            "data": tracking_data
        }

    except Exception:
        return {
            "success": False,
            "error": (
                "An unexpected error occurred while tracking "
                "the shipment."
            )
        }
