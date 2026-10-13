"""Browser take-overs (bin/takeover): when an assistant's browser hits something only its person
can do (a CAPTCHA, a press-and-hold check, a payment confirmation), the person gets a private link,
sees that browser live on their phone, taps and types into it, and taps Done.

  browser   finds the agent-browser session's stream server
  stream    the WebSocket bridge to it: the latest frame in, input events out
  controls  what the person does on the page, as the stream's input events
  page      the web server for the page, every path under a secret token
  tunnel    the Cloudflare quick tunnel that takes the page to the phone
  notice    the link in the person's Telegram chat, and how the take-over ended
  run       the take-over itself, and the command's options and exit status
"""
