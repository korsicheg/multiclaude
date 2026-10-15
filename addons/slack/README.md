# Slack

People can also talk to their assistant from a DM with a Slack app: the same assistant and conversation as on Telegram, answering in the app they wrote from. Slash commands (`/clear`, `/model`, `/help`…) work there too, as do questions with buttons (`slack ask`), files, and voice clips, which are transcribed while the voice-notes add-in is on as well. A `/clear` from either app empties both chats, leaving the note that it cleared in each; the bot deletes a person's own Slack messages only once they have signed in with Slack, which a `/clear` on Slack offers.

Slack never talks to the machine. It sends everything to the relay (`relay/src/slack.js`), which checks Slack's signature, answers at once (a slash command fails unless answered within 3 seconds) and holds what came until the router collects it, so nothing is lost while the machine is offline. People are matched by `"slack_id"` in their `users.json` entry. Assistants answer with `slack`, the way they use `tg`.

## What it needs

- The relay, set up as in the main README ("The control panel and the relay").
- A Slack workspace where you can create apps.

## Setup

1. At https://api.slack.com/apps: **Create New App → From a manifest**, pick your workspace and paste `addons/slack/slack-manifest.json`. Its request URLs are placeholders until step 4.
2. **Install App** gives the Bot User OAuth Token, `xoxb-`. **Basic Information → App Credentials** has the Signing Secret, the Client ID and the Client Secret (the last two let people sign in so `/clear` deletes their own Slack messages too). Save them, readable only by you:

   ```sh
   (umask 077; printf '{"bot_token": "%s", "signing_secret": "%s", "client_id": "%s", "client_secret": "%s"}\n' \
      "xoxb-…" "…" "…" "…" > ~/assistant/state/slack.json)
   ```

3. `assistant addon enable slack` checks those are saved and puts `slack` on the PATH.
4. `assistant relay deploy` gives the relay the signing secret and Slack's routes, and writes `state/slack-manifest.json` with the relay's address and the bot's name. Paste that into the app's **App Manifest** page.
5. Link each person: `assistant slack-link <name> <Slack user id>`. When someone not linked writes to the bot on Slack, the owner gets their id on Telegram with the command ready.

## Files

- `lib/multiclaude/addons/slack/`: `api` (the Web API, DMs, wiping a DM, signing in), `messages` (what arrives, as the router's messages), `routing` (whose it is, taps, sign-ins, unlinked people), `chat` (where the router answers), `clips` (voice clips, for voice-notes) and the hooks in `__init__`.
- `relay/src/slack.js`: the relay's routes, mounted while this is on.
- `addons/slack/slack`: the assistants' command. `assistant-slack-link`: `assistant slack-link`. `setup` and `relay-deploy`: run by `assistant addon enable slack` and `assistant relay deploy`. `slack-manifest.json`: the Slack app's manifest, with placeholders.
- State: `state/slack.json` (the app's tokens), `state/slack-people.json` (the user tokens of those who signed in) and `state/slack-dms.json` (DM ids), all readable only by the owner's user.
