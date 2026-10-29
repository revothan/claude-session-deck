# Claude Session Deck

A small local dashboard for everyone who runs lots of Claude Code sessions at once on a Mac.

It shows every running session as a little bot, sorted by what it needs from you:

- **Needs you**: finished in the last 30 minutes, waiting for your reply
- **Working**: Claude is busy
- **Napping**: idle for 30+ minutes
- **Resting**: sessions you've hidden for a while (they wake up when you use them)

And it lets you:

- Read each session's latest reply and **chat with it** from the page (your message is typed into its terminal tab)
- **Focus** a session's iTerm tab, **quit** it, **resume** an old one, or start a new one
- **Rename** sessions, with suggested names (instant ones, plus 4 ideas from Claude Haiku)
- **Tune** a session: switch model or effort, `/compact`, `/fast`, `/clear`
- See your **plan usage** (5-hour and weekly limits, same numbers as `/usage`) and each session's context size
- Stay on a **daily budget**: each day of the week adds 100% ÷ 7 ≈ 14.3%. What you don't use rolls over to the next day (and overuse is taken off). Warning at 80% of today's budget, alert when you pass it
- See a GitHub-style graph of your prompts per day and your streak
- Get a chime + desktop notification when a session finishes
- Play Bug Squash or do box breathing while you wait

It also picks up **[Kimi Code](https://github.com/MoonshotAI/kimi-code)** sessions (`kimi`) running in iTerm: they show with a Kimi badge, and you can read, chat with, focus, rest, resume and quit them. Tune and Rename are Claude-only.

Everything runs on `127.0.0.1`. Nothing is uploaded anywhere except as listed under Privacy.

## Requirements

- macOS
- [iTerm2](https://iterm2.com) (focus, resume, chat and tune use iTerm's AppleScript)
- Python 3 (the one that ships with macOS is fine; no packages needed)
- [Claude Code](https://claude.com/claude-code), signed in

## Install

```sh
git clone https://github.com/revothan/claude-session-deck.git ~/claude-session-deck
~/claude-session-deck/start.sh
```

`start.sh` starts the server (if it isn't running) and opens http://127.0.0.1:7337.

The first time you click Focus, Chat or Tune, macOS asks whether your terminal may control iTerm. Click OK.

### Optional settings

Create `config.local.sh` next to `start.sh` (it's git-ignored):

```sh
# Flags added when the page starts or resumes a session
export CSD_CLAUDE_ARGS="--dangerously-skip-permissions"
# Use a different port
export PORT=7337
```

### Start it at login (optional)

Add `~/claude-session-deck/start.sh` to System Settings → General → Login Items, or run it from your shell profile.

## How it works

- Running sessions come from `~/.claude/sessions/*.json` (Claude Code's own registry) plus `ps`.
- Titles, last prompts, replies and context size come from the transcripts in `~/.claude/projects/`.
- The prompt graph reads `~/.claude/history.jsonl`.
- Chat, Tune and Rename on a running session type text into its iTerm tab. Tune and Rename only send while the session is idle; a chat message sent to a busy session gets queued by Claude Code.
- Renaming a closed session appends a `custom-title` line to its transcript, the same thing `/rename` does.
- Kimi sessions come from `~/.kimi-code/sessions/*/session_*/` (`state.json` + `agents/main/wire.jsonl`). Kimi doesn't record which process has which session open, so the page matches each running `kimi-code` process by folder and by its iTerm tab title.
- Rest and archive choices are saved in `state.json` next to the server.
- Today's budget = (days of the week so far × 14.3%) − weekly % at local midnight. Today's usage is the weekly % now minus the weekly % at local midnight. The server logs the weekly % to `usage_log.jsonl` while it runs, so the first day it's counted from when you started it.

## Privacy

- **Plan usage** reads your Claude Code login from the macOS keychain (`Claude Code-credentials`) and calls `https://api.anthropic.com/api/oauth/usage`. The token never leaves your machine except in that request to Anthropic, and it's never sent to the browser.
- **Avatars** load from `api.dicebear.com`, seeded with the session ID (no names or content).
- **Fonts and libraries** load from Google Fonts and jsDelivr ([canvas-confetti](https://github.com/catdad/canvas-confetti), [marked](https://github.com/markedjs/marked), [DOMPurify](https://github.com/cure53/DOMPurify)).
- **Claude's name ideas** run `claude -p --model haiku` with the session's title and recent prompts, which uses a little of your plan.

## Limits

- macOS + iTerm2 only. Sessions in Terminal.app, Warp, VS Code, etc. are listed, but Focus, Chat and Tune can't reach them.
- If you type in the iTerm tab at the same moment the page sends a message, the two get mixed.
- Context size is estimated from the last reply's token usage (and the compaction summary right after `/compact`).

## License

MIT
