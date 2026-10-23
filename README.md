# Telegram AI Bot (Kurigram + 9.todict.tech)

A Telegram bot built with [Kurigram](https://github.com/KurimuzonAkuma/kurigram)
that forwards prompts to the AI router at `https://9.todict.tech/v1`
(OpenAI-compatible `/chat/completions`, SSE streaming) — and that can also
run commands on the VPS it is hosted on.

It runs **one model**, set with `AI_MODEL` (default
`tokenharbor/deepseek-v4.1-flash:free`). There is no provider/model picker:
plain messages and `/ai` both go straight to that model.

## Project structure

```
aibot/
├── main.py                 # entrypoint (+ startup git auto-pull)
├── config.py               # reads .env / environment variables
├── requirements.txt
├── .env.example
├── bot/
│   ├── client.py           # builds the Kurigram Client instance
│   ├── handlers.py         # /start /help /reset /model /ai /sh + message handlers
│   ├── mentions.py         # builds the prompt text out of an incoming message
│   └── updater.py          # git pull --ff-only + pip install (pull only)
├── ai/
│   ├── models.py           # the single model id
│   └── router.py           # streaming client for /v1/chat/completions
└── telegram_rich/
    ├── formatting.py       # Markdown -> Telegram HTML
    └── stream.py           # streams the reply, throttled message edits
```

## Commands

| Command | What it does |
| --- | --- |
| any plain message | asks the model, streams the answer |
| `/ai <prompt>` | same model, explicit command |
| `/model` | shows which model is in use |
| `/reset` | clears this chat's conversation history |
| `/sh <command>` | **admin only** — runs a shell command on the VPS |
| `/stats` | **admin only** — user count / database path |
| `/broadcast <text>` | **admin only** — DMs every registered user |
| `/gitpull` | **admin only** — `git pull --ff-only` + `pip install`, then restart |

In groups the bot answers every text message it receives — add it as a
group admin, or turn off Privacy Mode in @BotFather (`/setprivacy` →
Disable), otherwise Telegram won't deliver plain group messages to it.

## The single model

`ai/models.py -> MODEL` is `AI_MODEL` from `.env`. Change that one value to
switch models; nothing else needs touching. If the model call fails
(HTTP error, timeout, dropped stream) the bot reports it in the chat —
there is no fallback model any more. If some of the answer already
streamed before the failure, that partial answer is kept and finished off
instead of being thrown away.

## VPS shell (`/sh`)

Admins can use the bot as a remote terminal:

```
/sh df -h
/sh systemctl status nginx
/sh tail -n 50 /var/log/nginx/error.log
```

Output is posted back as a `<pre>` block (truncated at ~3500 chars, one
message), with the exit code in the header. Bounded by `SHELL_TIMEOUT`
seconds (default 60) and run from `SHELL_WORKDIR` (default `/root`).
`ENABLE_SHELL=false` disables the command entirely.

> This is full shell access as root on the host: keep `ADMIN_IDS` private.

## Reasoning / "thinking" display

Whenever the model streams reasoning tokens they're rendered inside a
native Telegram expandable blockquote (`<blockquote expandable>` in HTML
parse mode) above the final answer, so people can tap to expand/collapse
the model's reasoning instead of it cluttering the chat.

The reply itself streams in by periodically editing one message
(`edit_text`), throttled by `DRAFT_UPDATE_INTERVAL` seconds. Answers past
~3900 chars are cut off in-chat and the full text is uploaded to
paste.rs with the link underneath.

## Conversation history

Kept in memory per `chat_id` (up to `MAX_HISTORY` messages). Lost on
process restart. Use `/reset` to clear it manually.

## Admin, SQLite & deployment

Users are registered in a local SQLite database when they use `/start`.
The database file defaults to `data/bot.db` and is intentionally local.

Set `ADMIN_IDS` to the numeric Telegram user ID(s) of trusted admins —
they get `/sh`, `/stats`, `/broadcast` and `/gitpull`.

## Auto update from GitHub

`GIT_AUTO_PULL=true` (default) runs `git pull --ff-only` +
`pip install -r requirements.txt` at startup; `/gitpull` does the same on
demand and then replaces the running process (`os.execv`) so the new code
loads immediately. Updates are **pull only** — the bot never commits or
pushes anything.

## Notes

- `TG_API_ID` / `TG_API_HASH` are still required even though this is a
  bot (not a user account), because Kurigram is an MTProto client.
- All bot messages use `parse_mode=HTML` so `<b>` / `<i>` / `<code>` /
  `<pre>` / `<blockquote>` render, and known emoji are swapped for their
  custom-emoji versions (`ENABLE_CUSTOM_EMOJI`).
