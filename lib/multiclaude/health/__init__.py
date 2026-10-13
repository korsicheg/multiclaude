"""The machine's health: `assistant health sample`, which cron runs every 5 minutes, reads the
machine, keeps a week of samples in state/health/, and tells the owner on Telegram when something
runs high or stops, and again when it's fine. It sends through the Bot API itself, never through
the router, so it also tells when the router is what's down. The control panel shows the owner the
latest sample and the last day; `assistant health` prints the machine now.

  machine  CPU, memory, swap, disks, uptime and the programs using the most memory, from /proc;
           whether the router and each person's assistant are running
  history  the latest sample, a week of their numbers, and the last day for the panel's sparklines
  alerts   the rules, where each stands after a sample, and what the owner is told when one changes
  words    how sizes, shares and the top memory users read
  sample   one run of `assistant health sample`
  status   `assistant health`: the machine now, for the terminal
"""
