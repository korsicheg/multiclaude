"""Browser take-overs (bin/takeover): when an assistant's browser hits something only its person
can do (a CAPTCHA, a press-and-hold check, a payment confirmation), the person gets a private link,
sees that browser live on their phone or computer, works in it, and taps Done.

  browser   the agent-browser session: its stream server and its viewport
  stream    the WebSocket bridge to it: the latest frame in, input events out
  viewport  the viewport fitted to the person's screen, and put back afterwards
  controls  what the person does on the page, as the stream's input events
  page      the web server for the page, every path under a secret token
  tunnel    the Cloudflare quick tunnel that takes the page to the phone
  notice    the link in the person's Telegram chat, and how the take-over ended
  run       the take-over itself, and the command's options and exit status
"""
