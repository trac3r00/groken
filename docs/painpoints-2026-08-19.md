# AI Agent Pain Points — sourced research (2026-08-19)

Channels: Hacker News (Algolia API), X/Twitter (via DDG index; X direct + Bluesky gated).
Reddit lane: pending (appended when complete).

## Clusters

### A. Context loss & compounding amnesia
- **HN** — mid-session "forgetting" after compaction: "Claude and other LLMs often 'forget' previous instructions mid-session due to compaction." — https://news.ycombinator.com/item?id=44546127
- **X** — single-session loop of admissions: "'You're right. I should read the transcript instead of asking you.' 'I guessed that URL and I shouldn't have.'" — https://x.com/SethGammon/status/2050691237376635173
- **HN** — race conditions + lost context across approval workflows/multi-session: "Streaming responses + approval workflows + multiple sessions meant race conditions kept popping up... context getting lost after approvals, messages routing to wrong sessions." — https://news.ycombinator.com/item?id=46568757

### B. Runaway loops & runaway damage
- **HN** — "Claude code just goes mad and gets stuck in a loop and I need to pull it out..." — https://news.ycombinator.com/item?id=47475859
- **HN** — confident harmful edits to unrelated code: "Random, unnecessary and often harmful edits are made confidently... They get stuck in loops for an hour." — https://news.ycombinator.com/item?id=45001051
- **HN** — silent deletion: "Half of my file had been silently deleted, replaced by a single terrifying comment along the lines of 'continue similarly with the rest of the file'." — https://news.ycombinator.com/item?id=43927914

### C. Cost & availability anxiety
- **HN** — "Claude Code really drains your pocket extremely fast." — https://news.ycombinator.com/item?id=46515696
- **X** — "Claude Code has major outages globally... daily outage issues which affect productivity." — https://x.com/its_ShubhamK/status/2079592548356763923

### D. Output trust & review burden
- **HN** — "coworkers open up slop PRs with bunch of garbage generated code" — https://news.ycombinator.com/item?id=47388646

### E. State/ephemerality surprises
- **HN** — Cursor conversations expire with unclear rules: "'the conversation expired'. I am not completely sure what the Cursor Agent rules for conversations expiring are." — https://news.ycombinator.com/item?id=46003144

### F. Ecosystem fatigue
- **HN** — MCP dismissed as thin plugin plumbing with proprietary, non-composing skills: "MCP isn't a fundamental enabling technology... just a plugin interface." — https://news.ycombinator.com/item?id=45840088
- **HN** — support bots navigating users in circles (agent UX anti-pattern) — https://news.ycombinator.com/item?id=47239943

Channels: Hacker News (Algolia API, verbatim), X/Twitter (via DDG index; direct fetch gated),
Cursor Community Forum (Discourse JSON, verbatim), GitHub Issues (API, verbatim),
Reddit (permalinks via search index; content fetch gated by anti-bot).

### B2. Destructive filesystem damage (Cursor forum, verbatim)
- **Cursor forum** — agent wiped a C: drive: "Your AI agent just destroyed my entire system. I asked it to clone a Git repository... it ran aggressive cleanup commands including Remove-Item -Recurse -Force... deleted hundreds of thousands of files across my C: drive." — https://forum.cursor.com/t/cursor-agent-completely-wiped-my-c-drive-and-deleted-everything/164675
- **Cursor forum** — agent edits auto-deleted: "The agent will fix a bug... and then the code is immediately deleted. I can actually see the code change, and then see it almost instantly disappear." — https://forum.cursor.com/t/agent-code-changes-are-automatically-deleted/149024
- **Reddit (r/cursor)** — "Cursor's agent deleted my entire project after one..." — https://www.reddit.com/r/cursor/comments/1t6hbl6/ (indexed; body fetch gated)

### A2. Context loss on restart (GitHub, verbatim)
- **GitHub anthropics/claude-code#39663** — "When Claude Code is in the middle of a debugging session and suggests the user restart Claude... all conversation context is permanently lost. There is no automatic save of what was diagnosed, what was changed, and what remains to be done." — https://github.com/anthropics/claude-code/issues/39663

### E2. Unwanted auto-application of edits (Reddit)
- **Reddit (r/cursor)** — "Cursor is now automatically modifying my code and it's creating way too many bugs. Prior to this we used to be able to select the code change... and then apply it." — https://www.reddit.com/r/cursor/comments/1jw3g38/ (indexed snippet)

<!-- reddit-lane: lane cancelled; Reddit sourced via search index (2 permalinks) -->

## What groken does about it (mapping)

| Cluster | groken answer | Status |
|---|---|---|
| A/A2 context loss | Bots are persistent + standing guardrail instructions (`WORKER_DESCRIPTION`); bridge keeps full transcript via `tail` | shipped |
| B/B2 runaway loops, silent deletes, fs damage | Delegation isolates work to the cloud VM (host fs untouched); guardrails: "never delete silently, ask before destructive" upgraded in place via `updateAgent` | shipped |
| C cost/outage anxiety | `groken doctor` self-diagnosis; error translation (auth vs sandbox vs app-version) instead of raw failures | shipped |
| D slop PRs | guardrail: verify post-action state before reporting | shipped |
| E/E2 ephemerality, auto-apply | Bot conversations persist (tail always readable); groken never touches host files itself | inherent |
| F ecosystem fatigue | groken exposes plain CLI + MCP, no lock-in framework | inherent |
