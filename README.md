# My Plex Bot

A Discord bot that lets my friends request movies and TV shows for my Plex server, with a built-in approval system so I stay in control of what actually gets downloaded.

## The problem

I run Plex, Sonarr, and Radarr at home for my friends and family, and I wanted an easy way for people to request stuff without me having to manually add every title myself. I looked at Overseerr/Seerr first, but I wanted to actually build something myself instead of just deploying someone else's tool — partly to learn AWS and distributed systems properly, and partly because I wanted more control over the approval workflow than a pre-built tool gives you out of the box.

So I built my own version: a Discord bot, hosted entirely on AWS, that talks directly to Sonarr and Radarr running on my home server.

## How it works

- Anyone I've marked as **trusted** can run `!requestMovie` or `!requestTV` with an IMDb or TheTVDB link.
- The bot looks up the title through Sonarr/Radarr, checks it's not already requested, and posts it in a channel with ✅ / ❌ reactions.
- I (or anyone I've made an **admin**) react to approve or deny it.
- Admins and I skip the approval step entirely — our requests go straight through.
- Once something's approved, a Lambda function picks it up automatically and adds it to Sonarr/Radarr — no manual step on my end at all.
- Admins can run `!setupHelp` to set up a read-only `#help` channel that documents every command for everyone in the server, so people don't have to ask what they can do.
- Admins can run `!setupStats` to set up a read-only `#stats` channel showing request totals, approval rate, and a top-requesters leaderboard, which the bot then keeps refreshed automatically every 10 minutes.
- Every request's lifecycle — requested, approved, denied, now available — gets posted to a read-only `#request-log` channel that the bot creates on its own the first time it's needed, so the server has a running audit trail without me digging through old messages.
- A background task checks approved requests against Sonarr/Radarr every 10 minutes, and once a title's actually finished downloading, the bot announces it in `#request-log` with who requested it, then marks it announced so it's never posted twice.
- Admins can run `!untrust @user` to walk a trusted role back to guest, and `!pending` to see everything still awaiting approval without scrolling back through old reaction messages.

There's a full role system under the hood (`GUEST` / `TRUSTED` / `ADMIN` / `OWNER`) built with a Python decorator, so every command checks permissions before it does anything.

## Tech stack

- **Python / discord.py** — the bot itself
- **AWS DynamoDB** — single-table design storing user roles, requests, and dedup state, with a GSI for querying pending approvals
- **AWS Lambda + DynamoDB Streams** — listens for approved requests and automatically calls the Sonarr/Radarr APIs to add them
- **AWS ECS (Fargate)** — runs the bot itself as a long-lived, auto-restarting container instead of a script on my machine
- **Amazon ECR** — hosts the bot's Docker image
- **AWS Systems Manager Parameter Store** — securely stores API keys and tokens instead of baking them into the container
- **Cloudflare Tunnel + Access** — lets AWS reach my home server's Sonarr/Radarr APIs securely, without opening any ports on my router
- **arrapi** — Python wrapper for the Sonarr/Radarr APIs
- **Docker** — packages the bot for ECS

## Why I built it this way

The whole point of this project for me was learning how these pieces actually fit together, not just getting a working bot as fast as possible. A few decisions I made on purpose:

- **Single-table DynamoDB design** instead of a table per data type — forced me to actually think through access patterns instead of just treating it like a SQL database with extra steps.
- **Reaction-based approval instead of a `!approve <id>` command** — better UX, and it pushed me to figure out how to reliably tie a Discord message back to a database record.
- **Bot writes to DynamoDB, Lambda does the actual Sonarr/Radarr call** — decoupling the two means the bot itself never needs direct network access to my home server at all, which shrinks the attack surface a lot.
- **ECS instead of just running the bot on a home server or a cheap VPS** — wanted real experience with containerizing an app, task definitions, IAM roles, and a properly deployed AWS service instead of a `screen` session that dies if my machine reboots.
- **DynamoDB's native TTL instead of a custom cleanup job** — denied requests get a 30-day expiry, and anything left unapproved for 14 days gets flagged stale with a 7-day expiry, so old items just age out on their own instead of me writing and scheduling a cleanup Lambda.

This one's not set up for other people to spin up themselves — it's tied pretty tightly to my own home server, API keys, and Discord server. It's here mostly as a portfolio piece and a record of what I built.