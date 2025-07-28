import vertexai
from vertexai.generative_models import GenerativeModel, Part
from google.oauth2 import service_account
import json
import re

def remove_friendly_response_field(summary_dict):
    """
    Removes the 'friendly_response_to_user' property from the dict, if present.
    Returns a new dict (shallow copy) without the field.
    """
    # Copy to avoid mutating input
    cleaned = dict(summary_dict)
    cleaned.pop("friendly_response_to_user", None)
    return cleaned

# Example usage:
# result = <your summary dict>
# summary_for_storage = remove_friendly_response_field(result)
# Now serialize summary_for_storage for storage, e.g. json.dumps(summary_for_storage)


def clean_llm_output(raw: str) -> str:
    """
    Removes Markdown code fences from the start and end of LLM output,
    including variations like ```json or ```
    """
    # Trim whitespace first
    text = raw.strip()
    # Remove leading code fence with optional 'json' or other label (case-insensitive)
    text = re.sub(r'^```[ \t]*[jJ][sS][oO][nN]?.*?\n', '', text)
    # Remove trailing code fence (common: ```
    text = re.sub(r'\n?```[\s]*$', '', text)
    return text.strip()
def get_project_id_from_service_account(sa_path: str = "./service-account.json"):
    with open(sa_path, "r") as f:
        data = json.load(f)
    # Most service account files contain "project_id" at the root
    return data["project_id"]


# gemini_tools.py

def quick_working_response(
        message: str,
        user_id: str,
        location: str = "us-central1",
        model_name: str = "gemini-2.5-flash",
):
    """
    Quickly generate a friendly message to the user confirming their request was received,
    that the agent is working, and setting expectations.

    Args:
        message (str): The user's original message.
        user_id (str): The Slack user ID (should be used in the response to @-mention).
        location (str): Vertex AI location.
        model_name (str): Gemini model name.

    Returns:
        str: A short, friendly message for the user.
    """

    sa_path = "./service-account.json"
    credentials = service_account.Credentials.from_service_account_file(sa_path)
    vertexai.init(project=get_project_id_from_service_account(sa_path), location=location, credentials=credentials)

    prompt = (
        f"You are a helpful Slack bot. The following message was just received from Slack user <@{user_id}>: '{message}'.\n"
        "Generate a single short reply for the thread, mentioning the user (e.g., <@user>), that: "
        "- Acknowledges the request has been received,\n"
        "- Assures the user the agent is now working on their request,\n"
        "- Uses a friendly, natural tone,\n"
        "- Avoids over-promising or giving an actual answer yet,\n"
        "- Never uses code fences or greetings like 'Hello!'.\n"
        "EXAMPLES:\n"
        "‘<@{user_id}> Got your request! Working on it now—hang tight while I process this.’\n"
        "‘<@{user_id}> On it! I’m analyzing your question and will reply soon.’\n"
        "\n"
        "Reply ONLY in Slack format, 1-2 sentences. Do not include explanations or apologies."
    )

    model = GenerativeModel(model_name)
    response = model.generate_content([Part.from_text(prompt)])
    reply = response.text.strip()
    # Minimal cleanup if needed
    return reply


def analyze_log_vertexai_with_json(
        log_thread: str,
        user_id: str,
        location: str = "us-central1",
        model_name: str = "gemini-2.5-flash",
        sa_path: str = "./service-account.json",  # <<--- your hardcoded relative or absolute path
):
    """
    Use a hardcoded path to service-account.json for Vertex AI Gemini analysis.
    Returns structured JSON as specified.
    """
    # 1. Load credentials from file
    credentials = service_account.Credentials.from_service_account_file(sa_path)

    # 2. Init Vertex AI with creds, project, location
    vertexai.init(project=get_project_id_from_service_account(sa_path), location=location, credentials=credentials)

    prompt = f"""
    Given the message thread log below, summarize it as this JSON structure:

    {{
      "issue_summary": "<Concise summary of key issue/goal, what agent did, and user outcome>",
      "interaction": [<full list of messages as objects like {{"from": "user"/"agent", "text": "<content>"}}>],
      "reaction": "<short emoji or reaction that marked user sentiment if present, else null>",
      "sentiment": "<positive|negative|neutral, as assessed>",
      "key_entities": [<key entities or topics, as short strings>],
      "should_have_done": "<What could the agent or workflow have done better to improve outcome; or 'no significant changes needed.'>",
      "friendly_response_to_user": "<What the agent should respond to the user in first person, thanking them for their feedback. The user's name should be addressed as `<@{user_id}>`, so that slack mentions them in your response. should be short and contextually appropriate. If the feedback is negative, it should very briefly tell the user what it thinks it might do better next time.>"
    }}

    Here is the complete message thread log (as raw JSON):

    {log_thread}

    Respond ONLY with the raw JSON object, without markdown, code fences, or any explanations. Output must be valid JSON, nothing else.
    """

    # 3. Run Gemini with prompt
    model = GenerativeModel(model_name)
    response = model.generate_content([Part.from_text(prompt)])

    import json
    try:
        json_str = clean_llm_output(response.text)
        result = json.loads(json_str)
    except Exception as e:
        print("Error parsing Gemini output:", str(e))
        print("Raw output was:", response.text)
        raise

    return result

