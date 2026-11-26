
from aftership_client import find_tracking_id, get_tracking

def normalize_aftership_response(response):
    """
    Convert an AfterShip tracking response into our standard
    internal tracking data structure.
    """

    # Step 1: Validate the input response
    if not isinstance(response, dict):
        return None

    # Step 2: Extract the tracking data
    tracking_data = response.get("data", {})

    if not isinstance(tracking_data, dict):
        return None

    if not tracking_data:
        return None

    # Step 3: Extract checkpoint information
    aftership_checkpoints = tracking_data.get("checkpoints") or []

    if not isinstance(aftership_checkpoints, list):
        return None

    normalized_checkpoints = []

    for checkpoint in aftership_checkpoints:

        if not isinstance(checkpoint, dict):
            continue

        normalized_checkpoint = {
            "date": checkpoint.get("checkpoint_time"),
            "location": checkpoint.get("location"),
            "status": checkpoint.get("tag"),
            "message": checkpoint.get("message")
        }

        normalized_checkpoints.append(normalized_checkpoint)

    # Step 4: Construct our normalized shipment structure
    normalized_tracking = {

        "tracking_number": tracking_data.get("tracking_number"),

        "carrier": tracking_data.get("slug"),

        "current_status": tracking_data.get("tag"),

        "status_message": tracking_data.get("subtag_message"),

        "origin": tracking_data.get("origin_raw_location"),

        "destination": tracking_data.get("destination_raw_location"),

        "pickup_date": tracking_data.get("shipment_pickup_date"),

        "delivery_date": tracking_data.get("shipment_delivery_date"),

        "checkpoints": normalized_checkpoints
    }

    # Step 5: Return normalized tracking information
    return normalized_tracking


# unified track_shipment() function to connect the two existing functions:
    #1. find_tracking_id(consignment_number) – finds the AfterShip tracking ID.
    #2. get_tracking(tracking_id) – retrieves the full tracking information.

def track_shipment(consignment_number, tracking_id=None):
    """
    Retrieve and normalize shipment tracking information
    using the consignment number.
    """

    # Step 1: Find the corresponding AfterShip tracking ID
    # only if one has not already been provided.

    if not tracking_id:
        tracking_id = find_tracking_id(consignment_number)

    if not tracking_id:
        print("Unable to find an existing tracking record.")
        return None

    # Step 2: Retrieve complete tracking information
    response = get_tracking(tracking_id)

    if not response:
        print("Unable to retrieve tracking information.")
        return None

    # Step 3: Normalize the AfterShip response
    normalized_data = normalize_aftership_response(response)

    if not normalized_data:
        print("Unable to normalize tracking information.")
        return None

    # Step 4: Return standardized tracking information
    return normalized_data