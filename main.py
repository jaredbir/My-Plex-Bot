import discord
from discord.ext import commands
from arrapi import SonarrAPI
import logging
from dotenv import load_dotenv
import os
import boto3
from enum import IntEnum
from functools import wraps
from boto3.dynamodb.conditions import Key

# --- Configuration & setup ---

# Load environment variables from .env (expects DISCORD_TOKEN)
load_dotenv()
token = os.getenv('DISCORD_TOKEN')

# Log bot activity to a file, overwriting on each run
handler = logging.FileHandler(filename='discord.log', encoding='utf-8', mode='w')

# Configure intents: need message content to read command args,
# and members to resolve mentioned users' roles
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

# Create the bot with "!" as the command prefix
bot = commands.Bot(command_prefix='!', intents=intents)

# Connect to the DynamoDB table used to store user roles and media requests
dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
table = dynamodb.Table('discord-bot-requests')


# --- Domain model ---

# Role hierarchy, ordered so comparisons like "user_role < minimum_role" work
class Role(IntEnum):
    GUEST = 0
    TRUSTED = 1
    ADMIN = 2
    OWNER = 3


# --- Data access helpers ---

def set_role(discord_id, role):
    """Write/overwrite a user's role in DynamoDB (PK=USER#<id>, SK=PROFILE)."""
    table.put_item(
        Item={
            'PK': 'USER#' + str(discord_id),
            'SK': 'PROFILE',
            'role': str(role.name)
        }
    )


def get_user_role(discord_id):
    """Fetch a user's role from DynamoDB, defaulting to 'GUEST' if no record exists."""
    response = table.get_item(Key={'PK': 'USER#' + str(discord_id), 'SK': 'PROFILE'})
    return response.get('Item', {}).get('role', 'GUEST')


# --- Permission decorator ---

def require_role(minimum_role):
    """
    Decorator factory for command permission checks.
    Wraps a command so it only runs if the invoking user's role
    meets or exceeds `minimum_role`; otherwise replies with an error.
    """
    def decorator(func):
        @wraps(func)  # preserves func's name/docstring for discord.py's help command
        async def wrapper(ctx, *args, **kwargs):
            try:
                user_role_str = get_user_role(ctx.author.id)
            except Exception as e:
                # DynamoDB lookup failed - fail closed and let the user know
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
    # Fires once the bot has connected and is ready to receive events
    print(f"{bot.user.name} is ready!")


@bot.event
async def on_message(message):
    # Ignore the bot's own messages to avoid feedback loops,
    # then hand off to the command processor
    if message.author == bot.user:
        return

    await bot.process_commands(message)


# --- Role management commands ---

@bot.command()
@require_role(Role.ADMIN)
async def trust(ctx):
    """All mentioned users will become trusted members, and will be able to make requests"""
    # Only promote users who are currently GUEST or TRUSTED (i.e. don't
    # downgrade an ADMIN/OWNER by accident via this command)
    for member in ctx.message.mentions:
        if Role[get_user_role(member.id)] <= Role.TRUSTED:
            set_role(member.id, Role.TRUSTED)

    await ctx.channel.send("Mentioned Users are now Trusted")


@bot.command()
@require_role(Role.OWNER)
async def makeAdmin(ctx):
    """All mentioned users will become admins, and will be able to appoint trusted users and approve trusted users requests."""
    # Don't let the command author accidentally include themselves
    # in the mentions list (e.g. self-mention in the message)
    try:
        ctx.message.mentions.remove(ctx.message.author)
    except ValueError:
        pass
    for member in ctx.message.mentions:
        set_role(member.id, Role.ADMIN)


# --- Media request commands ---

@bot.command()
@require_role(Role.TRUSTED)
async def requestMovie(ctx):
    # TODO: implement movie request logic (e.g. Radarr lookup + DynamoDB request entry)
    pass


@bot.command()
@require_role(Role.TRUSTED)
async def requestTV(ctx):
    # TODO: implement TV request logic (e.g. SonarrAPI lookup + DynamoDB request entry)
    pass


@bot.command()
@require_role(Role.TRUSTED)
async def status(ctx):
    # TODO: implement status check (e.g. query Status-index GSI for this user's requests)
    pass


# --- Manual/debug reference snippets (not executed) ---

# One-off item inserts, kept for reference when seeding data by hand:
# table.put_item(
#     Item={
#         'PK': 'USER#171047114611228672',
#         'SK': 'PROFILE',
#         'role': 'OWNER'
#     }
# )

# table.put_item(
#     Item={
#         'PK': 'USER#12345',
#         'SK': 'REQUEST#abc',
#         'title': 'test',
#         'status': 'unfinished',
#         'requestedAt': 1785305756
#     }
# )

# Example query using the Status-index GSI to find unfinished requests:
# print(table.query(IndexName='Status-index', KeyConditionExpression=Key('status').eq('unfinished')))


# --- Entry point ---

bot.run(token, log_handler=handler, log_level=logging.DEBUG)