# Source: https://github.com/Rapptz/discord.py/blob/master/examples/basic_voice.py

import asyncio
import random
import re
import typing
import traceback
import json
import queue
import time
import threading
import os
import sys
import shlex
from collections import deque

import discord
from discord.ext import commands
from discord.ext import tasks

import yt_dlp as youtube_dl

import jgm.patched_player as patched_player
import soundit as s

class Music(commands.Cog):
    # Options that are passed to youtube-dl
    _DEFAULT_YTDL_OPTS = {
        'format': 'bestaudio/best',
        'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
        'restrictfilenames': True,
        'noplaylist': True,
        'playlistend': 1,
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'logtostderr': False,
        'quiet': True,
        'no_warnings': True,
        'default_search': 'auto',
        'source_address': '0.0.0.0', # bind to ipv4 since ipv6 addresses cause issues sometimes
    }
    # Options passed to FFmpeg
    _STREAM_FFMPEG_OPTS = {
        'options': '-vn',
        # Source: https://stackoverflow.com/questions/66070749/
        "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    }
    _LOCAL_FFMPEG_OPTS = {
        "options": "-vn",  # Filter out video
        "before_options": ""
    }
    # Filters
    _FILTERS = {
        "bassboost": "bass=g=20",
        "deepfry": "'acrusher=level_in=8:level_out=18:bits=8:mode=log:aa=1'",  # Source: https://www.vacing.com/ffmpeg_audio_filters/index.html
        "nightcore": "asetrate=48000*1.25,aresample=48000",
        "daycore": "asetrate=48000*0.75,aresample=48000",
        "amogus": "asetrate=48000*0.75,aresample=48000,atempo=1/0.75"
    }

# -filter_complex "acrusher=level_in=8:level_out=18:bits=8:mode=log:aa=1"

    def __init__(
        self,
        bot,
        *,
        ytdl_opts=_DEFAULT_YTDL_OPTS,
        ffmpeg_opts=_STREAM_FFMPEG_OPTS,
    ):
        self.bot = bot
        # Options are stores on the instance in case they need to be changed
        self.ytdl_opts = ytdl_opts
        self.ffmpeg_opts = ffmpeg_opts

        # Data is persistent between extension reloads
        if not hasattr(bot, "_music_data"):
            bot._music_data = {}
        if not hasattr(bot, "_music_advance_queue"):
            bot._music_advance_queue = asyncio.Queue()
        self.data = bot._music_data
        self.advance_queue = bot._music_advance_queue
        # Start the advancer's auto-restart task
        self.advance_task = None
        self.advancer.start()
        self.current_audio_stream = None
        self.current_audio_link = None
        self.current_metadata = {
            "is_live": None,
            "duration": None,
            "title": None,
            "id": None,
            "webpage_url": None
        }
        # TODO init is not run once when reloaded


        self.task = None

    # Cancel just the advancer and the auto-restart tasks
    def cog_unload(self):
        self.advancer.cancel()

    # - Song players
    # Returns a source object and the title of the song

    # Finds a file using query. Title is query
    async def _play_local(self, ctx, query):
        # Runs when new song
        info = self.get_info(ctx)
        self.ffmpeg_opts = self._LOCAL_FFMPEG_OPTS
        new_ffmpeg_opts = self.apply_filters(ctx, self.ffmpeg_opts.copy())
        speed = info["cur_speed_filter"]
        filter = info["cur_audio_filter"]
        source = discord.PCMVolumeTransformer(patched_player.FFmpegPCMAudio(query, speed, filter=filter, **new_ffmpeg_opts))
        self.current_audio_link = query
        return source, query

    # Searches various sites using url. Title is data["title"] or url
    async def _play_stream(self, ctx, url):
        original_url = url
        if url[0] == "<" and url[-1] == ">":
            url = url[1:-1]
        player, data = await self.player_from_url(ctx, url, stream=True)
        return player, data.get("title", original_url)

    # Returns the raw source (calling the function if possible)
    async def _play_raw(self, source):
        if callable(source):
            source = source()
        if not isinstance(source, discord.AudioSource):
            source = s.wrap_discord_source(s.chunked(source))
        return source, repr(source)

    # Auto-restart task for the advancer task
    @tasks.loop(seconds=15)
    async def advancer(self):
        if self.advance_task is not None and self.advance_task.done():
            try:
                exc = self.advance_task.exception()
            except asyncio.CancelledError:
                pass
            else:
                print("Exception occured in advancer task:")
                traceback.print_exception(None, exc, exc.__traceback__)
            self.advance_task = None
        if self.advance_task is None:
            self.advance_task = asyncio.create_task(self.handle_advances(), name="music_advancer")

    # Cancel the advancer task if the monitoring task is getting cancelled
    # (such as when the cog is getting unloaded)
    @advancer.after_loop
    async def on_advancer_cancel(self):
        print("after the coro is run ")
        if self.advancer.is_being_cancelled():
            if self.advance_task is not None:
                self.advance_task.cancel()
                self.advance_task = None

    # The advancer task loop
    async def handle_advances(self):
        i = 0
        while True:
            item = await self.advance_queue.get()
            print("__iteration " + str(i))
            asyncio.create_task(self.handle_advance(item))
            print("iteration " + str(i))
            i += 1

    # The actual music advancing logic
    async def handle_advance(self, item):
        ctx, error = item
        info = self.get_info(ctx)
        channel = ctx.guild.get_channel(info["channel_id"])
        try:
            # If we are processing it right now...
            if info["processing"]:
                # Wait a bit and reschedule it again
                await asyncio.sleep(1)
                self.advance_queue.put_nowait(item)
                return
            info["processing"] = True
            # If there's an error, send it to the channel
            if error is not None:
                await channel.send(f"Player error: {error!r}")
            # If we aren't connected anymore, notify and leave
            if ctx.voice_client is None:
                await channel.send("Not connected to a voice channel anymore")
                await self.leave(ctx)
                return
            queue = info["queue"]
            history = info["history"]
            # If we're looping, put the current song at the end of the queue
            if info["current"] is not None:
                if info["loop"] == -1:
                    queue.appendleft(info["current"])
                elif info["loop"] == 1:
                    queue.append(info["current"])
            # if not info["jumped"]:  # If wasn't jumped, run if False
                history.append(info["current"])
            info["current"] = None

            # Prioritizing jump over queue message
            # if info["jumped"]:  # Was a jump
            #     info["jumped"] = False
            if queue:
                # Get the next song
                current = queue.popleft()
                info["current"] = current
                # Get an audio source and play it
                after = lambda error, ctx=ctx: self.schedule(ctx, error)
                async with channel.typing():
                    source, title = await getattr(self, f"_play_{current['ty']}")(ctx, current['query'])
                    # print(source, title)
                    self.current_audio_stream = source
                    ctx.voice_client.play(source, after=after)
                await channel.send(f"Now playing: {title}")
            else:
                await channel.send(f"Queue empty")
        except Exception as e:
            traceback.print_exception(type(e), e, e.__traceback__)
            await channel.send(f"Internal Error: {e!r}")
            info["waiting"] = False
            await self.skip(ctx)
            self.schedule(ctx)
        finally:
            info["waiting"] = False
            info["processing"] = False

    @commands.command()
    async def e(self, ctx):
        info = self.get_info(ctx)
        channel = ctx.guild.get_channel(info["channel_id"])
        async with channel.typing():
            time.sleep(5)
        await ctx.send("ayo bruh")

    #  advancement of the queue
    def schedule(self, ctx, error=None, *, force=False):
        info = self.get_info(ctx)
        self.current_audio_stream = None
        self.current_audio_link = None

        for k in self.current_metadata:
            self.current_metadata[k] = None

        if info["autoshuffle"]:
            self._shuffle(ctx)
        if force or not info["waiting"]:
            self.advance_queue.put_nowait((ctx, error))
            info["waiting"] = True

    # Helper function to create the info for a guild
    def get_info(self, ctx):
        guild_id = ctx.guild.id
        if guild_id not in self.data:
            wrapped = self.data[guild_id] = {}
            wrapped["queue"] = deque()
            wrapped["history"] = deque(maxlen=15)
            wrapped["current"] = None
            wrapped["waiting"] = False
            wrapped["loop"] = 0
            wrapped["processing"] = False
            wrapped["version"] = 3

            # New
            wrapped["cur_audio_filter"] = "normal"
            wrapped["cur_speed_filter"] = 1
            wrapped["next_audio_filter"] = "normal"
            wrapped["next_speed_filter"] = 1
            wrapped["autoshuffle"] = False
            wrapped["sleep_timer"] = None

        else:
            wrapped = self.data[guild_id]
        if wrapped["version"] == 3:
            wrapped["channel_id"] = ctx.channel.id
            wrapped["version"] = 4
        return wrapped

    # Helper function to remove the info for a guild
    def pop_info(self, ctx):
        return self.data.pop(ctx.guild.id, None)

    # Creates an audio source from a url
    async def player_from_url(self, ctx, url, *, loop=None, stream=False):
        self.ffmpeg_opts = self._STREAM_FFMPEG_OPTS  # Needed so dont have to do casework when jumping
        new_ffmpeg_opts = self.apply_filters(ctx, self.ffmpeg_opts.copy())
        ytdl = youtube_dl.YoutubeDL(self.ytdl_opts)
        loop = loop or asyncio.get_running_loop()
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=not stream))
        if 'entries' in data:
            # take first item from a playlist
            data = data['entries'][0]

        filename = data['url'] if stream else ytdl.prepare_filename(data)
        # print(filename)
        self.current_audio_link = filename
        # metadata
        md = self.current_metadata
        md["is_live"] = data.get("is_live")
        md["duration"] = data.get("duration")
        md["title"] = data.get("title")
        md["id"] = data.get("id")
        md["webpage_url"] = data.get("webpage_url")

        info = self.get_info(ctx)
        speed = info["cur_speed_filter"]
        filter = info["cur_audio_filter"]
        audio = patched_player.FFmpegPCMAudio(filename, speed, filter=filter, **new_ffmpeg_opts)
        player = discord.PCMVolumeTransformer(audio)
        return player, data

    # ==================================================
    # Original music.py functions
    # ==================================================

    @commands.command()
    async def join(self, ctx, *, channel: discord.VoiceChannel):
        """Joins a voice channel

        Text output will be sent from the channel this command was run in. This
        command can be run multiple times safely.

        """
        if ctx.voice_client is not None:
            await ctx.voice_client.move_to(channel)
        else:
            await channel.connect()
        info = self.get_info(ctx)
        if info["channel_id"] != ctx.channel.id:
            info["channel_id"] = ctx.channel.id
            await ctx.send("Switching music output to this channel")

    @commands.command()
    @commands.is_owner()
    async def local(self, ctx, *, query):
        """Plays a file from the local filesystem"""
        info = self.get_info(ctx)
        queue = info["queue"]
        queue.append({"ty": "local", "query": query})
        if info["current"] is None:
            self.schedule(ctx)
        await ctx.send(f"Added to queue: local {query}")

    @commands.command(aliases=["play", "p"])
    async def stream(self, ctx, *, url):
        """Plays from a url (almost anything youtube_dl supports)"""
        if len(url) > 100:
            raise ValueError("url too long (length over 100)")
        if not url.isprintable():
            raise ValueError(f"url not printable: {url!r}")
        print(ctx.message.author.name, "queued", repr(url))
        info = self.get_info(ctx)
        queue = info["queue"]
        history = info["history"]
        ty = "local" if url == "coco.mp4" else "stream"
        if url == "prev":
            if not history:
                raise commands.CommandError("no previous song")
            queue.append(history[-1])
        else:
            queue.append({"ty": ty, "query": url})
        if info["current"] is None:
            self.schedule(ctx)
        await ctx.send(f"Added to queue: {ty if url != 'prev' else history[-1]['ty']} {url if url != 'prev' else history[-1]['query']}{' (previous song)'*(url=='prev')}")

    @commands.command(aliases=["prepend","pplay", "pp"])
    async def stream_prepend(self, ctx, *, url):
        """Plays from a url (almost anything youtube_dl supports)"""
        if len(url) > 100:
            raise ValueError("url too long (length over 100)")
        if not url.isprintable():
            raise ValueError(f"url not printable: {url!r}")
        print(ctx.message.author.name, "queued", repr(url))
        info = self.get_info(ctx)
        queue = info["queue"]
        history = info["history"]
        ty = "local" if url == "coco.mp4" else "stream"
        if url == "prev":
            if not history:
                raise commands.CommandError("no previous song")
            queue.appendleft(history[-1])
        else:
            queue.appendleft({"ty": ty, "query": url})
        if info["current"] is None:
            self.schedule(ctx)
        await ctx.send(f"Prepended to queue: {ty if url != 'prev' else history[-1]['ty']} {url if url != 'prev' else history[-1]['query']}{' (previous song)'*(url=='prev')}")

    @commands.command()
    async def _add_playlist(self, ctx, *, url):
        """Adds all songs in a playlist to the queue"""
        if len(url) > 100:
            raise ValueError("url too long (length over 100)")
        if not url.isprintable():
            raise ValueError(f"url not printable: {url!r}")
        print(ctx.message.author.name, "queued playlist", repr(url))
        bracketed = False
        if url[0] == "<" and url[-1] == ">":
            bracketed = True
            url = url[1:-1]
        info = self.get_info(ctx)
        queue = info["queue"]
        ytdl = youtube_dl.YoutubeDL(self.ytdl_opts | {
            'noplaylist': None,
            'playlistend': None,
            "extract_flat": True,
        })
        data = await asyncio.to_thread(ytdl.extract_info, url, download=False)
        if 'entries' not in data:
            raise ValueError("cannot find entries of playlist")
        entries = data['entries']
        for entry in entries:
            url = f"https://www.youtube.com/watch?v={entry['url']}"
            if bracketed:
                url = f"<{url}>"
            queue.append({"ty": "stream", "query": url})
        if info["current"] is None:
            self.schedule(ctx)
        await ctx.send(f"Added playlist to queue: {url}")

    @commands.command(name="batch_add")
    async def _batch_add(self, ctx, *, urls):
        """Plays from multiple urls split by lines"""
        for url in urls.splitlines():
            await self.stream(ctx, url=url)
            await asyncio.sleep(0.1)

    def _shuffle(self, ctx):
        info = self.get_info(ctx)
        queue = info["queue"]
        temp = []
        while queue:
            temp.append(queue.popleft())
        random.shuffle(temp)
        while temp:
            queue.appendleft(temp.pop())

    @commands.command()
    async def shuffle(self, ctx):
        """Shuffles the queue"""
        self._shuffle(ctx)
        await ctx.send("Queue shuffled")

    @commands.command()
    async def volume(self, ctx, volume: float = None):
        """Gets or changes the player's volume"""
        if volume is None:
            volume = ctx.voice_client.source.volume * 100
            if int(volume) == volume:
                volume = int(volume)
            await ctx.send(f"Volume set to {volume}%")
            return
        if ctx.voice_client is None:
            return await ctx.send("Not connected to a voice channel")
        try:
            if int(volume) == volume:
                volume = int(volume)
        except (OverflowError, ValueError):
            pass
        if not await self.bot.is_owner(ctx.author):
            # prevent insane ppl from doing this
            volume = min(100, volume)
        ctx.voice_client.source.volume = volume / 100
        await ctx.send(f"Changed volume to {volume}%")

    @commands.command(aliases=["stop"])
    async def pause(self, ctx):
        """Pauses playing"""
        ctx.voice_client.pause()

    @commands.command(aliases=["start"])
    async def resume(self, ctx):
        """Resumes playing"""
        # after = lambda error, ctx=ctx: self.schedule(ctx, error)
        # info = self.get_info(ctx)
        # if info["jumped"]:
        #     info["jumped"] = False
        #     ctx.voice_client.play(self.current_audio_stream, after=after)
        #     print("jump moment")
        ctx.voice_client.resume()

    @commands.command()
    async def leave(self, ctx):
        """Disconnects the bot from voice and clears the queue"""
        self.task = None
        self.pop_info(ctx)
        if ctx.voice_client is None:
            return
        await ctx.voice_client.disconnect()

    @commands.command(aliases=["c"])
    async def current(self, ctx):
        """Shows the current song"""
        query = None
        if ctx.voice_client is not None:
            info = self.get_info(ctx)
            current = info["current"]
            if current is not None and not info["waiting"]:
                query = current["query"]
        await ctx.send(f"Current: {query}")

    @commands.command(aliases=["hist"])
    # TODO order might be a bit sus (unoptimal)
    async def playback_history(self, ctx):
        info = self.get_info(ctx)
        a = ""
        for v in info["history"]:
            a += f"{v}\n"
        await ctx.send(f"```{a}```")

    @commands.command(aliases=["q"])
    async def queue(self, ctx):
        """Shows the songs on queue"""
        looptype = {-1:"(looping current)", 1:"(looping queue)", 0:"", None:""}
        queue = ()
        length = 0
        looping = None
        if ctx.voice_client is not None:
            info = self.get_info(ctx)
            queue = info["queue"]
            length = len(queue)
            looping = info["loop"]
        if not queue:
            queue = (None,)
        paginator = commands.Paginator()
        paginator.add_line(f"Queue [{length}] {looptype[looping]}:")
        for i, song in enumerate(queue, start=1):
            if song is None:
                paginator.add_line("None")
            else:
                paginator.add_line(f"{i}: {song['query']}")
        for page in paginator.pages:
            await ctx.send(page)

    def normalize_index(self, ctx, position, length):
        index = position
        if index > 0:
            index -= 1
        if index < 0:
            index += length
        if not 0 <= index < length:
            raise ValueError(position)
        return index

    @commands.command()
    async def remove(self, ctx, position: int):
        """Removes a song on queue"""
        info = self.get_info(ctx)
        queue = info["queue"]
        try:
            index = self.normalize_index(ctx, position, len(queue))
        except ValueError:
            raise commands.CommandError(f"Index out of range [{position}]")
        queue.rotate(-index)
        song = queue.popleft()
        queue.rotate(index)
        await ctx.send(f"Removed song [{position}]: {song['query']}")

    @commands.command()
    async def move(self, ctx, origin: int, target: int):
        """Moves a song on queue"""
        info = self.get_info(ctx)
        queue = info["queue"]
        try:
            origin_index = self.normalize_index(ctx, origin, len(queue))
        except ValueError:
            raise commands.CommandError(f"Origin index out of range [{origin}]")
        try:
            target_index = self.normalize_index(ctx, target, len(queue))
        except ValueError:
            raise commands.CommandError(f"Target index out of range [{target}]")
        queue.rotate(-origin_index)
        song = queue.popleft()
        queue.rotate(origin_index - target_index)
        queue.appendleft(song)
        queue.rotate(target_index)
        await ctx.send(f"Moved song [{origin} -> {target}]: {song['query']}")

    @commands.command()
    async def clear(self, ctx):
        """Clears all songs on queue"""
        info = self.get_info(ctx)
        queue = info["queue"]
        queue.clear()
        await ctx.send("Cleared queue")

    @commands.command(aliases=["s"])
    async def skip(self, ctx):
        """Skips current song"""
        info = self.get_info(ctx)
        current = info["current"]
        # info["jumped"] = False
        ctx.voice_client.stop()
        if current is not None and not info["waiting"]:
            await ctx.send(f"Skipped: {current['query']}")

    @commands.command()
    async def loop(self, ctx, loop: int=None):
        '''
        ;loop q(queue) c(current) n(none) <>
        -1 = current
        0 = no loop
        1 = queue
        '''
        """Gets or sets queue looping"""
        info = self.get_info(ctx)
        if loop is None:
            await ctx.send(f"Queue {'is' if info['loop'] else 'is not'} looping")
            return
        if loop not in {-1, 0, 1}:
            raise commands.CommandError("ayo wrong loop type bruh")
        info["loop"] = loop
        await ctx.send(f"{loop} type loop")

    @commands.command()
    @commands.is_owner()
    async def reschedule(self, ctx):
        """Reschedules the current guild onto the advancer task"""
        self.schedule(ctx, force=True)
        await ctx.send("Rescheduled")

    # ==================================================
    # Functions referenced by filters.py
    # ==================================================

    async def _set_audio_filter(self, ctx, afilter):
        info = self.get_info(ctx)
        info["next_audio_filter"] = afilter
        await ctx.send(f"filter = `{afilter}`")


    async def _set_speed_filter(self, ctx, factor):
        # TODO floating poin precision deal with
        if not (0.5 <= factor <= 2):
            raise commands.CommandError(f"Speed factor [{factor}] outside of factor range from 0.5 to 2 inclusive")

        info = self.get_info(ctx)

        if info["next_audio_filter"] in {"daycore", "nightcore"}:
            raise commands.CommandError("in order to use this command, turn off daycore or nightcore")

        info["next_speed_filter"] = factor


        await ctx.send(f"speed = x{factor}")

    def apply_filters(self, ctx, opts, jump=False):
        # Filter name always guaranteed to be valid
        info = self.get_info(ctx)

        # Setting current to next, don't reset next (because it means filter is being reset)
        if not jump:
            info["cur_audio_filter"] = info["next_audio_filter"]
            info["cur_speed_filter"] = info["next_speed_filter"]
        current_filter = info["cur_audio_filter"]
        current_speed = info["cur_speed_filter"]

        filter_li = []

        if current_filter != "normal":
            filter_li.append(self._FILTERS[current_filter])
        if current_speed != 1:
            # astrate speeds it up already
            if current_filter not in {"daycore", "nightcore"}:
                filter_li.append(f"atempo={current_speed}")

        if filter_li:
            add_options = f" -filter_complex {','.join(filter_li)}"
            opts["options"] += add_options
        # If nothing in list then it means its default options

        print(opts)
        return opts

    # ==================================================
    # Functions referenced by more.py
    # ==================================================

    async def bruh(self, ctx, dur):
        info = self.get_info(ctx)
        info["sleep_timer"] = [dur, ctx.message.author, time.time()]
        await asyncio.sleep(dur)
        await self.leave(ctx)
        info["sleep_timer"] = None

    @commands.command(aliases=["fs"])
    async def forceskip(self, ctx):
        info = self.get_info(ctx)
        current = info["current"]
        # info["jumped"] = False
        ctx.voice_client.stop()
        if current is not None and not info["waiting"]:
            info["current"] = None
            await ctx.send(f"forceskipped {current}")


    @commands.command()
    async def sleepin(self, ctx, dur):
        # After this is in the form of <int> seconds or [[HH:]MM:]SS
        if not self.regex_time(dur) and not self.time_match(dur):
            raise commands.CommandError(f"Position [{dur}] not in the form of [[HH:]MM:]SS or a positive integer number of seconds")

        # Check for > 99:59:59 exceed
        if self.time_match(dur) and int(dur) > self.seconds("99:59:59"):
            raise commands.CommandError(f"time in seconds greater than 99:59:59")


        if self.task is None or self.task.done() or self.task.cancelled():
            self.task = asyncio.create_task(self.bruh(ctx, self.seconds(dur)))
            await ctx.send(f"{self.seconds(dur)}, {type(self.seconds(dur))}, {self.task}")
        # There is a task already running
        else:
            raise commands.CommandError("there is a task running")


    @commands.command()
    async def cancel(self, ctx):
        info = self.get_info(ctx)

        if self.task.done() or self.task.cancelled() or self.task == None:
            raise commands.CommandError(f"trying to cancel a completed task status {self.task.result()}")
        info["sleep_timer"] = None
        self.task.cancel()

        await ctx.send(self.task)


    @commands.command(aliases=["ashuffle"])
    async def autoshuffle(self, ctx, auto: typing.Optional[bool] = None):
        info = self.get_info(ctx)
        if auto is None:
            await ctx.send(f"autoshuffler is {'on' if info['autoshuffle'] else 'off'}")
            return

        info["autoshuffle"] = auto
        await ctx.send(f"autoshuffle set to {auto}")

    # TODO test unloading reloading with filters
    @commands.command(aliases = ["i"])
    async def info(self, ctx):
        info = self.get_info(ctx)
        # print(info)
        await ctx.send(f"`{info}\n\n{self.current_metadata}`")
        # ratio = round(self.current_audio_stream.original.ms_time/1000)/round(self.current_metadata["duration"])
        # norm = int(20*ratio)
        # await ctx.send(f"`[{'#'*norm}{' '*(20-norm)}]`")
        print(f"{time.time() - info['sleep_timer'][-1]} seconds have passed")

    async def _fast_forward(self, ctx, sec):
        if not (1 <= sec <= 15):
            raise commands.CommandError(f"Seek time [{sec}] not a positive integer number of seconds ranging from 1 to 15 seconds inclusive")

        # more often raised when try to seek during pause
        if not self.current_audio_stream.original.seekable():
            raise commands.CommandError("can't jump forward no more")

        info = self.get_info(ctx)
        speed = info["cur_speed_filter"]
        filter = info["cur_audio_filter"]
        if filter in {"daycore", "nightcore"}:
            scaled_frames = 1000 / (20*0.75 if filter == "daycore" else 20*1.25)
        else:
            scaled_frames = 1000 / (20*speed)
        self.current_audio_stream.original.seek_fw(round(scaled_frames*sec))
        await ctx.send(f"Seeked {sec} second(s) forward, scaled = {scaled_frames}")

    async def _rewind(self, ctx, sec):
        if not (1 <= sec <= 15):
            raise commands.CommandError(f"Seek time [{sec}] not a positive integer number of seconds ranging from 1 to 15 seconds inclusive")


        info = self.get_info(ctx)
        speed = info["cur_speed_filter"]
        filter = info["cur_audio_filter"]
        if filter in {"daycore", "nightcore"}:
            scaled_frames = 1000 / (20*0.75 if filter == "daycore" else 20*1.25)
        else:
            scaled_frames = 1000 / (20*speed)

        self.current_audio_stream.original.seek_bw(round(scaled_frames*sec))
        await ctx.send(f"Seeked {sec} second(s) backward scaled = {scaled_frames}")


    def regex_time(self, pos):
        # Based off simplified version of https://ffmpeg.org/ffmpeg-utils.html#time-duration-syntax
        # Match [[HH:]MM:]SS or integer seconds, brackets optional
        # First check regex match
        # Regex pattern slightly modified from: https://stackoverflow.com/a/8318367
        return re.match(r"^(?:(?:(\d?\d):)?([0-5]?\d):)?([0-5]?\d)$", pos)

    def time_match(self, pos):
        return pos.isdigit()

    async def _jump(self, ctx, pos):
        # put a cap on how much you can jump
        info = self.get_info(ctx)

        # After this is in the form of <int> seconds or [[HH:]MM:]SS
        if not self.regex_time(pos) and not self.time_match(pos):
            raise commands.CommandError(f"Position [{pos}] not in the form of [[HH:]MM:]SS or a positive integer number of seconds")

        # Check for > 99:59:59 exceed
        if self.time_match(pos) and int(pos) > self.seconds("99:59:59"):
                raise commands.CommandError(f"time in seconds greater than 99:59:59")

        # Not new song, so can keep current filter settings
        new_ffmpeg_opts = self.apply_filters(ctx, self.ffmpeg_opts.copy(), jump=True)
        new_ffmpeg_opts["before_options"] += f" -ss {pos}"

        speed = info["cur_speed_filter"]
        filter = info["cur_audio_filter"]
        strem = discord.PCMVolumeTransformer(patched_player.FFmpegPCMAudio(self.current_audio_link, speed, filter=filter, **new_ffmpeg_opts))

        # Seeking past the song
        if not strem.original.seekable():
            raise commands.CommandError(f"bruv ur trying to seek beyond the song")

        self.current_audio_stream = strem



        ctx.voice_client._player.source = strem
        secs = self.seconds(pos)
        self.current_audio_stream.original.ms_time = secs*1000
        await ctx.send(f"jumped to {pos} = {secs*1000}ms")
        '''
        print(self.get_info(ctx)["waiting"])

        await ctx.send("valid pos")
        '''

    @commands.command()
    async def ffmpog(self, ctx):
        await ctx.send(self.ffmpeg_opts)

    def seconds(self, hhmmss):
        '''
        if len 1 -> ss
        if len 2 -> mm:ss
        if len 3 -> hh:mm:ss

        never hh:ss
        '''
        hhmmss_list = hhmmss.split(":")
        hour_s = int(hhmmss_list[-3])*3600 if len(hhmmss_list) >= 3 else 0
        min_s = int(hhmmss_list[-2])*60 if len(hhmmss_list) >= 2 else 0
        return hour_s + min_s + int(hhmmss_list[-1])

    async def _loc(self, ctx):
        await ctx.send(f"{self.current_audio_stream.original.ms_time/1000}s")



    @local.before_invoke
    @stream.before_invoke
    async def ensure_connected(self, ctx):
        if ctx.voice_client is None:
            if ctx.author.voice:
                await ctx.author.voice.channel.connect()
            else:
                raise commands.CommandError("Author not connected to a voice channel")

    @pause.before_invoke
    @resume.before_invoke
    async def check_playing(self, ctx):
        await self.check_connected(ctx)
        if ctx.voice_client.source is None:
            raise commands.CommandError("Not playing anything right now")

    @remove.before_invoke
    @reschedule.before_invoke
    @skip.before_invoke
    @clear.before_invoke
    @volume.before_invoke
    @sleepin.before_invoke
    @cancel.before_invoke
    async def check_connected(self, ctx):
        if ctx.voice_client is None:
            raise commands.CommandError("Not connected to a voice channel")

def setup(bot):
    # Suppress noise about console usage from errors
    bot._music_old_ytdl_bug_report_message = youtube_dl.utils.bug_reports_message
    youtube_dl.utils.bug_reports_message = lambda: ''

    return bot.add_cog(Music(bot))

def teardown(bot):
    youtube_dl.utils.bug_reports_message = bot._music_old_ytdl_bug_report_message
    return bot.wrap_async(None)
