# Telegram AI Bot (Kurigram + 9.todict.tech)

A Telegram bot built with [Kurigram](https://github.com/KurimuzonAkuma/kurigram)
that forwards questions to the AI router at `https://9.todict.tech/v1`
(OpenAI-compatible `/chat/completions` endpoint, SSE streaming).

## Locked to admins

`ADMIN_ONLY=true` (default) makes the bot answer **only** the Telegram user
IDs in `ADMIN_IDS`. Everything from anyone else is dropped before any
handler runs — `/start`, `/help`, `/ai`, `/sh`, buttons, plain messages and
business messages alike. Non-admins get no reply at all, so a stranger
cannot even tell the bot is online (the attempt is only written to the
bot log).

The gate is a `group=-1` handler (`bot/handlers.py -> admin_gate`) that
raises `StopPropagation`, so it also covers commands added later — nothing
can slip past it. Set `ADMIN_ONLY=false` to open the bot up again.

## Commands

These are also registered as the Telegram ☰ **Menu** (via
`set_bot_commands` on startup), so nobody has to remember them:

| Command | What it does |
| --- | --- |
| anything you type | the agent answers — and can run commands on this VPS if needed |
| `/reset` | reset this chat's session (clears history, cancels an open picker) |
| `/model` | which model this bot uses + the model directory |
| `/ai <prompt>` | chat only: pick provider & model yourself, no tool use |
| `/sh <command>` | run one exact shell command (admin) |
| `/help`, `/start` | usage |
| `/stats`, `/broadcast`, `/gitpull` | admin extras |

With `ADMIN_ONLY=true` (default) the bot only reacts to `ADMIN_IDS` — for
everyone else the menu is dead too.

## Project structure

```
aibot/
├── main.py                 # entrypoint
├── config.py                # reads .env / environment variables
├── requirements.txt
├── .env.example
├── bot/
│   ├── client.py             # builds the Kurigram Client instance
│   ├── handlers.py           # /start /help /reset /model /ai + message + button handlers
│   └── mentions.py           # builds the prompt text out of an incoming message
├── ai/
│   ├── models.py             # model catalog, providers, and candidate-order builders
│   ├── agent.py              # agent mode: the one `shell` tool + tool loop
│   └── router.py             # streaming client for /v1/chat/completions + fallback
└── telegram_rich/
    ├── formatting.py          # Markdown -> Telegram HTML
    └── stream.py               # streams the reply, throttled message edits
```

## Two ways to get a reply

### 1. Plain message — automatic, no button

Just type anything, in a private chat **or** a group, no mention or
command needed (a mention / reply-to-bot still gets stripped into a
clean prompt if present). This runs immediately with a fixed order (see
`ai/models.py -> default_candidates()`):

1. `tokenharbor/deepseek-v4.1-flash:free` — the one default model
2. If it fails, a random model from the rest of the catalog (only the
   four providers below are in it).

> Adding the bot as a group **admin** makes Telegram deliver it every
> message regardless of Privacy Mode. If it's a regular (non-admin)
> member instead, disable Privacy Mode in @BotFather (`/setprivacy` →
> `Disable`) or the bot won't receive plain group messages — see
> "Notes" below.

In **agent mode** (see below) the same plain message also lets the model
run commands here first, so "cek spesifikasi vps saya" gets answered from
the real machine instead of from its imagination.

### 2. `/ai <question>` — pick the provider and model yourself

`/ai` is plain chat: it never runs commands, it just replies with the
model you picked. Use a normal message when you want the agent to act.

Sends a button grid (2 per row) with the four exposed providers
(TokenHarbor, Antigravity, NVIDIA NIM, Cloudflare). After picking a
provider, a second button grid lists that provider's models, also 2 per row, plus a "⬅️ Back to
providers" button. Once you pick a model, the bot runs it with this
order (see `ai/models.py -> provider_candidates()`):

1. The model you picked.
2. The rest of that same provider's models (shuffled), if it fails.
3. If the whole chosen provider fails, a shuffled random fallback across
   every other provider in the catalog.

Button taps expire after `PENDING_TTL` seconds (default 10 minutes, see
`.env.example`) — after that, just send `/ai` again.

## Model catalog

`ai/models.py -> MODEL_CATALOG` is deliberately limited to four
providers — everything else on the 9.todict.tech dashboard is left out:

| Provider | Key | Models |
| --- | --- | --- |
| TokenHarbor | `tokenharbor` | 1 (the default) |
| Antigravity | `ag` | 16 |
| NVIDIA NIM | `nvidia` | 10 |
| Cloudflare | `cf` | 13 |

`MODEL_PRIORITY` holds the single default model
(`tokenharbor/deepseek-v4.1-flash:free`); the catalog is both the `/ai`
picker's source and the random-fallback pool for plain messages.
`THINKING_MODELS` marks which models are known to stream reasoning tokens.

> Add/remove providers in `ai/models.py -> MODEL_CATALOG` — the picker is
> built from it, nothing else needs touching.

## Button colors (Bot API 9.4 `style`)

Provider/model buttons request the `"primary"` (blue) style, added in
[Bot API 9.4](https://core.telegram.org/bots/api-changelog#february-9-2026).
This is feature-detected at import time (`bot/handlers.py ->
_SUPPORTS_BUTTON_STYLE`) against whatever Kurigram version is installed
— if the installed version doesn't support the `style` parameter yet,
it's silently omitted instead of crashing, and starts applying
automatically the moment Kurigram catches up.

## Agent mode — the bot actually runs things

Modelled on [hermes-agent](https://github.com/nousresearch/hermes-agent)'s
idea (agent loop + `terminal` tool + tool-progress in the chat), minus
everything heavy: **one** tool, **one** loop, no skills/memory/subagent
machinery, no local model — so it costs nothing extra in RAM on top of the
process already running here.

What happens on a plain message from an admin:

```
you  -> cek spesifikasi vps saya
bot  -> 🖥️ lscpu | grep ...            — exit 0   (output shown, tappable)
        🖥️ free -h; df -h              — exit 0
        <answer built from the real output>
```

Identity questions ("kamu model apa?") are answered straight from the
system prompt — the model id, the router URL and the app directory are
injected into the agent brief (`ai/agent.py -> agent_system_prompt`), and
the brief explicitly forbids probing the filesystem/env to figure that out.
The same brief keeps tool use on topic: 1-3 focused commands, no grepping
the whole filesystem, no env dumps, no secrets printed.

Implementation:

| Piece | Where |
| --- | --- |
| tool schema (`shell`), operating brief, loop | `ai/agent.py` |
| `tools=` + streaming `tool_calls` accumulation | `ai/router.py` |
| tool progress rendered above the answer | `telegram_rich/stream.py` (`push_tool`) |
| agent vs plain chat switch | `bot/handlers.py` (`_agent_allowed`) |

Settings (`.env`):

| Variable | Default | Meaning |
| --- | --- | --- |
| `ENABLE_AGENT` | `true` | allow tool use at all |
| `AGENT_ADMIN_ONLY` | `true` | only `ADMIN_IDS` get shell access |
| `AGENT_MAX_STEPS` | `6` | model+tool rounds per message |
| `SHELL_TIMEOUT` | `60` | per command timeout |
| `SHELL_WORKDIR` | `/root` | working directory for commands |

> Agent mode is a root shell with extra steps: keep `ADMIN_IDS` private.
> `AGENT_ADMIN_ONLY` is a second lock on top of `ADMIN_ONLY` (plain messages
> in agent mode are the only place tool use can happen).

`/sh <command>` is still there for when you want to run one exact command
yourself — same shell, same timeout, no model in the middle.

## Reply layout (one message, always)

Every reply is **one** Telegram message, edited in place as it streams
(`edit_text`, throttled by `DRAFT_UPDATE_INTERVAL`) — nothing is split
into a second bubble and no continuation is parked somewhere else:

```
🧠 [reasoning]        -> collapsed expandable blockquote (last ~600 chars)
🔧 command · exit 0   -> one collapsed block per command (agent mode, max 3)
<the answer>          -> markdown -> HTML, tables/headings tidied
```

Reasoning and tool output are the noisy part, so they stay short,
collapsed, and are never allowed to push the answer out of the message —
if the budget still gets tight the oldest details are dropped first.
Before this, a long reasoning + tool tail ate the 4096-char budget, the
answer got cut mid-sentence and the rest was dumped to paste.rs, which
looked like the reply "continued in another session". Only a genuinely
huge answer (>3900 chars) is still cut with a paste.rs link.

Markdown that Telegram can't render is tidied instead of shown raw:
`# heading` becomes bold, `| tables |` become a monospace block (columns
line up, inline tags stripped since `<pre>` can't hold them), `---` rules
are dropped, and streamed reasoning that arrives as glued fragments
("check it.The next step") gets its spacing normalised.

Conversation history is a **single session per `chat_id`** (up to
`MAX_HISTORY` messages), shared by every topic/thread in that chat, and
kept in memory — it does not survive a restart, use `/reset` to clear it.

## Reasoning / "thinking" display

Reasoning tokens are rendered inside a native Telegram expandable
blockquote (`<blockquote expandable>` in HTML parse mode) above the final
answer, so people can tap to expand/collapse it instead of it cluttering
the chat.

> [Bot API 10.1](https://core.telegram.org/bots/api-changelog#june-11-2026)
> added native **Rich Messages** (`sendRichMessage`, `sendRichMessageDraft`,
> `RichBlockThinking`) specifically for streaming AI replies with a
> proper native "thinking" block. As of this writing Kurigram doesn't
> expose it yet, so this bot still uses the expandable-blockquote HTML
> approach above. Worth revisiting once Kurigram adds support.

## Conversation history

Kept in memory per `chat_id` (up to `MAX_HISTORY` messages, see `.env`).
Lost on process restart. Use `/reset` to clear it manually.

## Notes

- `TG_API_ID` / `TG_API_HASH` are still required even though this is a
  bot (not a user account), because Kurigram is an MTProto client.
- Make sure the bot's **Privacy Mode** is disabled in @BotFather
  (`/setprivacy` → `Disable`) if it's not a group admin and you still
  want it to see every group message.

## Admin, SQLite & deployment commands

Users are registered in a local SQLite database when they use `/start`.
The database file defaults to `data/bot.db` and is intentionally local.

Set `ADMIN_IDS` to the numeric Telegram user ID(s) of trusted
administrators. With `ADMIN_ONLY=true` (the default) they are also the only
accounts the bot reacts to at all — see "Locked to admins" above.

Admins get:

- Plain messages also get agent mode (tool use) for admins — see above.
- `/sh <command>` — run a shell command on the VPS hosting the bot
  (output posted back as a `<pre>` block with the exit code; bounded by
  `SHELL_TIMEOUT`, run from `SHELL_WORKDIR`, disabled with
  `ENABLE_SHELL=false`). Full shell access as root — keep `ADMIN_IDS`
  private.
- `/stats` — database/user count.
- `/broadcast pesan` — send a text broadcast to every user who has started the bot.
- Reply to a message + `/broadcast` — copy that message to every registered user.
- `/gitpull` — `git pull --ff-only`, install `requirements.txt`, then replace the
  running Python process with the updated `main.py`.
- `GIT_AUTO_PULL=true` (default) does the same pull + `pip install` at
  startup. Updates are pull-only: the bot never commits or pushes.

The `/gitpull` command is intentionally admin-only. Keep `ADMIN_IDS` private.

## Button colors

The provider picker uses **primary (blue)** buttons, model choices use
**success (green)** buttons, and navigation/back uses **danger (red)**.
The `style` argument is feature-detected so older Kurigram versions continue
to work without crashing.

## GitHub

Commit `data/bot.db` only if you deliberately want to version the database.
For a normal deployment it should remain ignored and backed up separately.
