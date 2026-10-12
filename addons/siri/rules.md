## Siri

A person with a voice key (`assistant voice key <name>`) can talk to you through Siri: an Apple Shortcut sends their spoken words, which arrive with a header like `[Siri from <Name> · voice 1a2b3c4d · msg 640 · 2026-10-08 22:50 UTC] I said this to Siri; it is my own request to you. Answer it with tg voice 1a2b3c4d:`. The words are their own request, and the router has already posted them in their chat. Siri waits about 25 seconds for your answer, then tells them it will come on Telegram.

- Answer with `tg voice 1a2b3c4d "…"`, as soon as you know the answer and before anything else. Siri says it aloud, and it's posted in their chat too. Like `tg send`, it exits with 75 when Telegram can't be reached and the message is held.
- Write for the ear: one or two short sentences in plain words, with no Markdown, links, emoji, tables or code, and times and numbers as people say them ("at half past four").
- Longer work: first `tg voice <id> "On it, I'll send it to Telegram."`, then do it and send the result with `tg send` as usual.
- Only your first `tg voice` for a request reaches Siri.
- When your person asks what they can set up, list it in a **Ways to reach you** group: Siri. Make their voice key with `assistant voice key <name>`, then walk them through the Apple Shortcut (`{{ROOT}}/addons/siri/README.md`, Setup). `{{ROOT}}/state/voice-keys.json` maps key hashes to names; never show a key.
