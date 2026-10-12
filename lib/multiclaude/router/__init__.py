"""The router (assistant-watcher.service): it tails the front desk's transcript and passes each
Telegram message to the sender's own assistant, handles the bot's own commands, and keeps
every session healthy and current. desk.py is the loop; see ARCHITECTURE.md."""
