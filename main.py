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

dynamodb = boto3.resource('dynamodb',region_name = 'us-east-1')
table = dynamodb.Table('discord-bot-requests')

table.put_item(
    Item={
        'PK': 'USER#12345',
        'SK': 'PROFILE',
        'role': 'TRUSTED'
    }
)

table.put_item(
    Item={
        'PK': 'USER#12345',
        'SK': 'REQUEST#abc',
        'title': 'test',
        'status': 'unfinished',
        'requestedAt': 1785305756
    }
)

print(table.query(IndexName='Status-index',KeyConditionExpression=Key('status').eq('unfinished')))

class Role(IntEnum):
    GUEST = 0
    TRUSTED = 1
    ADMIN = 2
    OWNER = 3


def get_user_role(discord_id):
    response = table.get_item(Key={'PK': 'USER#' + str(discord_id), 'SK': 'PROFILE'})
    return response.get('Item', {}).get('role', 'GUEST')


def require_role(minimum_role):
    def decorator(func):
        @wraps(func)
        async def wrapper(ctx, *args, **kwargs):
            user_role_str = get_user_role(ctx.author.id)
            user_role = Role[user_role_str]

            if user_role < minimum_role:
                await ctx.channel.send("Insufficient Permissions")
                return
            return await func(ctx,*args,**kwargs)
        return wrapper
    return decorator
 
                     
bot = commands.Bot(command_prefix='!', intents=intents)

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
    for member in ctx.message.mentions:
        table.put_item(
            Item={
                'PK': 'USER#' + str(member.id),
                'SK': 'PROFILE',
                'role': 'TRUSTED'
            }
        )

   
@bot.command()
@require_role(Role.OWNER)
async def makeAdmin(ctx):
    for member in ctx.message.mentions:
            table.put_item(
                Item={
                    'PK': 'USER#' + str(member.id),
                    'SK': 'PROFILE',
                    'role': 'ADMIN'
                }
            )


bot.run(token, log_handler=handler, log_level=logging.DEBUG)