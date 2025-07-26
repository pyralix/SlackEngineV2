# SlackEngineV2

A Python-based Slack bot framework designed for advanced integrations with Google Cloud Storage (GCS), Gemini tools, and AgentEngine agents powered by the Agent Development Kit (ADK). This repository enables teams to orchestrate intelligent workflows in Slack, leverage AI-based agents, and collect and store user feedback securely in Google Cloud Storage.

---

## Features

- Listen to and respond to Slack messages and events.
- Integrate with Google AI Gemini tools and Google Cloud Storage.
- Proxy requests and feedback to a remote agent hosted in AgentEngine using a service account.
- Store Slack conversation feedback in a configurable GCS bucket.

---

## Setup: Running a Slack Bot

### 1. Clone and Prepare the Repository

```
git clone https://github.com/pyralix/SlackEngineV2.git
cd SlackEngineV2
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure Your Bot

- Copy `.env.example` to `.env` and fill out your tokens and environment variables.
- Copy `config.json.example` to `config.json` and adjust as needed.
- Ensure you have a valid Google Cloud service account key in `service-account.json` with access to:
  - The intended GCS bucket (for feedback)
  - The AgentEngine-hosted agent

### 3. Create and Configure your Slack App

1. Go to [Slack API: Apps](https://api.slack.com/apps) and create a new app.
2. Add a **Bot User** to your app.

#### **Add the necessary OAuth Scopes**

Based on the code’s interactions with Slack events, you’ll typically need these scopes:

| **Scope**           | **Purpose**                                                      |
|---------------------|------------------------------------------------------------------|
| `chat:write`        | Send messages as the bot                                         |
| `chat:write.public` | Send messages to channels the app isn't a member of (optional)   |
| `im:read`           | Read direct messages                                             |
| `im:history`        | Access message history in DMs                                    |
| `channels:read`     | Read info about public channels                                  |
| `groups:read`       | Read info about private channels (if needed)                     |
| `channels:history`  | Access message events in public channels                         |
| `groups:history`    | Access message events in private channels                        |
| `app_mentions:read` | Receive mention events                                           |
| `users:read`        | Read user information                                            |

> If you want the bot to join channels programmatically, also add: `channels:join`.

*Adjust the scopes as needed based on your compliance requirements and actual integration points. See the repository’s `slack_message_handler.py` and `slack_bot.py` for specifics on events handled.*

3. In **OAuth & Permissions**, add the above scopes under "Bot Token Scopes".
4. Install the app to your workspace and copy the **Bot User OAuth Token**.

### 4. Event Subscriptions

1. Enable "Event Subscriptions" in your Slack App.
2. Set the request URL to your deployed bot endpoint.
3. Subscribe to the events your bot handles (e.g., `message.channels`, `message.im`, `app_mention`).

### 5. Run the Bot

`python main.py -c config.json -p 3000`


### 6. GCS Configuration

- Ensure a GCS bucket is created for feedback.
- Grant the service account appropriate roles to read/write to this bucket.

---

## Integration with AgentEngine and Gemini

### AgentEngine Overview

[AgentEngine](https://cloud.google.com/vertex-ai/docs/agent) is a Google-powered framework for hosting and managing AI and workflow agents created using the Agent Development Kit (ADK). Deploy modular AI agents on Vertex AI Agent Engine, expose them for queries via RPC or API, and manage sessions, feedback, or workflows with built-in scalability and logging.

- *For this project, you assume you have:*
  - An ADK-based agent hosted and deployed to Agent Engine.
  - A service account (`service-account.json`) with permissions to access Agent Engine and the required GCS bucket(s).
  - A GCS bucket specified in `config.json` (or as an environment variable) for storing feedback from Slack interactions.

> You do not need to set up AgentEngine in this repository, but the service account and buckets must pre-exist and be accessible.

---

## Notes

- This project assumes you are familiar with Google Cloud IAM and Slack app administration.
- Always check your local and organizational policies before deploying bots to production Slack workspaces.

---

## Contributing

Open PRs and issues! Contributions toward expanded integrations with Gemini tools, enhanced Slack workflows, and better feedback management are welcome.

---

## License

All rights reserved

---

> _This README is a generated summary for quick onboarding. Please inspect the repository and codebase for deeper technical details._
