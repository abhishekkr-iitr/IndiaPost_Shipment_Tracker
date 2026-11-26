
import os
from dotenv import load_dotenv


def run_test():
    # Load configuration from the project's .env file.
    load_dotenv(override=False)

    api_key = os.getenv("AFTERSHIP_API_KEY")
    base_url = os.getenv("AFTERSHIP_BASE_URL")

    # Validate required configuration.
    assert api_key, "AfterShip API key is missing!"
    assert base_url, "AfterShip Base URL is missing!"

    print("AfterShip configuration loaded successfully!")
    print("API Key:", "Configured")
    print("Base URL:", "Configured")
    print("\nCONFIGURATION TEST: PASSED")


if __name__ == "__main__":
    run_test()
