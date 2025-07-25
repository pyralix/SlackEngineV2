from google.oauth2 import service_account
from google.auth.transport.requests import Request


def get_token() -> str:
    # Path to your service account JSON key
    service_account_file = 'service-account.json'

    # Scopes needed for your API (modify as needed)
    scopes = ['https://www.googleapis.com/auth/cloud-platform']

    # Load credentials from the service account file
    credentials = service_account.Credentials.from_service_account_file(
        service_account_file,
        scopes=scopes
    )

    # Create a request object for refreshing the token
    auth_request = Request()

    # Refresh the token (this ensures the token is current and valid)
    credentials.refresh(auth_request)

    # Get the access token string to use in your REST API request
    access_token = credentials.token
    return access_token