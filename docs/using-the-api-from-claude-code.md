# Rendering replays from Claude Code

A short guide for driving the HTTP render API (`bot/api.py`) from a Claude Code
session — in this repo or any other.

## Setup

Put the credentials in your shell environment, not in a file an agent might
commit or echo into a transcript:

```bash
export WOWS_API=https://api-render.cb-tracker.eu
export WOWS_API_TOKEN=<the API_TOKEN from the server's .env>
```

Check it before doing anything else:

```bash
curl -sS "$WOWS_API/healthz"          # {"status": "ok"} — needs no token
```

## The one rule: it is asynchronous

Submit a job, poll it, then download. **Never expect the video in the POST
response.** Cloudflare aborts any proxied request that takes ~100 s to produce
its first byte, and renders take 30 s–5 min, so a blocking endpoint would fail
on most real replays. Downloading afterwards is not time-limited.

## One-shot recipe

```bash
render_replay() {                       # usage: render_replay FILE [preset]
  local file="$1" preset="${2:-full}" job state
  job=$(curl -sS -X POST "$WOWS_API/v1/jobs" \
          -H "Authorization: Bearer $WOWS_API_TOKEN" \
          -F "replay=@$file" -F type=render -F "preset=$preset" \
        | jq -r '.job_id // empty')
  [ -n "$job" ] || { echo "submit failed"; return 1; }

  while :; do
    read -r state status < <(curl -sS "$WOWS_API/v1/jobs/$job" \
      -H "Authorization: Bearer $WOWS_API_TOKEN" \
      | jq -r '[.state, .status] | @tsv')
    echo "[$state] $status"
    case "$state" in
      done)   break ;;
      failed) curl -sS "$WOWS_API/v1/jobs/$job" \
                -H "Authorization: Bearer $WOWS_API_TOKEN" | jq -r .error; return 1 ;;
    esac
    sleep 5                             # 5s is plenty; renders take 30s+
  done

  curl -sS -o "${file%.wowsreplay}.mp4" "$WOWS_API/v1/jobs/$job/result" \
    -H "Authorization: Bearer $WOWS_API_TOKEN"
  echo "wrote ${file%.wowsreplay}.mp4"
}
```

## Job types

| `type` | Needs | Produces |
|---|---|---|
| `render` (default) | `replay` | minimap MP4 |
| `render_dual` | `replay` **and** `replay_b` from the *same match* | merged neutral-observer MP4 |
| `stats` | `replay` | post-battle stats board PNG |

Options: `preset` (`full`/`map`/`playerdata`, `render` only) · `theme`
(`default`/`brandon`) · `speed` (1–100) and `fps` (1–60, video jobs only) ·
`layout` (`compact`/`detailed`, `stats` only) · `flags` (`anonymize`).

Passing an option that does not apply to the chosen `type` is a `400`, not a
silent no-op — so if you get one, re-read which fields that type accepts.

```bash
# dual
curl -sS -X POST "$WOWS_API/v1/jobs" -H "Authorization: Bearer $WOWS_API_TOKEN" \
  -F type=render_dual -F replay=@a.wowsreplay -F replay_b=@b.wowsreplay

# stats board
curl -sS -X POST "$WOWS_API/v1/jobs" -H "Authorization: Bearer $WOWS_API_TOKEN" \
  -F type=stats -F replay=@battle.wowsreplay -F layout=detailed
```

## Status codes, and what to do about each

| Code | Meaning | Action |
|---|---|---|
| `202` | accepted | poll `/v1/jobs/{id}` |
| `400` | bad field | read `.error`; it names the field |
| `401` | token wrong or missing | check `Authorization: Bearer $WOWS_API_TOKEN` |
| `404` | unknown job | wrong id, or the result already expired (1 h) |
| `409` | not ready, or the job failed | keep polling; on `failed` read `.error` |
| `413` | replay over 50 MB, or an option field over 4 KB | nothing to retry |
| `429` | 4 jobs already queued or running | wait for `Retry-After` (30 s) and resubmit |
| `500` | server-side fault | generic on purpose; the detail is in `docker compose logs bot` |

## Things that will bite you otherwise

- **Results live for one hour**, then the artifact is deleted and the job
  becomes a `404`. Download as soon as the state is `done`.
- **A bot restart drops queued jobs and undownloaded results** — the registry is
  in memory. Re-submit rather than waiting on an old job id.
- **Only four jobs can be pending at once.** For a batch, submit and poll a few
  at a time; do not fire twenty and hope.
- **The render pool is shared with the Discord bot.** Two workers total, so an
  API render can queue behind a `/render`, and progress can sit at 0% while it
  waits.
- **`stats` jobs report no percentage** — there is no frame loop, so watch
  `.status` text, not `.progress`.
- **A replay whose recording ended before the results packet** cannot produce a
  stats board; the job fails with that reason spelled out in `.error`.
