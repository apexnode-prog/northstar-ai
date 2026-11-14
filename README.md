# Northstar

A small, local AI chat workspace using the OpenRouter chat completions API.

## Run

From this folder, start a static server:

```sh
python3 -m http.server 8080
```

Open <http://localhost:8080>. Choose the gear icon to enter an OpenRouter API key and model ID (for example, `anthropic/claude-3.7-sonnet`). Start a new conversation and send a prompt. Responses stream into the conversation; chats and settings persist in this browser's local storage.

The API key is stored in browser local storage on this device and sent to OpenRouter from the browser. Use this only on a trusted machine and browser profile. The app does not include a backend or sync data between devices.

## Included

- Recent conversation navigation and new-chat action
- OpenRouter model and API key settings
- Streaming chat completions
- Local conversation history
- Prompt suggestions and responsive layout

The original Electron app's investigation notes are in the task summary; this folder contains its extracted `app.asar` under `work/claude-asar` for local review.
