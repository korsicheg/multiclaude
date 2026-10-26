# multiclaude

One Telegram bot that gives each person their own Claude Code assistant, all on one Linux machine.

Each person gets their own Claude Code session, folder (`~/workspaces/<name>`), `CLAUDE.md`, memory and chat history. They talk to it in a Telegram DM. It works on the machine as a full admin: it runs commands, edits files, schedules cron jobs, and replies with text, files and buttons. A control panel in Telegram shows each person their usage and lets them change their settings with a tap. Add-ins, off by default, bring more: Slack, Siri, voice notes and others.

![How multiclaude works: a Telegram message reaches the front desk, a Claude Code session that hosts the Telegram plugin; the router reads it there and types it into the sender's own Claude Code assistant, which replies with tg through Telegram's Bot API. The control panel and the optional Slack, Siri, locations (OwnTracks) and reminders add-ins reach the router through the relay, a Cloudflare Worker. When a browser needs the person, an assistant runs takeover, which shows them that browser live through a Cloudflare quick tunnel until they tap Done.](assets/architecture.svg)

## How it fits together

- **Front desk:** a Claude Code session (`assistant.service`, tmux session `assistant`) that only hosts the official Telegram plugin, patched to pass on button taps, replies, reactions and formatted messages, and so that no other Claude Code session can take its Telegram connection. It answers nothing.
- **Router:** `bin/watcher.py` (`assistant-watcher.service`, code in `lib/multiclaude/router/`) reads each inbound Telegram message from the front desk's transcript and types it into the sender's own session. It keeps "typing…" on while that session works, and handles `/clear`, `/restart`, `/stop`, `/usage`, `/model`, `/effort`, `/plugin`, `/skills`, `/mcp`, `/reload-plugins`, `/help` and `/config` itself.
- **Assistants:** one tmux session per person (`u-<name>`), started by `bin/run-user.sh`, in that person's folder. Each replies through `bin/tg`, which calls the Telegram Bot API.
- **Scheduled jobs:** cron lines the assistants write, each running `assistant job`: a one-shot Claude in a person's folder whose answer is sent to them.
- **Shared plugins and skills:** each person installs their own; `assistant shared sync` moves what everyone has into `~/.claude`, so there's one copy to keep up to date.
- **Machine health:** every 5 minutes, `assistant health sample` checks CPU, memory, swap, disks, the router and each person's assistant. The owner gets a Telegram message when something runs high or stops, and again when it's fine, and sees the machine in the control panel.
- **Browser take-overs:** when an assistant's browser hits something only its person can do (a CAPTCHA, a payment confirmation), `bin/takeover` sends them a private link to that browser, live, through a Cloudflare quick tunnel. They work in it on their phone or computer, tap Done, and the assistant carries on.
- **Relay and control panel** (optional): a Cloudflare Worker in `relay/` that serves the control panel, a Telegram Mini App, and holds its taps until the router collects them. Add-ins that bring other apps (Slack, Siri, OwnTracks, a Reminders shortcut) come in through it too.

The details, and the reasons behind them, are in [ARCHITECTURE.md](ARCHITECTURE.md).

### Add-ins

Add-ins are in the repo but off until the owner turns them on. The core works fully without them.

| Add-in | What it does | What it needs |
| --- | --- | --- |
| [slack](addons/slack/README.md) | People talk to their assistant from a Slack DM too | The relay, a Slack app |
| [siri](addons/siri/README.md) | People talk to their assistant through Siri, with an Apple Shortcut | The relay, Apple's Shortcuts app |
| [voice-notes](addons/voice-notes/README.md) | Voice notes are transcribed with Whisper before the assistant gets them | uv, about 1 GB of disk |
| [stickers](addons/stickers/README.md) | Stickers reach the assistant as a picture, animated ones as a grid of frames, not only as their emoji | uv, about 100 MB of disk |
| [locations](addons/locations/README.md) | Shared locations reach the assistant, and the latest position is kept for tools, from Telegram or the OwnTracks app | Nothing; the relay for OwnTracks |
| [reminders](addons/reminders/README.md) | A person's Apple Reminders reach their assistant, which can add to them, through an iPhone Shortcut, with no Apple sign-in on the machine | The relay, Apple's Shortcuts app |
| [quiet-hours](addons/quiet-hours/README.md) | The bot's messages arrive silently during each person's quiet hours | Nothing |
| [model-fallback](addons/model-fallback/README.md) | A person hears when a safety classifier moves their assistant to another model, and it can switch back | Nothing |
| [skill-notes](addons/skill-notes/README.md) | A quiet note in the chat whenever an assistant loads a skill | Nothing |
| [backup](addons/backup/README.md) | Every night, everyone's settings, memory and files, and the bot's own, go into an encrypted backup off the machine | restic, and somewhere to keep the backups |

## Requirements

[Preparing a fresh VM](#preparing-a-fresh-vm) below sets all of this up on a new Ubuntu server.

- Any Linux machine with systemd, and a user with passwordless sudo. The bot and every person's assistant run as that user, with full admin rights, so add only people you would trust with the machine.
- The machine's timezone set to UTC (`sudo timedatectl set-timezone UTC`): cron runs in it, and the bot reads cron times as UTC.
- [Claude Code](https://code.claude.com), with its first-run onboarding done (run `claude` once by hand). The sessions use either Claude Code's own login (a Claude Pro or Max plan: run `claude`, then `/login`) or an Anthropic API key.
- [Bun](https://bun.sh) (the Telegram plugin runs on it), plus `tmux`, `jq`, `curl` 8.3+, `git`, `openssl` and Python 3.10+. `assistant install` adds [uv](https://docs.astral.sh/uv/) and [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/) if they're missing (cloudflared from Cloudflare's apt repository; elsewhere than Debian or Ubuntu, install it yourself).
- A Telegram bot token. Message [@BotFather](https://t.me/BotFather), send `/newbot`, pick a name and a username ending in `bot`, and copy the token.
- For the control panel: a free Cloudflare account, and Node.js 22+ (`npx` runs `wrangler` to deploy the relay). Everything else works without it.
- For development: Node.js and `shellcheck`, which `./check` uses with uv.

## Preparing a fresh VM

What a new server needs before `assistant install`, as commands for Ubuntu 24.04 LTS. Run them over SSH as the user the bot will run as (on most cloud images, `ubuntu`).

**Pick the machine.** It has to be a full VM, not a container: the bot needs systemd and sudo. Give it nothing else to run, since every assistant is a full admin. The models run at Anthropic, so the machine needs memory more than CPU. Count about 2 GB for the system and the bot and about 1 GB per person (more with many MCP servers). The voice-notes add-in also needs 1.5–2.5 GB while it transcribes, and about 1 GB of disk. A 4-core, 8 GB VPS is plenty for two to four people. Choose Ubuntu 24.04: Ubuntu 22.04 and Debian 12 ship a `curl` older than 8.3.

**1. Packages, UTC, passwordless sudo.**

```sh
sudo apt update && sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt -y upgrade
sudo apt -y install tmux jq curl git openssl unzip python3
sudo timedatectl set-timezone UTC
echo "$USER ALL=(ALL) NOPASSWD:ALL" | sudo tee /etc/sudoers.d/90-$USER
```

If the upgrade brought a new kernel ("System restart required"), `sudo reboot` and log in again.

**2. Swap and a firewall.** Long-running Claude Code sessions can grow in memory, and swap keeps one spike from taking the machine down. The bot needs no open ports: the router only connects out, to Telegram and the relay, and so does a browser take-over's tunnel.

```sh
if ! swapon --show | grep -q .; then
  sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
fi
sudo ufw allow OpenSSH && sudo ufw --force enable
```

**3. Bun and Claude Code.** `assistant install` adds uv and cloudflared itself.

```sh
curl -fsSL https://bun.sh/install | bash
curl -fsSL https://claude.ai/install.sh | bash
```

Log out and in again so `~/.bun/bin` and `~/.local/bin` are on your `PATH`, and check with `claude --version && bun --version`. Then run `claude` once and go through its first-run screens. To use a Claude Pro or Max plan, `/login` there; for an API key, skip it (`assistant setup` asks for the key). `/exit` when done.

**4. Node.js 22, for the control panel only.** Ubuntu's own Node.js is too old for `wrangler`.

```sh
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt-get install -y nodejs
```

**5. Lock SSH to keys.** The bot can do anything on this machine, so a guessed password would give away everything. Log in with an SSH key first; once that works, turn password logins off. The file sorts first among the SSH settings, so it wins over a cloud image's own.

```sh
echo 'PasswordAuthentication no' | sudo tee /etc/ssh/sshd_config.d/00-no-passwords.conf
sudo sshd -t && sudo systemctl restart ssh
```

If you lock yourself out, your provider's web console still gets you in. Take a snapshot in the provider's panel before big changes.

Plugins that the assistants install may need more on the machine (a browser, `rclone`, a password manager's CLI): each plugin's README says what.

## Setup

Run everything as the user the bot will run as, not root.

**1. Get the code.** The docs assume it lives in `~/assistant`; it can live anywhere.

```sh
git clone https://github.com/korsicheg/multiclaude ~/assistant
```

**2. Install.** This sets the machine up for the user who runs it, and is safe to run again.

```sh
~/assistant/bin/assistant install
```

It links `assistant`, `tg`, `takeover` and `claude-usage` into `~/.local/bin` (make sure that is on your `PATH`), and then:

- adds three Claude Code managed settings in `/etc/claude-code/managed-settings.json`: `channelsEnabled`, so the front desk can use `--channels`; `skipDangerousModePermissionPrompt`, so the bypass-permissions warning doesn't hold up sessions nobody watches; and `cleanupPeriodDays`, which keeps the transcripts that `--continue` and the router read.
- installs [uv](https://docs.astral.sh/uv/) into `~/.local/bin` if it's missing: plugins' MCP servers and the voice-notes and stickers add-ins run with `uv` and `uvx`.
- installs `cloudflared` from Cloudflare's apt repository if it's missing, so `apt upgrade` keeps it current: browser take-overs reach the person's phone or computer through its quick tunnels.
- sets up the status line, which saves the snapshot `/usage`, `/model` and `/effort` read.
- writes the assistants' rules to `~/workspaces/.claude/CLAUDE.md`, from `system/workspaces-CLAUDE.md` and the rules of each add-in that is on. Claude Code loads a parent folder's `.claude/CLAUDE.md`, so every assistant reads it, and the front desk never does.
- installs the Telegram plugin for the repo's folder only (its `.claude/settings.json` enables it there).
- installs the two systemd units, `assistant` and `assistant-watcher`.
- adds two cron lines: `assistant shared sync` every 10 minutes, and `assistant health sample` every 5 (see [Machine health](#machine-health)).

**3. Token and login.**

```sh
assistant setup
```

It asks for the bot token and saves it in `~/.claude/channels/telegram/.env`, and the bot's name and username in `~/assistant/config.json`. Then it asks for an Anthropic API key: enter one, or press Enter to have the sessions use Claude Code's own login. A key goes to `~/assistant/.env`, readable only by you. If your API calls go through a proxy, have `ANTHROPIC_BASE_URL` set when you run `assistant setup`, and it is saved there too. Then it starts the service.

**4. Pair yourself.** Send the bot any message on Telegram. It replies with a 6-character code. Then run:

```sh
assistant pair <code> <name> "Display Name"     # e.g. assistant pair a1b2c3 ada "Ada"
```

This approves you and switches the bot to allowlist mode, so strangers get no reply. It also creates `~/workspaces/<name>` with its own short, personal `CLAUDE.md` (from `templates/CLAUDE.md.tmpl`), starts your assistant, and sends you a welcome message. The first person added becomes the owner: the default target for `assistant notify` and `assistant job`, and the one told about updates, outages and the machine's health.

**5. Check.**

```sh
assistant status
```

### Plan limits (optional)

The status line, `/usage` and the control panel show the plan's 5-hour and weekly limits, and each person is told once as a limit passes 80%, 90% and 99% of the 5-hour limit, and 25%, 50%, 75%, 90% and 99% of a weekly one. They come from a separate claude.ai login that only has the `user:profile` scope, so it can read usage but can't run anything. The assistants' own credentials are never used for this.

```sh
claude-usage login            # prints a URL: open it, approve, then
claude-usage login <code>     # with the code it shows
claude-usage                  # prints the usage JSON
```

The login is kept in `~/.config/claude-usage/credentials.json` and refreshes by itself.

### The control panel and the relay

`/config` opens the control panel, a Telegram Mini App where each person sees their usage and what their scheduled jobs did, and changes their model, effort, name, timezone, scheduled jobs and MCP servers. The owner also sees the machine's health there. It is served by the relay, a Cloudflare Worker on the free plan, which also holds the panel's taps until the router collects them, so nothing is lost while the machine is offline. The router connects out to it, so nothing on the machine is reachable from the internet.

1. In Cloudflare, create an API token from the **Edit Cloudflare Workers** template, and note your account id.
2. Save them, readable only by you:

   ```sh
   (umask 077; mkdir -p ~/assistant/state
    printf '{"api_token": "%s", "account_id": "%s"}\n' "…" "…" > ~/assistant/state/cloudflare.json)
   ```

3. Deploy:

   ```sh
   assistant relay deploy
   ```

   It deploys the Worker `assistant-relay` on your workers.dev subdomain, gives it its secrets, saves its address for the router in `state/relay.json`, and restarts the router, which then puts a **Panel** button by every chat's message box.

Run `assistant relay deploy` again after a change in `relay/`, and after turning an add-in with routes on the relay (slack, siri, locations, reminders) on or off.

### The bot's name

The bot calls itself by its Telegram name, saved as `name` in `config.json`. To change it, edit `name` there (`aka` adds a second name, shown next to it), then run `assistant install`, which writes it into the assistants' rules, and `sudo systemctl restart assistant-watcher`, which updates the bot's description.

## Add-ins

```sh
assistant addon list                # each add-in, on or off, and what it does
assistant addon enable <name>
assistant addon disable <name>
```

Enabling runs the add-in's own setup, if it has one (voice-notes installs Whisper, stickers its renderer, slack checks its tokens, backup starts its repository), and disabling its teardown (backup takes its cron line out). Enabling lists it in `config.json`, adds its rules to the assistants' rules, restarts the router and prints anything left to do. Each assistant reads its new rules from its next `/clear` or restart. Each add-in's README says what it needs and how to set it up:

- [slack](addons/slack/README.md), [siri](addons/siri/README.md), [voice-notes](addons/voice-notes/README.md), [stickers](addons/stickers/README.md), [locations](addons/locations/README.md), [reminders](addons/reminders/README.md), [quiet-hours](addons/quiet-hours/README.md), [model-fallback](addons/model-fallback/README.md), [skill-notes](addons/skill-notes/README.md), [backup](addons/backup/README.md)

## Adding people

```sh
assistant invite                                # open pairing
# they message the bot and send you the code it gives them
assistant pair <code> <name> "Display Name"     # approves them and closes pairing again
```

`assistant invite off` closes pairing without adding anyone. `assistant pair <code>` without a name only approves the code; `assistant add-user <name> <telegram id> ["Display Name"]` later gives an approved person their assistant. Telegram access is only ever changed on the machine, never from a chat message.

A person's entry in `users.json` can also hold `"timezone"` (an IANA name such as `"Europe/Berlin"`), which sets the times the bot shows them. Their assistant sets it when it learns it, and they can change it in the control panel.

## Bot commands

| Command | What it does |
| --- | --- |
| `/clear` | Clears your assistant's conversation (memory is kept), first reloading its plugins and skills so it starts on their latest versions, and deletes your chat with the bot from the last 48 hours. While your assistant is busy, `/clear` waits for it to finish and says so, with a **Cancel** button. |
| `/restart` | A harder `/clear`: ends your assistant's Claude Code process at once, even mid-task or stuck, and starts a new one with a new conversation. That also reloads its MCP servers, plugins and settings. Memory, files and session-only `/model` and `/effort` switches stay. |
| `/stop` | Stops what your assistant is doing, like pressing Esc in its terminal. Messages you sent meanwhile still come next. |
| `/usage` | Context used, session cost, model and effort, the plan's limits, and what all sessions have cost so far, per person |
| `/model`, `/model opus`, `/model opus default` | Shows or switches the model. A switch is for this session only; adding `default` also keeps it after the session restarts and for scheduled jobs. |
| `/effort`, `/effort high`, `/effort high default` | The same, for effort (`low`, `medium`, `high`, `xhigh`, `max`) |
| `/plugin` (or `/plugins`), `/plugin install name@marketplace`, `/plugin marketplace add owner/repo` … | Plugins and marketplaces, with Claude Code's own syntax (`/plugin help` lists it all). Changes go into your own folder only, and your assistant reloads its plugins once it's idle. |
| `/skills`, `/skills off name`, `/skills on name` | Lists your skills (yours, shared, from plugins, built in), or turns one off or on for you |
| `/mcp`, `/mcp add …`, `/mcp login name`, `/mcp remove name` … | Your MCP servers, with Claude Code's syntax (`/mcp help` lists it all). Never shared: each person connects their own accounts. `/mcp login` sends a sign-in link and takes the address your browser lands on. |
| `/reload-plugins`, `/reload-plugins --force` | Reloads your assistant's plugins, skills and MCP servers once it's idle. A reload that changes MCP tools costs the prompt cache, so Claude Code holds it back until it comes with `--force`. Not in Telegram's command menu, which takes no hyphens. |
| `/help` | What you can send the bot, the other ways you can reach it, and these commands. Then your assistant sends what it can do with your MCP servers. |
| `/config` | A button that opens the control panel (so does the **Panel** button by the message box): model and effort, context and plan limits, your scheduled jobs (pause, resume, delete) and what they did (each run's outcome, cost and the start of its answer, with today's and the week's totals), your MCP servers (reconnect, sign in, turn off), your plugins and skills (on or off, as `/plugin enable|disable` and `/skills on|off`), and your name and timezone. The owner also sees the machine: CPU, memory, swap and disks with their last 24 hours, the alerts standing, the router and each assistant, and the programs using the most memory. It needs the relay. |

With `default`, `/model` and `/effort` write `model` or `effortLevel` into that person's `~/workspaces/<name>/.claude/settings.json`, which wins over the shared `~/.claude/settings.json`; the shared file is never changed. `max` effort can only be set for one session, because Claude Code doesn't save it.

The router also:

- sets the bot's command menu, its description (the **What can this bot do?** box of an empty chat) and the line on its profile when it starts.
- relays questions with buttons: an assistant asks with `tg ask`, you tap, the buttons are replaced by your choice, and the assistant edits the question to say what it will do. You can also just type an answer.
- passes on your reactions: a 👍 or 👎 on any message reaches your assistant as a short answer to it, which it acts on when the meaning is clear and otherwise asks about.
- passes on Claude Code's approval questions: when a built-in safety check stops an assistant, you get the question with Approve and Deny buttons. Claude Code denies it by itself after 2 minutes.
- copes with the machine losing its network. Replies that can't reach Telegram are held and go out once they can, marked with when they were written; a request whose turn failed because Claude couldn't be reached is picked up again by itself; and the owner gets a quiet note about an outage of 3 minutes or more.
- keeps the bot listening: if the front desk's Telegram plugin stops, the router restarts the front desk within two minutes and tells the owner. Messages sent meanwhile arrive then.
- keeps Claude Code current: it runs `claude update` every half hour and restarts each session still on an older version once it has been idle for 5 minutes, conversation included. The owner gets one message per update.

Button taps, what a reply quotes, reactions, formatted messages and a paired person's `/help` reach the router through a small patch to the Telegram plugin (`bin/patch-telegram-plugin`), which the front desk reapplies before every start, so plugin updates keep it. The same patch keeps the bot's Telegram connection with the front desk: another session that starts the plugin, such as a `claude mcp list` in `~/assistant`, gets its tools but doesn't poll.

## Browser take-overs

The assistants browse with [agent-browser](https://github.com/vercel-labs/agent-browser), which they or their plugins install. When its browser hits something only the person can do, such as a CAPTCHA, a "press and hold" check, a 3-D Secure payment confirmation or a consent screen, the assistant runs:

```sh
takeover --session <agent-browser session> --for "Solve the CAPTCHA, then tap Done"
```

The person gets a Telegram message with what to do and an **Open the browser** button. The page shows that browser live, and while they have it the browser takes the size of their screen, so it fills a phone or a laptop window 1:1; afterwards it goes back to its own. On a phone it fills the screen under a slim bar with the task and **Done**, and works like the site itself: they tap in it, drag to scroll, touch and hold to press and drag (a slider, a "press and hold" check) and pinch to zoom, and tapping a text box brings up their keyboard, which types into the browser as they go, autocorrect and any alphabet included. On a computer they click, hover and drag with the mouse, scroll with the wheel or trackpad, and type and paste straight into it once they've clicked in it. Then they tap **Done** (or **I can't do it**). The command waits until then, or until the link expires (`--minutes`, 15 by default, at most 60), edits the message to say how it ended, and exits: 0 for Done, 3 for I can't do it, 4 when the time ran out, 1 for anything else. The assistants run it in the background and carry on when it ends.

It needs `cloudflared`, which `assistant install` adds, and no Cloudflare account: each take-over opens a [quick tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/), a random `trycloudflare.com` address that lasts only while it runs. The page shows whatever that browser shows, payment pages included, so every address is under a long random token, the page is only served over HTTPS, nothing about it is logged, and one browser is taken over by one person at a time. To try it on the machine without Telegram, `--print-link` prints the link instead of sending it. `--home` finds a session a plugin runs with its own `HOME`.

## Machine health

Cron runs `assistant health sample` every 5 minutes (`assistant install` adds the line). Each sample reads how busy the CPU was since the last one, the load, memory and swap used, each disk, the uptime, the five programs using the most memory, whether the router is running and whether each person's assistant is. A week of samples is kept in `~/assistant/state/health/`.

The owner gets a Telegram message when one of these goes wrong, and again when it's fine, never in between. The message has the numbers, and for CPU and memory the programs using the most memory, named by whose they are (`claude (ada)`, `Chrome, 34 processes`). The defaults:

| What | Alerts | Fine again |
| --- | --- | --- |
| CPU busy | 90% or more for 3 samples in a row (15 minutes) | under 70% |
| Memory used (what isn't available) | 90% or more for 3 samples | under 80% |
| Swap used | 50% or more for 3 samples | under 40% |
| A disk (`/`, and the repo's or the workspaces' if elsewhere) | 85% full, and again at 95% | under 80% |
| The router (`assistant-watcher`) | not running, at once | running |
| A person's assistant | no Claude Code in its tmux session for 2 samples | running |

The gap between the two numbers keeps a value hovering at the line from sending a message every 5 minutes. To change any of them, add a `"health"` section to `~/assistant/config.json` with only what you change; the next sample uses it:

```json
{"health": {"ram": {"at": 95, "clear": 85, "samples": 2}, "disk": {"at": 80, "critical": 90, "critical_clear": 85}}}
```

The keys are `cpu`, `ram`, `swap` and `disk` (`at`, `clear`, `samples`, and for disks `critical`, `critical_clear`), `router` and `assistant` (`samples`). The alerts go out like `assistant notify`, straight through Telegram's Bot API, so the owner still hears when the router is what's down. A machine that's off can't say so: then the samples stop, and the control panel's Machine section says how old the last one is.

In the control panel, the owner sees the machine with bars for CPU, memory, swap and each disk, a sparkline of the last 24 hours under CPU, memory and the fullest disk (touch it to read a time), the alerts standing, the router and each assistant, the uptime, and the programs using the most memory. On the machine:

```sh
assistant health                # the machine now, the alerts standing and the last day's highs
```

## Day to day

```sh
assistant help                  # every command
assistant status                # services, pairing, people, the front desk's last lines
assistant attach [<name>]       # watch someone's assistant (or the front desk); leave with Ctrl-b d
assistant start|stop|restart    # front desk, every assistant and the router
assistant start-users           # start any person's assistant that isn't running
assistant notify [--user <name>] "text"         # a Telegram message to one person (default: the owner)
assistant job [--user <name>] [--model <m>] [--deny <tools>] "instructions"   # a one-shot task whose answer goes to that person
assistant health                # the machine now: CPU, memory, swap, disks, the router and each assistant
assistant shared list           # plugins, skills and marketplaces: shared and per person
assistant shared sync [--dry-run] [--wait]
assistant relay deploy          # deploy the relay: the control panel, and the add-ins' routes
assistant addon list|enable|disable
```

`assistant restart` stops every tmux session, so everyone's assistant restarts (each resumes its conversation). For a router change, restart only the router: `sudo systemctl restart assistant-watcher`. It routes the messages that arrived while it was down.

- **Logs:** the router logs to `journalctl -u assistant-watcher`. Jobs log to `~/assistant/logs/jobs.log` (errors in `jobs.err`), and each run's outcome, cost and the start of its answer to `state/job-runs/<name>.jsonl` (each person's last 200, shown in the control panel's Activity); the shared sync logs to `logs/shared.log`, and the health samples to `logs/health.log` (only the alerts they sent, and errors).
- **Scheduled jobs:** each assistant adds its own cron lines (`assistant job --user <name> "…"`), each under a `# <name>:` comment and listed in that person's `~/workspaces/<name>/jobs.md`. `assistant job` runs `claude -p` in the person's folder, at most one run of the same job at a time and for up to an hour, and sends its final answer unless that is exactly `NOTHING`, which makes "only tell me if…" jobs possible. `--model` runs a job on another model than the person's saved one, and `--deny` forbids tools outright (sending mail, say).
- **Plugins, skills and marketplaces:** each assistant installs them only in its person's own `~/workspaces/<name>/.claude/`. Once everyone has the same one, `assistant shared sync` moves it to `~/.claude` and deletes the copies. If someone turns a shared one off for themselves, it moves back into everyone else's folder. When you add a person, everything shared moves back into the folders of the people who had it, so the newcomer starts with none of it. The owner gets a message for each move. The bot's `/plugin` and `/mcp` commands run with the `env` from the person's own `.claude/settings.json`, so a login kept there, such as their own GitHub login for a private marketplace, works for them too.

## What's not in this repo

These files hold secrets or per-person data, and `.gitignore` keeps them out. Back them up separately if you want a full restore. The [backup](addons/backup/README.md) add-in does that every night:

- `~/assistant/.env`: the API key and proxy address, if any
- `~/assistant/config.json`: the bot's name and the add-ins that are on
- `~/.claude/channels/telegram/`: the bot token (`.env`) and the access allowlist (`access.json`)
- `~/assistant/users.json`: people, their Telegram ids, roles and timezones, and the add-ins' fields
- `~/assistant/state/`: secrets (`cloudflare.json`, `relay.json`, and the add-ins' own), the router's state (message ids for `/clear`, held messages, open questions, what was shared) and a week of the machine's health
- `~/workspaces/<name>/` (including its `.claude/settings.json`) and `~/.claude/projects/-home-<user>-workspaces-<name>/`: each person's files, memory and chat history
- `~/.claude.json`: among much else, each person's own MCP servers (local scope)
- The crontab (`crontab -l`): everyone's scheduled jobs
- `~/.config/claude-usage/`: the plan-limits login

To restore on a new machine, follow the setup above, put these back, and run `assistant start-users`, or pair everyone again. The backup add-in's README has the commands for its backups.

## Updating

```sh
cd ~/assistant && git pull && assistant install
```

`assistant install` copies the systemd units and the assistants' rules again, and adds any cron line of its own that's missing; everything else is linked into the repo and follows it. Then restart what changed: `sudo systemctl restart assistant-watcher` for the router, `assistant restart` for everything, and `assistant relay deploy` after a change in `relay/`.

## Development

Run `./check` before every commit: the Python tests, the relay's Node tests, ruff and shellcheck. The tests never touch the live system. The code layout, and which doc to update with a change, are in [ARCHITECTURE.md](ARCHITECTURE.md), "Code layout and tests". The bot's Python uses only the standard library.

## License

[PolyForm Noncommercial 1.0.0](LICENSE.md): free to use, change and share for any noncommercial purpose, such as personal use, research, education or a non-profit. Commercial use needs the author's permission.
