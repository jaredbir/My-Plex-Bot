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

load_dotenv()
token = os.getenv('DISCORD_TOKEN')

handler = logging.FileHandler(filename='discord.log', encoding='utf-8', mode='w')
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)

dynamodb = boto3.resource('dynamodb',region_name = 'us-east-1')
table = dynamodb.Table('discord-bot-requests')

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


# print(table.query(IndexName='Status-index',KeyConditionExpression=Key('status').eq('unfinished')))


class Role(IntEnum):
    GUEST = 0
    TRUSTED = 1
    ADMIN = 2
    OWNER = 3


def set_role(discord_id,role):
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


def require_role(minimum_role):
    def decorator(func):
        @wraps(func)
        async def wrapper(ctx, *args, **kwargs):
            print("wrapper entered")
            try:
                user_role_str = get_user_role(ctx.author.id)
                print("got role string:", user_role_str)
            except Exception as e:
                print("ERROR in get_user_role:", e)
                return
            user_role = Role[user_role_str]
            print("converted to enum:", user_role)

            if user_role < minimum_role:
                print("permission check failed, sending rejection")
                await ctx.channel.send("Insufficient Permissions")
                return
            print("permission check passed, calling real function")
            return await func(ctx, *args, **kwargs)
        return wrapper
    return decorator
 

@bot.event
async def on_ready():
    print(f"{bot.user.name} is ready!")
    


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    await bot.process_commands(message)


@bot.command()
@require_role(Role.ADMIN)
async def trust(ctx):
    """All mentioned users will become trusted members, and will be able to make requests"""
    for member in ctx.message.mentions:
        set_role(member.id,Role.TRUSTED)

    await ctx.channel.send("Mentioned Users are now Trusted")

   
@bot.command()
@require_role(Role.OWNER)
async def makeAdmin(ctx):
    """All mentioned users will become admins, and will be able to appoint trusted users and approve trusted users requests."""
    for member in ctx.message.mentions:
        set_role(member.id,Role.ADMIN)


bot.run(token, log_handler=handler, log_level=logging.DEBUG)