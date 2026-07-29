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
async def trust(ctx, *, message:str, member:discord.Member):
    if message.author == ctx.guild.owner and message.mentions:
        return
    
@bot.command()
async def makeAdmin(ctx, *, message:str, member:discord.Member):
    if message.author == ctx.guild.owner and message.mentions:
        return


bot.run(token, log_handler=handler, log_level=logging.DEBUG)