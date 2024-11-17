import discord
import aiohttp
import re
import random
import logging
import urllib.parse
import html
from redbot.core import commands, app_commands, Config
from expiringdict import ExpiringDict

log = logging.getLogger("red.crab-cogs.rule34cog")

EMBED_COLOR = 0xD7598B
EMBED_ICON = "https://i.imgur.com/FeRu6Pw.png"
IMAGE_TYPES = (".png", ".jpeg", ".jpg", ".webp", ".gif")
TAG_BLACKLIST = ["loli", "shota", "guro", "video"]
HEADERS = {
    "User-Agent": f"crab-cogs/v1 (https://github.com/hollowstrawberry/crab-cogs);"
}

class Rule34(commands.Cog):
    """Searches images on Rule34.xxx with slash command and tag completion support."""

    def __init__(self, bot):
        super().__init__()
        self.bot = bot
        self.tag_cache = {}  # tag query -> tag results
        self.image_cache = ExpiringDict(max_len=100, max_age_seconds=24*60*60)  # channel id -> list of sent post ids
        self.config = Config.get_conf(self, identifier=62667275)
        self.config.register_global(tag_cache={})

    async def cog_load(self):
        self.tag_cache = await self.config.tag_cache()

    async def red_delete_data_for_user(self, requester: str, user_id: int):
        pass

    @commands.command()
    @commands.is_owner()
    async def rule34deletecache(self, ctx: commands.Context):
        self.tag_cache = {}
        async with self.config.tag_cache() as tag_cache:
            tag_cache.clear()
        await ctx.react_quietly("✅")

    @commands.hybrid_command(aliases=["r34"])
    @app_commands.describe(tags="Will suggest tags with autocomplete. Separate tags with spaces.")
    async def rule34(self, ctx: commands.Context, *, tags: str):
        """Finds an image on Rule34.xxx. Type tags separated by spaces.

        As a slash command, will provide suggestions for the latest tag typed.
        Won't repeat the same post until all posts with the same search have been exhausted.
        Will be limited to safe searches in non-NSFW channels.
        Type - before a tag to exclude it."""

        tags = tags.strip()
        if tags.lower() in ["none", "error"]:
            tags = ""
        if not ctx.channel.nsfw:
            await ctx.send("This command can only be used in NSFW channels.")
            return

        try:
            result = await self.grab_image(tags, ctx)
        except:
            log.exception("Failed to grab image from Rule34.xxx")
            await ctx.send("Sorry, there was an error trying to grab an image from Rule34.xxx. Please try again or contact the bot owner.")
            return
        if not result:
            description = "💨 No results found..."
            await ctx.send(embed=discord.Embed(description=description, color=EMBED_COLOR))
            return

        embed = discord.Embed(color=EMBED_COLOR)
        embed.set_author(name="Rule34 Post", url=f"https://rule34.xxx/index.php?page=post&s=view&id={result['id']}", icon_url=EMBED_ICON)
        embed.set_image(url=result["file_url"] if result["width"] * result["height"] < 4200000 else result["sample_url"])
        if result.get("source", ""):
            embed.description = f"[🔗 Original Source]({result['source']})"
        embed.set_footer(text=f"⭐ {result.get('score', 0)}")
        await ctx.send(embed=embed)

    @rule34.autocomplete("tags")
    async def tags_autocomplete(self, interaction: discord.Interaction, current: str):
        if current is None:
            current = ""
        if ' ' in current:
            previous, last = [x.strip() for x in current.rsplit(' ', maxsplit=1)]
        else:
            previous, last = "", current.strip()
        excluded = last.startswith('-')
        last = last.lstrip('-')
        if not last and not excluded:
            results = []
            if "full_body" not in previous:
                results.append("full_body")
            if "-" not in previous:
                results.append("-excluded_tag")
            if "score" not in previous:
                results += ["score:>10", "score:>100"]
        else:
            try:
                results = await self.grab_tags(last)
            except:
                log.exception("Failed to load Rule34 tags")
                results = ["Error"]
                previous = None
        if excluded:
            results = [f"-{res}" for res in results]
        if previous:
            results = [f"{previous} {res}" for res in results]
        return [discord.app_commands.Choice(name=i, value=i) for i in results]

    async def grab_tags(self, query) -> list[str]:
        if query in self.tag_cache:
            return self.tag_cache[query].split(' ')
        query = urllib.parse.quote(query.lower(), safe=' ')
        url = f"https://rule34.xxx/index.php?page=dapi&s=tag&q=index&json=1&sort=desc&order_by=index_count&name_pattern=%25{query}%25"
        async with aiohttp.ClientSession(headers=HEADERS) as session:
            async with session.get(url) as resp:
                data = await resp.json()
        if not data or "tag" not in data:
            return []
        results = [tag["name"] for tag in data["tag"]][:20]
        results = [html.unescape(tag) for tag in results]
        self.tag_cache[query] = ' '.join(results)
        async with self.config.tag_cache() as tag_cache:
            tag_cache[query] = self.tag_cache[query]
        return results

    async def grab_image(self, query: str, ctx: commands.Context) -> dict:
        query = urllib.parse.quote(query.lower(), safe=' ')
        tags = [tag for tag in query.split(' ') if tag]
        tags = [tag for tag in tags if tag not in TAG_BLACKLIST]
        tags += [f"-{tag}" for tag in TAG_BLACKLIST]
        query = ' '.join(tags)
        url = "https://rule34.xxx/index.php?page=dapi&s=post&q=index&json=1&limit=1000&tags=" + query.replace(' ', '+')
        async with aiohttp.ClientSession(headers=HEADERS) as session:
            async with session.get(url) as resp:
                data = await resp.json()
        if not data or "post" not in data:
            return {}
        images = [img for img in data["post"] if img["file_url"].endswith(IMAGE_TYPES)]
        key = ctx.channel.id
        if key not in self.image_cache:
            self.image_cache[key] = []
        if all(img["id"] in self.image_cache[key] for img in images):
            self.image_cache[key] = self.image_cache[key][-1:]
        if len(images) > 1:
            images = [img for img in images if img["id"] not in self.image_cache[key]]
        choice = random.choice(images)
        self.image_cache[key].append(choice["id"])
        return choice
