"""Plugins, skills and marketplaces shared by everyone's assistants.

Each person's assistant installs plugins, marketplaces and skills only in that person's
workspace (~/workspaces/<name>/.claude). A sync moves an item to user scope (~/.claude),
which every Claude session on the VM reads, once every registered person has it, and
deletes the per-person copies:

  plugin       enabled (true) in everyone's project or local settings
  marketplace  declared in everyone's settings with the same source
  skill        a folder in everyone's .claude/skills with identical files

When someone new is registered, everything shared moves back into the workspaces of the
people who had it, marketplaces included, so a newcomer starts with nothing shared; it moves
up again once they have it too. When someone turns a shared item off for themselves (the
plugin set to false, or the skill set to "off" in skillOverrides, in their workspace
settings), it moves back into everyone else's workspace, and its marketplace stays shared.

Only items a sync moved up are changed in ~/.claude; state/shared.json lists them, and the
people who had them, which is how a run tells who is new. Anything
else that appears in ~/.claude is reported to the owner once, since assistants should never
install there directly. The front desk gets every shared plugin switched off in
~/assistant/.claude/settings.local.json.

The router also runs the commands /plugin, /skills and /mcp through here (commands.py,
mcp.py), so they change only the sender's own folder.
"""
