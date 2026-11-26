from aftership_client import find_tracking_id
from tracking.tracking_service import track_shipment
from database.shipment_repository import (save_shipment, get_shipment_by_tracking_number)
from database.checkpoint_repository import save_checkpoints
from database.tracking_check_repository import (start_tracking_check, complete_tracking_check)
from database.user_shipment_repository import save_user_shipment


def track_and_save_shipment( consignment_number, user_id=None, monitoring_interval_hours=6):
    """
    Retrieve shipment tracking information and save it
    in the local database.
    """

    # Step 0: Check whether this shipment already exists
    # in our local database.

    existing_shipment = get_shipment_by_tracking_number(
        consignment_number
    )

    check_id = None

    # If the shipment exists, start recording a tracking attempt.

    if existing_shipment:

        check_id = start_tracking_check(
            existing_shipment["shipment_id"]
        )

        print(
            f"Tracking check started. Check ID: {check_id}"
        )

    
    # Step 1: Find the existing AfterShip tracking ID

    tracking_id = find_tracking_id(consignment_number)

    if not tracking_id:
        print("Unable to find an existing AfterShip tracking record.")
        if check_id:
            complete_tracking_check(
                check_id,
                check_status="failed",
                error_message="AfterShip tracking ID not found."
            )
        return None


    # Step 2: Retrieve and normalize tracking information

    tracking_data = track_shipment(
        consignment_number,
        tracking_id=tracking_id
    )

    if not tracking_data:
        print("Unable to retrieve shipment tracking information.")
        if check_id:
            complete_tracking_check(
                check_id,
                check_status="failed",
                error_message="Unable to retrieve tracking information from AfterShip."
            )
        return None


    # Step 3: Save the normalized information in the database

    shipment_id = save_shipment(
        tracking_data,
        provider_tracking_id=tracking_id
    )

    if not shipment_id:

        print("Unable to save shipment information in the database.")

        if check_id:

            complete_tracking_check(
                check_id,
                check_status="failed",
                error_message="Unable to save shipment information in the database."
            )

        return None


    # Step 3.1: Create or update the user's shipment subscription

    user_shipment_id = None

    if user_id is not None:

        user_shipment_id = save_user_shipment(
            user_id=user_id,
            shipment_id=shipment_id,
            monitoring_interval_hours=monitoring_interval_hours,
            monitoring_enabled=1
        )

        print(
            f"User shipment subscription saved. ID: {user_shipment_id}"
        )


    # If this is a new shipment, start its tracking check now.

    if not check_id:
        check_id = start_tracking_check(
            shipment_id
        )
        print(
            f"Tracking check started. Check ID: {check_id}"
        )


    # Step 4: Save shipment checkpoints

    checkpoints = tracking_data.get("checkpoints", [])

    try:

        inserted_checkpoints = save_checkpoints(
            shipment_id,
            checkpoints
        )

    except Exception:

        # Avoid exposing database details or shipment data in console output
        # and persisted tracking-check diagnostics.
        print("Unable to save shipment checkpoints due to an internal error.")

        complete_tracking_check(
            check_id,
            check_status="failed",
            error_message="Checkpoint saving failed due to an internal error."
        )

        return None


    print(
        f"New checkpoints saved: {inserted_checkpoints}"
    )


    # Step 5: Mark the tracking check as successful

    complete_tracking_check(
        check_id,
        check_status="success",
        retrieved_status=tracking_data.get("current_status")
    )

    print("Tracking check completed successfully.")


    # Step 6: Return result

    return {
        "shipment_id": shipment_id,
        "tracking_check_id": check_id,
        "user_shipment_id": user_shipment_id,
        "tracking_data": tracking_data,
        "inserted_checkpoints": inserted_checkpoints
    }