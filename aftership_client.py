import os
from urllib.parse import urlsplit

import httpx
from dotenv import load_dotenv

# Load optional local configuration. Importing this module must not require
# provider credentials: application components and offline tests may import
# provider adapters without making an AfterShip request.
load_dotenv()


def _get_aftership_config():
    """Return validated request configuration, or None when not configured."""
    api_key = os.getenv("AFTERSHIP_API_KEY", "").strip()
    base_url = os.getenv("AFTERSHIP_BASE_URL", "").strip().rstrip("/")

    if not api_key or not base_url:
        print(
            "AfterShip is not configured. Set AFTERSHIP_API_KEY and "
            "AFTERSHIP_BASE_URL before requesting live tracking data."
        )
        return None

    # Only accept an HTTPS URL with a hostname. Query strings, fragments and
    # embedded credentials are unsuitable for the configured API base URL.
    try:
        parsed_url = urlsplit(base_url)
        valid_url = (
            parsed_url.scheme.lower() == "https"
            and bool(parsed_url.hostname)
            and parsed_url.username is None
            and parsed_url.password is None
            and not parsed_url.query
            and not parsed_url.fragment
            and not any(character.isspace() for character in base_url)
        )
        # Accessing .port validates numeric syntax and the TCP port range.
        port = parsed_url.port
        if port is not None and not (1 <= port <= 65535):
            valid_url = False
    except ValueError:
        valid_url = False

    if not valid_url:
        print(
            "AfterShip configuration is invalid. "
            "AFTERSHIP_BASE_URL must be a valid HTTPS URL."
        )
        return None

    return api_key, base_url


def get_tracking(tracking_id):

    config = _get_aftership_config()
    if config is None:
        return None
    api_key, base_url = config

    # Construct API endpoint
    url = f"{base_url}/trackings/{tracking_id}"

    # Configure authentication headers
    headers = {
        "as-api-key": api_key,
        "Content-Type": "application/json"
    }

    try:
        # Send GET request
        response = httpx.get(
            url,
            headers=headers,
            timeout=30.0
        )

        # Check for HTTP errors
        response.raise_for_status()

        # Convert response into JSON
        data = response.json()

        return data

    except httpx.HTTPStatusError as error:
        # Provider response bodies can contain shipment or account data.
        print("AfterShip HTTP request failed. Status code:", error.response.status_code)

    except httpx.RequestError:
        # Exception text may expose request details or URLs.
        print("AfterShip connection request failed.")

    except ValueError:
        print("Unable to decode API response as JSON.")

    return None

def create_tracking(consignment_number):

    config = _get_aftership_config()
    if config is None:
        return None
    api_key, base_url = config

    # Construct API endpoint
    url = f"{base_url}/trackings"

    # Configure authentication headers
    headers = {
        "as-api-key": api_key,
        "Content-Type": "application/json"
    }

    # Prepare request body
    payload = {
        "tracking_number": consignment_number,
        "slug": "india-post"
    }

    try:
        # Send POST request
        response = httpx.post(
            url,
            headers=headers,
            json=payload,
            timeout=30.0
        )

        # Check for HTTP errors
        response.raise_for_status()

        # Convert response into JSON
        data = response.json()

        return data

    except httpx.HTTPStatusError as error:
        # Provider response bodies can contain shipment or account data.
        print("AfterShip HTTP request failed. Status code:", error.response.status_code)

    except httpx.RequestError:
        # Exception text may expose request details or URLs.
        print("AfterShip connection request failed.")

    except ValueError:
        print("Unable to decode API response as JSON.")

    return None
    
def find_tracking_id(consignment_number):

    config = _get_aftership_config()
    if config is None:
        return None
    api_key, base_url = config

    # Construct API endpoint
    url = f"{base_url}/trackings"

    # Configure authentication headers
    headers = {
        "as-api-key": api_key,
        "Content-Type": "application/json"
    }

    # Prepare search parameters
    params = {
        "tracking_numbers": consignment_number,
        "slug": "india-post"
    }

    try:

        # Send GET request
        response = httpx.get(
            url,
            headers=headers,
            params=params,
            timeout=30.0
        )

        # Check for HTTP errors
        response.raise_for_status()

        # Convert response into JSON
        data = response.json()

        # Extract tracking records
        tracking_records = data.get("data", {}).get("trackings", [])

        # Search for the exact consignment number
        for tracking in tracking_records:

            if tracking.get("tracking_number") == consignment_number:

                return tracking.get("id")

        print("No existing tracking record found.")

    except httpx.HTTPStatusError as error:
        # Provider response bodies can contain shipment or account data.
        print("AfterShip HTTP request failed. Status code:", error.response.status_code)

    except httpx.RequestError:
        # Exception text may expose request details or URLs.
        print("AfterShip connection request failed.")

    except ValueError:
        print("Unable to decode API response as JSON.")

    return None


# Test the function
if __name__ == "__main__":

    tracking_id = "2e7678143b4447238cbc6513f4caeda5"

    result = get_tracking(tracking_id)

    if result:
        print("\nTracking information retrieved successfully!\n")

        tracking_data = result.get("data", {})

        print("Consignment Number:", tracking_data.get("tracking_number"))
        print("Carrier:", tracking_data.get("slug"))
        print("Current Status:", tracking_data.get("tag"))
        print("Destination:", tracking_data.get("destination_raw_location"))
        print("Delivery Date:", tracking_data.get("shipment_delivery_date"))

        checkpoints = tracking_data.get("checkpoints", [])

        print("Total Checkpoints:", len(checkpoints))

    else:
        print("Unable to retrieve tracking information.")