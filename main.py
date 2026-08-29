import discord
from discord.ext import commands
from arrapi import SonarrAPI, RadarrAPI
import logging
from dotenv import load_dotenv
import os
import boto3
from enum import IntEnum
from functools import wraps
from boto3.dynamodb.conditions import Key
from requests import Session
import re
import requests
import asyncio
import time
import uuid
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


# --- Bot events ---

@bot.event
async def on_ready():
    print(f"{bot.user.name} is ready!")


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
    else:
        table.update_item(
            Key={'PK': message_item['mediaPK'], 'SK': 'REQUEST'},
            UpdateExpression="SET #s = :new_status, deniedBy = :denier",
            ExpressionAttributeNames={'#s': 'status'},
            ExpressionAttributeValues={
                ':new_status': 'denied',
                ':denier': str(payload.user_id)
            }
        )
        table.update_item(
            Key=user_request_key,
            UpdateExpression="SET #s = :new_status, deniedBy = :denier",
            ExpressionAttributeNames={'#s': 'status'},
            ExpressionAttributeValues={
                ':new_status': 'denied',
                ':denier': str(payload.user_id)
            }
        )


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
@require_role(Role.OWNER)
async def makeAdmin(ctx):
    """All mentioned users will become admins, and will be able to appoint trusted users and approve trusted users requests."""
    try:
        ctx.message.mentions.remove(ctx.message.author)
    except ValueError:
        pass
    for member in ctx.message.mentions:
        set_role(member.id, Role.ADMIN)

    await ctx.channel.send("Mentioned Users are now Admins")


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
    else:
        await ctx.channel.send(f"{movie.title} has been approved and queued")

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
    else:
        await ctx.channel.send(f"{series.title} has been approved and queued")

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


# --- Entry point ---

if __name__ == "__main__":
    bot.run(Discord_token, log_handler=handler, log_level=logging.DEBUG)