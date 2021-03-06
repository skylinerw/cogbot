import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

import feedparser
from dateutil.parser import parse as dateutil_parse
from discord import Channel
from discord.ext import commands
from discord.ext.commands import Context

from cogbot import checks
from cogbot.cog_bot import CogBot

log = logging.getLogger(__name__)


class FeedSubscription:
    def __init__(self, name: str, url: str, recency: int = 0):
        self.name = name
        self.url = url
        self.recency = recency

        self.last_datetime = datetime.now(timezone.utc)
        self.last_titles = set()
        self.last_ids = set()

        if self.recency:
            self.last_datetime -= timedelta(seconds=self.recency)

    def update(self):
        try:
            # parse feed and datetime
            data = feedparser.parse(self.url)
            channel_datetime = dateutil_parse(data.feed.updated).astimezone(timezone.utc)

            # keep a record of last-updated articles to help eliminate duplication
            next_last_titles = set()
            next_last_ids = set()

            # don't bother checking entries unless the entire feed has been updated since our last check
            if channel_datetime > self.last_datetime:

                for entry in data.entries:
                    # try first the published datetime, then updated datetime
                    entry_datetime = dateutil_parse(entry.get('published', entry.updated))
                    entry_id = entry.id or entry.get('id', entry.get('guid'))
                    entry_title = entry.title or entry.get('title')

                    # calculate whether the entry is fresh/new
                    is_fresh = (entry_datetime > self.last_datetime) \
                               and (entry_title not in next_last_titles) \
                               and (entry_title not in self.last_titles) \
                               and (entry_id not in next_last_ids) \
                               and (entry_id not in self.last_ids)

                    # if it is fresh, add it to the records for the next iteration... and yield
                    if is_fresh:
                        if entry_title:
                            next_last_titles.add(entry_title)
                        if entry_id:
                            next_last_ids.add(entry_id)
                        yield entry

            # update timestamp and records for future iterations
            self.last_datetime = channel_datetime
            self.last_titles = next_last_titles
            self.last_ids = next_last_ids

        except:
            log.exception(f'Failed to parse feed at: {self.url}')


class Feed:
    DEFAULT_POLLING_INTERVAL = 60

    SubscriptionType = Dict[str, Dict[str, FeedSubscription]]

    def __init__(self, bot: CogBot, ext: str):
        self.bot = bot

        self.options = bot.state.get_extension_state(ext)

        self.polling_interval: int = self.options.get('polling_interval', self.DEFAULT_POLLING_INTERVAL)

        # Access like so: self._subscriptions[channel_id][name]
        self._subscriptions: Feed.SubscriptionType = {}

        self.polling_task: Optional[asyncio.Task] = None

    async def on_ready(self):
        log.info('Ready event received; proceeding to initial reset...')
        self._reset()

    def _reset(self, intentional=False):
        log.info('Resetting subscriptions and polling task...')

        # Initialize subscriptions.

        # Clear any existing subscriptions.
        self._subscriptions = {}

        raw_subscriptions = self.options.get('subscriptions', {})

        log.info(f'Initializing subscriptions for {len(raw_subscriptions)} channels...')

        for channel_id, v in raw_subscriptions.items():
            channel = self.bot.get_channel(channel_id)
            for name, data in v.items():
                url = data['url']
                recency = data.get('recency')
                try:
                    self._add_feed(channel, name, url, recency)
                except:
                    log.exception(f'Failed to add initial feed {name} at: {url}')

        # If a polling task does not yet exist, create a new one.
        if self.polling_task is None:
            polling_task = self._create_polling_task()
            log.info(f'Created a new polling task (hash: {hash(polling_task)}) for the first time')
            self.polling_task = polling_task

        # If one does already exist, but it has cancelled and/or completed, then create a new one.
        elif self._should_create_polling_task():
            polling_task = self._create_polling_task()
            log.info(
                f'Created a new polling task (hash: {hash(polling_task)}) because the previous one was '
                f'cancelled and/or completed')
            self.polling_task = polling_task

        # If the reset was intentional, we can safely keep the existing polling task running.
        elif intentional:
            log.info(f'Keeping existing polling task running (hash: {hash(self.polling_task)}) after intentional reset')

        # Otherwise, warn about the existing polling task that's still running and could pose a problem.
        else:
            log.warning(
                f'Not creating a new polling task because the previous one is still running '
                f'(hash: {hash(self.polling_task)})')

    async def _loop_poll(self):
        while self.bot.is_logged_in:
            await self.update_all_feeds()
            await asyncio.sleep(self.polling_interval)
        log.info('Bot logged out, polling loop terminated')

    def _should_create_polling_task(self) -> bool:
        return isinstance(self.polling_task, asyncio.Task) and (
                self.polling_task.done() or self.polling_task.cancelled())

    def _create_polling_task(self) -> asyncio.Task:
        return self.bot.loop.create_task(self._loop_poll())

    def _add_feed(self, channel: Channel, name: str, url: str, recency: int = None):
        # Don't add the same subscription more than once.
        try:
            if name in self._subscriptions[channel.id]:
                log.warning(
                    f'[{channel.server.name}#{channel.name}] Tried to subscribe to feed {name} more than once at: {sub.url}')
                return
        except:
            pass

        sub = FeedSubscription(name, url, recency)

        if channel.id not in self._subscriptions:
            self._subscriptions[channel.id] = {}

        subs = self._subscriptions[channel.id]

        log.info(f'[{channel.server.name}#{channel.name}] Subscribing to feed {name} at: {sub.url}')

        subs[name] = sub

    def _remove_feed(self, channel: Channel, name: str):
        subs = self._subscriptions[channel.id]
        sub = subs[name]

        log.info(f'[{channel.server.name}#{channel.name}] Unsubscribing from feed {name} at: {sub.url}')

        del subs[name]

    async def _update_feed(self, channel: Channel, name: str):
        subs = self.subscriptions[channel.id]
        sub = subs[name]

        # this is where we get serious
        # grab the last couple messages in the channel to make sure we aren't posting a dupe
        recent_contents = []
        async for message in self.bot.logs_from(channel, limit=2):
            recent_contents.append(message.content)

        fresh_entries = tuple(sub.update())
        if fresh_entries:
            log.info(f'Found {len(fresh_entries)} new posts for feed at: {sub.url}')
            for entry in fresh_entries:
                # but is it really fresh?
                # scan content of recent messages and match title to make sure
                really_fresh = True
                for recent_content in recent_contents:
                    if entry.title in recent_content:
                        really_fresh = False
                        break

                if really_fresh:
                    log.info(f'Posting fresh update for feed {name}: {entry.title}')
                    message = f'**{entry.title}**\n{entry.link}'
                    await self.bot.send_message(channel, message)
                else:
                    log.info(f'Skipping stale update for feed {name}: {entry.title}')

        # and this is where we get really serious
        # forcibly delete any dupes... in case they still manage to slip through
        # do this even if we don't post anything, because dupes are a mystery
        # go through the last several messages *again* and make sure we didn't post any dupes
        # go in reverse and delete any dupes from the oldest message onward
        content_hash = set()
        messages = []
        async for message in self.bot.logs_from(channel, limit=10):
            messages.append(message)
        for message in reversed(messages):
            if (message.author == self.bot) and (message.content in content_hash):
                message_title = message.content.split('\n')[0]
                log.warning(f'Deleting duped message for feed {name}: {message_title}')
                await self.bot.delete_message(message)
            else:
                content_hash.add(message.content)

    async def add_feed(self, ctx: Context, name: str, url: str, recency: int = None):
        channel = ctx.message.channel
        subs = self.subscriptions.get(channel.id)

        if name not in subs:
            try:
                self._add_feed(channel, name, url, recency)
                await self.bot.react_success(ctx)
            except:
                log.exception(f'Failed to add new feed {name} at: {url}')
                await self.bot.react_failure(ctx)
        else:
            await self.bot.react_neutral(ctx)

    async def remove_feed(self, ctx: Context, name: str):
        channel = ctx.message.channel
        subs = self.subscriptions.get(channel.id)

        if name in subs:
            self._remove_feed(channel, name)
            await self.bot.react_success(ctx)

        else:
            await self.bot.react_neutral(ctx)

    async def list_feeds(self, ctx: Context):
        channel = ctx.message.channel
        subs = self.subscriptions.get(channel.id)

        if subs:
            subs_str = '\n'.join([f'  - {name}: {sub.url}' for name, sub in subs.items()])
            reply = f'Subscribed feeds:\n{subs_str}'

        else:
            reply = f'No subscribed feeds.'

        await self.bot.send_message(ctx.message.channel, reply)

    async def update_feeds(self, ctx: Context, *names):
        """ Update only the given feeds for the channel in context. """
        channel = ctx.message.channel
        for name in names:
            await self._update_feed(channel, name)
        await self.bot.react_success(ctx)

    async def update_all_feeds(self):
        """ Update all feeds for all channels. """
        for channel_id, subs in self.subscriptions.items():
            channel = self.bot.get_channel(channel_id)
            for name, sub in subs.items():
                await self._update_feed(channel, name)

    @property
    def subscriptions(self) -> 'Feed.SubscriptionType':
        return self._subscriptions

    @checks.is_manager()
    @commands.group(pass_context=True, name='feed', hidden=True)
    async def cmd_feed(self, ctx: Context):
        if ctx.invoked_subcommand is None:
            await self.list_feeds(ctx)

    @cmd_feed.command(pass_context=True, name='add')
    async def cmd_feed_add(self, ctx: Context, name: str, url: str, recency: int = None):
        await self.add_feed(ctx, name, url, recency)

    @cmd_feed.command(pass_context=True, name='remove')
    async def cmd_feed_remove(self, ctx: Context, name: str):
        await self.remove_feed(ctx, name)

    @cmd_feed.command(pass_context=True, name='list')
    async def cmd_feed_list(self, ctx: Context):
        await self.list_feeds(ctx)

    @cmd_feed.command(pass_context=True, name='update')
    async def cmd_feed_update(self, ctx: Context, *names):
        if not names:
            channel = ctx.message.channel
            subs = self.subscriptions[channel.id]
            names = subs.keys()
        await self.update_feeds(ctx, *names)

    @cmd_feed.command(pass_context=True, name='reset')
    async def cmd_feed_reset(self, ctx: Context):
        self._reset(intentional=True)
        await self.bot.react_success(ctx)


def setup(bot):
    bot.add_cog(Feed(bot, __name__))
