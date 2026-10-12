## Model fallback

- When a model's safety filter stops one of your replies, Claude Code moves the conversation to another model, and the router tells your person. With `"restore_model": true` in their entry in `{{ROOT}}/users.json` (or **Back to my model** in the control panel), it switches back once you're idle; without it they switch back with `/model` themselves.
- When your person asks what they can set up, list it under **Claude**: back to their model after a safety fallback, `"restore_model": true` in users.json.
