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

def json_to_jsonl(json_str):
    """
    Convert JSON array or wrapped-list dict to a JSONL string.
    Each object will be on its own line in the output.
    """
    data = json.loads(json_str)

    # If the root is a list, treat each element as a record
    if isinstance(data, list):
        lines = [json.dumps(obj, ensure_ascii=False) for obj in data]
        return '\n'.join(lines)
    # If the root is a dict with a single list value (e.g. {"records": [...]})
    elif isinstance(data, dict):
        # Look for a single key that is a list
        array_keys = [k for k, v in data.items() if isinstance(v, list)]
        if len(array_keys) == 1:
            records = data[array_keys[0]]
            lines = [json.dumps(obj, ensure_ascii=False) for obj in records]
            return '\n'.join(lines)
        else:
            # Fallback: treat the entire dict as a single JSONL line
            return json.dumps(data, ensure_ascii=False)
    else:
        raise ValueError("JSON root must be array or object.")


def upload_json_to_gcs(json_data, filename, bucket_path):
    """
    Upload a Python dict or list as a JSONL file to Google Cloud Storage.
    Args:
        json_data: dict or list to upload (input to JSONL converter).
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

    # Convert to JSONL string
    jsonl_str = json_to_jsonl(json_data)

    blob = bucket.blob(full_path)
    blob.upload_from_string(
        jsonl_str,
        content_type="application/json"
    )
    return f"gs://{bucket_name}/{full_path}"

