import os
import json
from google.cloud import storage
from google.oauth2 import service_account
from dotenv import load_dotenv

load_dotenv()


def get_gcs_client():
    """
    Returns a Google Cloud Storage client authenticated with service-account.json in project root or as set in env.
    """
    # Prefer SERVICE_ACCOUNT_JSON from env, else default
    sa_path = os.getenv("SERVICE_ACCOUNT_JSON", "./service-account.json")
    if not os.path.exists(sa_path):
        raise FileNotFoundError(
            f"Service account file not found at {sa_path}. "
            f"Set SERVICE_ACCOUNT_JSON env var or place service-account.json in project root."
        )
    creds = service_account.Credentials.from_service_account_file(sa_path)
    return storage.Client(credentials=creds)


def upload_json_to_gcs(json_data, filename, bucket_path):
    """
    Upload a Python dict as a JSON file to Google Cloud Storage.
    Args:
        json_data: dict to upload.
        filename: The filename (not full path).
        bucket_path: Folder/path inside the bucket; should be the Slack bot's name.
    Returns:
        Full GCS URI.
    """
    bucket_name = os.getenv("GCS_BUCKET_NAME")
    if not bucket_name:
        raise ValueError("GCS_BUCKET_NAME must be set in the .env")

    client = get_gcs_client()
    bucket = client.get_bucket(bucket_name)
    full_path = f"{bucket_path}/{filename}" if bucket_path else filename
    blob = bucket.blob(full_path)
    blob.upload_from_string(
        json.dumps(json_data, indent=2, ensure_ascii=False),
        content_type="application/json"
    )
    return f"gs://{bucket_name}/{full_path}"
