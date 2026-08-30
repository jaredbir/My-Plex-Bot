import discord
from discord.ext import commands, tasks
from arrapi import SonarrAPI, RadarrAPI
import logging
from dotenv import load_dotenv
import os
import boto3
from enum import IntEnum
from functools import wraps
from boto3.dynamodb.conditions import Key, Attr
from requests import Session
import re
import requests
import asyncio
import time
import uuid
from collections import Counter
from arrapi.exceptions import NotFound

# --- Configuration & setup ---

load_dotenv()
Discord_token = os.getenv('DISCORD_TOKEN')

Sonarr_api_key = os.getenv('SONARR_API_KEY')
Sonarr_url = os.getenv('SONARR_BASE_URL')
Radarr_api_key = os.getenv('RADARR_API_KEY')
Radarr_url = os.getenv('RADARR_BASE_URL')

cf_client_id = os.getenv('CF_ACCESS_CLIENT_ID')
cf_client_secret = os.getenv('CF_ACCESS_CLIENT_SECRET')

handler = logging.FileHandler(filename='discord.log', encoding='utf-8', mode='w')

# Shared session so every arrapi request carries the Cloudflare Access
# service token headers, letting it through the Access policy
session = Session()
session.headers.update({
    'CF-Access-Client-Id': cf_client_id,
    'CF-Access-Client-Secret': cf_client_secret
})

# Configure Sonarr/Radarr API
sonarr = SonarrAPI(Sonarr_url, Sonarr_api_key, session=session)
radarr = RadarrAPI(Radarr_url, Radarr_api_key, session=session)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)

dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
table = dynamodb.Table('discord-bot-requests')


def get_tvdb_id_from_url(thetvdb_url: str) -> int | None:
    if 'thetvdb.com' not in thetvdb_url:
        return None

    response = requests.get(thetvdb_url, timeout=10)
    response.raise_for_status()

    match = re.search(r'/series/(\d+)/edit', response.text)
    if match:
        return int(match.group(1))
    return None


def get_imdb_id_from_url(imdb_url: str) -> str | None:
    if 'imdb.com' not in imdb_url:
        return None
    match = re.search(r'/title/(tt\d+)', imdb_url)
    if match:
        return match.group(1)
    return None


# --- Domain model ---

class Role(IntEnum):
    GUEST = 0
    TRUSTED = 1
    ADMIN = 2
    OWNER = 3


# --- Cleanup / notification tuning ---

SECONDS_PER_DAY = 86400
DENIED_ITEM_TTL_DAYS = 30          # how long a denied request stays in the table before DynamoDB TTL deletes it
STALE_REQUEST_THRESHOLD_DAYS = 14  # how long a request can sit unapproved before it's considered stale
STALE_ITEM_TTL_DAYS = 7            # grace period after an item is flagged stale before it's deleted
MAINTENANCE_INTERVAL_MINUTES = 10  # how often the availability/staleness sweep runs


# --- Data access helpers ---

def set_role(discord_id, role):
    table.put_item(
        Item={
            'PK': 'USER#' + str(discord_id),
            'SK': 'PROFILE',
            'role': str(role.name)
        }
    )


def get_user_role(discord_id):
    response = table.get_item(Key={'PK': 'USER#' + str(discord_id), 'SK': 'PROFILE'})
    return response.get('Item', {}).get('role', 'GUEST')


def media_pk_for_request(item):
    """Reconstructs a MEDIA# item's PK from a USER# request record, so the
    two copies of a request can be updated together."""
    if item.get('type') == 'Movie' and item.get('imdbID'):
        return f'MEDIA#MOVIE#{item["imdbID"]}'
    if item.get('type') == 'TV' and item.get('tvdbID'):
        return f'MEDIA#TV#{item["tvdbID"]}'
    return None


# --- Permission decorator ---

def require_role(minimum_role):
    def decorator(func):
        @wraps(func)
        async def wrapper(ctx, *args, **kwargs):
            try:
                user_role_str = get_user_role(ctx.author.id)
            except Exception as e:
                print("ERROR in get_user_role:", e)
                await ctx.channel.send("Something went wrong checking permissions.")
                return
            user_role = Role[user_role_str]

            if user_role < minimum_role:
                await ctx.channel.send("Insufficient Permissions")
                return
            return await func(ctx, *args, **kwargs)
        return wrapper
    return decorator


# --- Request log channel ---

async def log_request_event(guild, message: str):
    """Best-effort post to the #request-log channel (creating it if needed).
    Logging is auxiliary -- any failure here must never break the request/
    approval flow that called it, so all errors are swallowed."""
    if guild is None:
        return

    try:
        log_channel = discord.utils.get(guild.text_channels, name='request-log')

        if log_channel is None:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(send_messages=False, view_channel=True),
                guild.me: discord.PermissionOverwrite(send_messages=True, view_channel=True),
            }
            log_channel = await guild.create_text_channel(
                'request-log', overwrites=overwrites, reason="Request activity log"
            )

        await log_channel.send(message)
    except Exception as e:
        print("ERROR in log_request_event:", e)


# --- Bot events ---

@bot.event
async def on_ready():
    print(f"{bot.user.name} is ready!")
    if not periodic_maintenance.is_running():
        periodic_maintenance.start()


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    await bot.process_commands(message)


@bot.event
async def on_raw_reaction_add(payload):
    if payload.user_id == bot.user.id:
        return

    if payload.emoji.name != "✅" and payload.emoji.name != "❌":
        return

    if Role[get_user_role(payload.user_id)] < Role.ADMIN:
        return

    message_item = table.get_item(Key={'PK': f'MESSAGE#{payload.message_id}', 'SK': 'REQUEST'}).get('Item')

    if message_item is None:
        return

    media_item = table.get_item(Key={'PK': message_item['mediaPK'], 'SK': 'REQUEST'}).get('Item')

    if media_item is None or media_item.get('status') != 'unfinished':
        return

    user_request_key = {
        'PK': f"USER#{media_item['requestedBy']}",
        'SK': f"REQUEST#{media_item['requestId']}"
    }

    guild = bot.get_guild(payload.guild_id)
    title = media_item.get('title', 'Unknown')

    if payload.emoji.name == "✅":
        table.update_item(
            Key={'PK': message_item['mediaPK'], 'SK': 'REQUEST'},
            UpdateExpression="SET #s = :new_status, approvedBy = :approver",
            ExpressionAttributeNames={'#s': 'status'},
            ExpressionAttributeValues={
                ':new_status': 'approved',
                ':approver': str(payload.user_id)
            }
        )
        table.update_item(
            Key=user_request_key,
            UpdateExpression="SET #s = :new_status, approvedBy = :approver",
            ExpressionAttributeNames={'#s': 'status'},
            ExpressionAttributeValues={
                ':new_status': 'approved',
                ':approver': str(payload.user_id)
            }
        )
        await log_request_event(guild, f"✅ **{title}** approved by <@{payload.user_id}>")
    else:
        expires_at = int(time.time()) + DENIED_ITEM_TTL_DAYS * SECONDS_PER_DAY
        table.update_item(
            Key={'PK': message_item['mediaPK'], 'SK': 'REQUEST'},
            UpdateExpression="SET #s = :new_status, deniedBy = :denier, expiresAt = :expires_at",
            ExpressionAttributeNames={'#s': 'status'},
            ExpressionAttributeValues={
                ':new_status': 'denied',
                ':denier': str(payload.user_id),
                ':expires_at': expires_at
            }
        )
        table.update_item(
            Key=user_request_key,
            UpdateExpression="SET #s = :new_status, deniedBy = :denier, expiresAt = :expires_at",
            ExpressionAttributeNames={'#s': 'status'},
            ExpressionAttributeValues={
                ':new_status': 'denied',
                ':denier': str(payload.user_id),
                ':expires_at': expires_at
            }
        )
        await log_request_event(guild, f"❌ **{title}** denied by <@{payload.user_id}>")


# --- Role management commands ---

@bot.command()
@require_role(Role.ADMIN)
async def trust(ctx):
    """All mentioned users will become trusted members, and will be able to make requests"""
    for member in ctx.message.mentions:
        if Role[get_user_role(member.id)] <= Role.TRUSTED:
            set_role(member.id, Role.TRUSTED)

    await ctx.channel.send("Mentioned Users are now Trusted")


@bot.command()
@require_role(Role.ADMIN)
async def untrust(ctx):
    """All mentioned users who are currently Trusted are reverted to Guest. Admins/Owners are left unchanged."""
    demoted = []
    for member in ctx.message.mentions:
        if Role[get_user_role(member.id)] == Role.TRUSTED:
            set_role(member.id, Role.GUEST)
            demoted.append(member.mention)

    if demoted:
        await ctx.channel.send(f"{', '.join(demoted)} are no longer Trusted")
    else:
        await ctx.channel.send("None of the mentioned users were Trusted")


@bot.command()
@require_role(Role.OWNER)
async def makeAdmin(ctx):
    """All mentioned users will become admins, and will be able to appoint trusted users and approve trusted users requests. Users who are already Admins or Owners are left unchanged."""
    try:
        ctx.message.mentions.remove(ctx.message.author)
    except ValueError:
        pass

    newly_promoted = []
    already_admin = []

    for member in ctx.message.mentions:
        if Role[get_user_role(member.id)] >= Role.ADMIN:
            already_admin.append(member.mention)
        else:
            set_role(member.id, Role.ADMIN)
            newly_promoted.append(member.mention)

    if newly_promoted:
        await ctx.channel.send(f"{', '.join(newly_promoted)} are now Admins")
    if already_admin:
        await ctx.channel.send(f"{', '.join(already_admin)} are already an Admin")


# --- Help / documentation ---

COMMAND_DOCS = [
    {
        "usage": "!requestMovie <imdb_url>",
        "min_role": Role.TRUSTED,
        "description": "Request a movie by pasting its IMDb page link. The bot looks it up in Radarr; if it's not already in the library or already requested, it's posted for approval (or added immediately if you're an Admin/Owner).",
    },
    {
        "usage": "!requestTV <thetvdb_url>",
        "min_role": Role.TRUSTED,
        "description": "Request a TV show by pasting its TheTVDB page link. Same flow as !requestMovie, but through Sonarr.",
    },
    {
        "usage": "!status",
        "min_role": Role.TRUSTED,
        "description": "Shows your last 10 requests and their current download progress.",
    },
    {
        "usage": "✅ / ❌ react on a pending request",
        "min_role": Role.ADMIN,
        "description": "Approve or deny a request that's waiting on approval by reacting on its message.",
    },
    {
        "usage": "!trust @user [@user2 ...]",
        "min_role": Role.ADMIN,
        "description": "Marks the mentioned users as Trusted, letting them use !requestMovie, !requestTV, and !status.",
    },
    {
        "usage": "!untrust @user [@user2 ...]",
        "min_role": Role.ADMIN,
        "description": "Reverts the mentioned users from Trusted back to Guest. Admins/Owners are left unchanged.",
    },
    {
        "usage": "!pending",
        "min_role": Role.ADMIN,
        "description": "Lists every request that's currently awaiting approval.",
    },
    {
        "usage": "!makeAdmin @user [@user2 ...]",
        "min_role": Role.OWNER,
        "description": "Promotes the mentioned users to Admin, letting them approve/deny requests and trust new users. Requests from Admins and the Owner skip approval entirely.",
    },
    {
        "usage": "!setupHelp",
        "min_role": Role.ADMIN,
        "description": "Creates (or refreshes) the #help channel with this command reference.",
    },
    {
        "usage": "!setupStats",
        "min_role": Role.ADMIN,
        "description": "Creates (or refreshes) the #stats channel with the request leaderboard. Once created, it's kept current automatically.",
    },
]


def build_help_embed():
    embed = discord.Embed(
        title="📖 Plex Bot Commands",
        description="Here's everything the bot can do. Anything above your role will just reply \"Insufficient Permissions.\"",
        color=discord.Color.blurple(),
    )
    for doc in COMMAND_DOCS:
        embed.add_field(
            name=doc["usage"],
            value=f"{doc['description']}\n*Requires: {doc['min_role'].name}+*",
            inline=False,
        )
    return embed


@bot.command(name='setupHelp')
@require_role(Role.ADMIN)
async def setup_help(ctx):
    """Creates (or refreshes) a #help channel documenting every command."""
    guild = ctx.guild
    if guild is None:
        await ctx.channel.send("This command only works in a server.")
        return

    help_channel = discord.utils.get(guild.text_channels, name='help')

    if help_channel is None:
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(send_messages=False, view_channel=True),
            guild.me: discord.PermissionOverwrite(send_messages=True, view_channel=True),
        }
        try:
            help_channel = await guild.create_text_channel(
                'help', overwrites=overwrites, reason="Bot command documentation"
            )
        except discord.Forbidden:
            await ctx.channel.send("I don't have permission to create channels in this server.")
            return
    else:
        await help_channel.purge(limit=50, check=lambda m: m.author == bot.user)

    await help_channel.send(embed=build_help_embed())

    await ctx.channel.send(f"Done — check {help_channel.mention}")


# --- Stats ---

def compute_request_stats():
    items = []
    for status_value in ('unfinished', 'approved', 'denied'):
        response = table.query(
            IndexName='Status-index',
            KeyConditionExpression=Key('status').eq(status_value),
            FilterExpression=Attr('PK').begins_with('USER#')
        )
        items.extend(response.get('Items', []))

    by_status = Counter(item.get('status', 'unknown') for item in items)
    by_type = Counter(item.get('type', 'Unknown') for item in items)
    by_requester = Counter(item['PK'].split('#', 1)[1] for item in items)

    approved = by_status.get('approved', 0)
    denied = by_status.get('denied', 0)
    decided = approved + denied
    approval_rate = round((approved / decided) * 100) if decided else None

    return {
        'total': len(items),
        'by_status': by_status,
        'by_type': by_type,
        'top_requesters': by_requester.most_common(5),
        'approval_rate': approval_rate,
    }


def build_stats_embed():
    stats = compute_request_stats()

    embed = discord.Embed(
        title="📊 Plex Bot Stats",
        color=discord.Color.gold(),
    )
    embed.add_field(name="Total Requests", value=str(stats['total']), inline=True)
    embed.add_field(
        name="Movies / TV",
        value=f"{stats['by_type'].get('Movie', 0)} / {stats['by_type'].get('TV', 0)}",
        inline=True,
    )
    embed.add_field(
        name="Approval Rate",
        value=f"{stats['approval_rate']}%" if stats['approval_rate'] is not None else "No decisions yet",
        inline=True,
    )
    embed.add_field(
        name="Status Breakdown",
        value=(
            f"✅ Approved: {stats['by_status'].get('approved', 0)}\n"
            f"❌ Denied: {stats['by_status'].get('denied', 0)}\n"
            f"⏳ Pending: {stats['by_status'].get('unfinished', 0)}"
        ),
        inline=False,
    )

    if stats['top_requesters']:
        leaderboard = "\n".join(
            f"{i}. <@{user_id}> — {count}"
            for i, (user_id, count) in enumerate(stats['top_requesters'], start=1)
        )
    else:
        leaderboard = "No requests yet."
    embed.add_field(name="Top Requesters", value=leaderboard, inline=False)

    return embed


@bot.command(name='setupStats')
@require_role(Role.ADMIN)
async def setup_stats(ctx):
    """Creates (or refreshes) a #stats channel with the request leaderboard."""
    guild = ctx.guild
    if guild is None:
        await ctx.channel.send("This command only works in a server.")
        return

    stats_channel = discord.utils.get(guild.text_channels, name='stats')

    if stats_channel is None:
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(send_messages=False, view_channel=True),
            guild.me: discord.PermissionOverwrite(send_messages=True, view_channel=True),
        }
        try:
            stats_channel = await guild.create_text_channel(
                'stats', overwrites=overwrites, reason="Bot request stats"
            )
        except discord.Forbidden:
            await ctx.channel.send("I don't have permission to create channels in this server.")
            return
    else:
        await stats_channel.purge(limit=50, check=lambda m: m.author == bot.user)

    await stats_channel.send(embed=build_stats_embed())

    await ctx.channel.send(f"Done — check {stats_channel.mention}")


async def refresh_stats_channels():
    """Keeps any already-set-up #stats channel current. Does not create one --
    that's !setupStats's job -- only refreshes channels admins opted into."""
    for guild in bot.guilds:
        stats_channel = discord.utils.get(guild.text_channels, name='stats')
        if stats_channel is None:
            continue
        await stats_channel.purge(limit=50, check=lambda m: m.author == bot.user)
        await stats_channel.send(embed=build_stats_embed())


# --- Media request commands ---

@bot.command()
@require_role(Role.TRUSTED)
async def requestMovie(ctx, imdb_url: str):
    imdb_id = await asyncio.to_thread(get_imdb_id_from_url, imdb_url)

    if imdb_id is None:
        await ctx.channel.send("Could not find a IMDB ID on that page, please confirm that page is valid and try again")
        return

    media_key = {'PK': f'MEDIA#MOVIE#{imdb_id}', 'SK': 'REQUEST'}
    existing = table.get_item(Key=media_key).get('Item')

    if existing and existing.get('status') != 'denied':
        await ctx.channel.send("That movie has already been requested, please check the requested discord channel.")
        return

    try:
        movie = await asyncio.to_thread(radarr.get_movie, imdb_id=imdb_id)
    except NotFound:
        await ctx.channel.send("Radarr could not find a show with that IMDB ID.")
        return

    if movie.hasFile:
        await ctx.channel.send(f"{movie.title} is already in the library.")
        return

    requested_at = int(time.time())
    request_id = str(uuid.uuid4())

    status = "approved" if Role[get_user_role(ctx.author.id)] >= Role.ADMIN else "unfinished"

    if status == "unfinished":
        sent_message = await ctx.channel.send(f"{movie.title} is pending approval")
        await sent_message.add_reaction("✅")
        await sent_message.add_reaction("❌")
        table.put_item(Item={
            'PK': f'MESSAGE#{sent_message.id}',
            'SK': 'REQUEST',
            'mediaPK': media_key['PK']
        })
        await log_request_event(ctx.guild, f"📥 **{movie.title}** (Movie) requested by <@{ctx.author.id}> — pending approval")
    else:
        await ctx.channel.send(f"{movie.title} has been approved and queued")
        await log_request_event(ctx.guild, f"✅ **{movie.title}** (Movie) requested by <@{ctx.author.id}> — auto-approved")

    table.put_item(Item={
        **media_key,
        'title': movie.title,
        'status': status,
        'requestedBy': str(ctx.author.id),
        'requestId': request_id,
        'requestedAt': requested_at
    })

    table.put_item(Item={
        'PK': f'USER#{ctx.author.id}',
        'SK': f'REQUEST#{request_id}',
        'title': movie.title,
        'imdbID': imdb_id,
        'type': 'Movie',
        'status': status,
        'requestedAt': requested_at
    })


@bot.command()
@require_role(Role.TRUSTED)
async def requestTV(ctx, thetvdb_url: str):
    tvdb_id = await asyncio.to_thread(get_tvdb_id_from_url, thetvdb_url)

    if tvdb_id is None:
        await ctx.channel.send("Could not find a TVDB ID on that page, please confirm that page is valid and try again")
        return

    media_key = {'PK': f'MEDIA#TV#{tvdb_id}', 'SK': 'REQUEST'}
    existing = table.get_item(Key=media_key).get('Item')

    if existing and existing.get('status') != 'denied':
        await ctx.channel.send("That show has already been requested, please check the requested discord channel.")
        return

    try:
        series = await asyncio.to_thread(sonarr.get_series, tvdb_id=tvdb_id)
    except NotFound:
        await ctx.channel.send("Sonarr could not find a show with that TVDB ID.")
        return

    if series.episodeFileCount:
        await ctx.channel.send(f"{series.title} is already in the library.")
        return

    requested_at = int(time.time())
    request_id = str(uuid.uuid4())

    status = "approved" if Role[get_user_role(ctx.author.id)] >= Role.ADMIN else "unfinished"

    if status == "unfinished":
        sent_message = await ctx.channel.send(f"{series.title} is pending approval")
        await sent_message.add_reaction("✅")
        await sent_message.add_reaction("❌")
        table.put_item(Item={
            'PK': f'MESSAGE#{sent_message.id}',
            'SK': 'REQUEST',
            'mediaPK': media_key['PK']
        })
        await log_request_event(ctx.guild, f"📥 **{series.title}** (TV) requested by <@{ctx.author.id}> — pending approval")
    else:
        await ctx.channel.send(f"{series.title} has been approved and queued")
        await log_request_event(ctx.guild, f"✅ **{series.title}** (TV) requested by <@{ctx.author.id}> — auto-approved")

    table.put_item(Item={
        **media_key,
        'title': series.title,
        'status': status,
        'requestedBy': str(ctx.author.id),
        'requestId': request_id,
        'requestedAt': requested_at
    })

    table.put_item(Item={
        'PK': f'USER#{ctx.author.id}',
        'SK': f'REQUEST#{request_id}',
        'title': series.title,
        'tvdbID': tvdb_id,
        'type': 'TV',
        'status': status,
        'requestedAt': requested_at
    })


@bot.command()
@require_role(Role.TRUSTED)
async def status(ctx):
    """Shows current download progress for the last 10 requested items."""
    items = []
    for status_value in ('unfinished', 'approved', 'denied'):
        response = table.query(
            IndexName='Status-index',
            KeyConditionExpression=Key('status').eq(status_value),
            FilterExpression=Attr('PK').begins_with('USER#'),
            ScanIndexForward=False
        )
        items.extend(response.get('Items', []))

    if not items:
        await ctx.channel.send("No requests yet.")
        return

    items.sort(key=lambda item: item['requestedAt'], reverse=True)
    items = items[:10]

    needs_radarr_queue = any(item.get('status') == 'approved' and item.get('type') == 'Movie' for item in items)
    needs_sonarr_queue = any(item.get('status') == 'approved' and item.get('type') == 'TV' for item in items)

    radarr_queue_records = []
    sonarr_queue_records = []

    if needs_radarr_queue:
        radarr_queue = await asyncio.to_thread(radarr._raw._get, "queue", pageSize=250)
        radarr_queue_records = radarr_queue.get('records', [])

    if needs_sonarr_queue:
        sonarr_queue = await asyncio.to_thread(sonarr._raw._get, "queue", pageSize=250)
        sonarr_queue_records = sonarr_queue.get('records', [])

    def format_progress(size, sizeleft):
        percent = round((1 - sizeleft / size) * 100) if size > 0 else 0
        size_gb = size / (1024 ** 3)
        left_gb = sizeleft / (1024 ** 3)
        downloaded_gb = size_gb - left_gb
        return f"⬇️ {percent}% ({downloaded_gb:.1f}GB / {size_gb:.1f}GB)"

    lines = ["**Recent Requests**"]

    for i, item in enumerate(items, start=1):
        title = item.get('title', 'Unknown')
        media_type = item.get('type', 'Unknown')

        if item.get('status') == 'unfinished':
            progress = "⏳ Pending approval"
        elif item.get('status') == 'denied':
            progress = "❌ Denied"
        elif media_type == 'Movie':
            try:
                movie = await asyncio.to_thread(radarr.get_movie, imdb_id=item.get('imdbID'))
            except NotFound:
                progress = "⚠️ Not found in Radarr"
            else:
                if movie.hasFile:
                    progress = "✅ Downloaded"
                else:
                    record = next((r for r in radarr_queue_records if r.get('movieId') == movie.id), None)
                    if record:
                        progress = format_progress(record.get('size', 0), record.get('sizeleft', 0))
                    else:
                        progress = "🔍 Searching / not yet downloading"
        elif media_type == 'TV':
            try:
                series = await asyncio.to_thread(sonarr.get_series, tvdb_id=item.get('tvdbID'))
            except NotFound:
                progress = "⚠️ Not found in Sonarr"
            else:
                record = next((r for r in sonarr_queue_records if r.get('seriesId') == series.id), None)
                if record:
                    progress = format_progress(record.get('size', 0), record.get('sizeleft', 0))
                elif series.episodeFileCount:
                    progress = "✅ Downloaded"
                else:
                    progress = "🔍 Searching / not yet downloading"
        else:
            progress = "⚠️ Unknown request type"

        lines.append(f"{i}. {title} ({media_type}) — {progress}")

    await ctx.channel.send("\n".join(lines))


@bot.command(name='pending')
@require_role(Role.ADMIN)
async def pending(ctx):
    """Lists every request currently awaiting approval."""
    response = table.query(
        IndexName='Status-index',
        KeyConditionExpression=Key('status').eq('unfinished'),
        FilterExpression=Attr('PK').begins_with('USER#'),
        ScanIndexForward=False
    )
    items = response.get('Items', [])

    if not items:
        await ctx.channel.send("No requests are pending approval.")
        return

    items.sort(key=lambda item: item['requestedAt'], reverse=True)

    lines = ["**Pending Approval**"]
    for i, item in enumerate(items, start=1):
        requester_id = item['PK'].split('#', 1)[1]
        title = item.get('title', 'Unknown')
        media_type = item.get('type', 'Unknown')
        lines.append(f"{i}. {title} ({media_type}) — requested by <@{requester_id}>")

    await ctx.channel.send("\n".join(lines))


# --- Availability notifications & stale-request cleanup ---

async def check_available_requests():
    """Finds approved requests that have finished downloading and haven't
    been announced yet, posts to #request-log, and marks them notified."""
    response = table.query(
        IndexName='Status-index',
        KeyConditionExpression=Key('status').eq('approved'),
        FilterExpression=Attr('PK').begins_with('USER#') & Attr('notifiedAvailable').not_exists()
    )

    for item in response.get('Items', []):
        media_type = item.get('type')

        if media_type == 'Movie':
            try:
                movie = await asyncio.to_thread(radarr.get_movie, imdb_id=item.get('imdbID'))
            except NotFound:
                continue
            available = movie.hasFile
        elif media_type == 'TV':
            try:
                series = await asyncio.to_thread(sonarr.get_series, tvdb_id=item.get('tvdbID'))
            except NotFound:
                continue
            available = bool(series.episodeFileCount)
        else:
            continue

        if not available:
            continue

        requester_id = item['PK'].split('#', 1)[1]
        title = item.get('title', 'Unknown')

        for guild in bot.guilds:
            await log_request_event(guild, f"🎬 **{title}** is now available on Plex! (requested by <@{requester_id}>)")

        table.update_item(
            Key={'PK': item['PK'], 'SK': item['SK']},
            UpdateExpression="SET notifiedAvailable = :true_val",
            ExpressionAttributeValues={':true_val': True}
        )


async def sweep_stale_requests():
    """Flags requests that have sat unapproved past the staleness threshold
    with a near-term DynamoDB TTL, so they eventually clean themselves up."""
    cutoff = int(time.time()) - STALE_REQUEST_THRESHOLD_DAYS * SECONDS_PER_DAY

    response = table.query(
        IndexName='Status-index',
        KeyConditionExpression=Key('status').eq('unfinished'),
        FilterExpression=Attr('PK').begins_with('USER#') & Attr('expiresAt').not_exists()
    )

    for item in response.get('Items', []):
        if item.get('requestedAt', 0) >= cutoff:
            continue

        expires_at = int(time.time()) + STALE_ITEM_TTL_DAYS * SECONDS_PER_DAY

        table.update_item(
            Key={'PK': item['PK'], 'SK': item['SK']},
            UpdateExpression="SET expiresAt = :expires_at",
            ExpressionAttributeValues={':expires_at': expires_at}
        )

        media_pk = media_pk_for_request(item)
        if media_pk:
            table.update_item(
                Key={'PK': media_pk, 'SK': 'REQUEST'},
                UpdateExpression="SET expiresAt = :expires_at",
                ExpressionAttributeValues={':expires_at': expires_at}
            )


@tasks.loop(minutes=MAINTENANCE_INTERVAL_MINUTES)
async def periodic_maintenance():
    try:
        await check_available_requests()
    except Exception as e:
        print("ERROR in check_available_requests:", e)

    try:
        await sweep_stale_requests()
    except Exception as e:
        print("ERROR in sweep_stale_requests:", e)

    try:
        await refresh_stats_channels()
    except Exception as e:
        print("ERROR in refresh_stats_channels:", e)


# --- Entry point ---

if __name__ == "__main__":
    bot.run(Discord_token, log_handler=handler, log_level=logging.DEBUG)