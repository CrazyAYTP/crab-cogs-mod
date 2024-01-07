import io
import json
import base64
import asyncio
import discord
import logging
import calendar
from PIL import Image
from hashlib import md5
from datetime import datetime, timedelta
from redbot.core import commands, app_commands, Config
from redbot.core.bot import Red
from novelai_api import NovelAIError
from novelai_api.ImagePreset import ImageModel, ImagePreset, ImageSampler, ImageGenerationType, UCPreset
from typing import Optional, Union, Tuple, Coroutine

from novelai.naiapi import NaiAPI
from novelai.imageview import ImageView, RetryView
from novelai.constants import *

log = logging.getLogger("red.crab-cogs.novelai")

def round_to_nearest(x, base):
    return int(base * round(x/base))

def scale_to_size(width: int, height: int, size: int) -> Tuple[int, int]:
    scale = (size / (width * height)) ** 0.5
    return int(width * scale), int(height * scale)


class NovelAI(commands.Cog):
    """Generate anime images with NovelAI v3."""

    def __init__(self, bot: Red):
        super().__init__()
        self.bot = bot
        self.api: Optional[NaiAPI] = None
        self.queue: list[Tuple[Coroutine, discord.Interaction]] = []
        self.queue_task: Optional[asyncio.Task] = None
        self.generating: dict[int, bool] = {}
        self.last_img: dict[int, datetime] = {}
        self.loading_emoji = ""
        self.config = Config.get_conf(self, identifier=66766566169)
        defaults_user = {
            "base_prompt": DEFAULT_PROMPT,
            "base_negative_prompt": DEFAULT_NEGATIVE_PROMPT,
            "resolution": "832,1216",
            "guidance": 5.0,
            "guidance_rescale": 0.0,
            "sampler": "k_euler",
            "sampler_version": "Regular",
            "noise_schedule": "Always pick recommended",
            "decrisper": False,
        }
        defaults_global = {
            "server_cooldown": 0,
            "dm_cooldown": 60,
            "loading_emoji": "",
        }
        defaults_guild = {
            "nsfw_filter": False,
        }
        self.config.register_user(**defaults_user)
        self.config.register_global(**defaults_global)
        self.config.register_global(**defaults_guild)

    async def cog_load(self):
        await self.try_create_api()
        self.loading_emoji = await self.config.loading_emoji()

    async def red_delete_data_for_user(self, requester: str, user_id: int):
        pass

    async def try_create_api(self):
        api = await self.bot.get_shared_api_tokens("novelai")
        username, password = api.get("username"), api.get("password")
        if username and password:
            self.api = NaiAPI(username, password)
            return True
        else:
            return False

    async def consume_queue(self):
        new = True
        while self.queue:
            task, ctx = self.queue.pop(0)
            alive = True
            if not new:
                try:
                    await ctx.edit_original_response(content=self.loading_emoji + "`Generating image...`")
                except discord.errors.NotFound:
                    self.generating[ctx.user.id] = False
                    alive = False
                except:
                    log.exception("Editing message in queue")
            if self.queue:
                asyncio.create_task(self.edit_queue_messages())
            if alive:
                await task
            new = False

    async def edit_queue_messages(self):
        tasks = [ctx.edit_original_response(content=self.loading_emoji + f"`Position in queue: {i + 1}`")
                 for i, (task, ctx) in enumerate(self.queue)]
        await asyncio.gather(*tasks, return_exceptions=True)

    def queue_add(self,
                  ctx: discord.Interaction,
                  prompt: str,
                  preset: ImagePreset,
                  requester: Optional[int] = None,
                  callback: Optional[Coroutine] = None):
        self.generating[ctx.user.id] = True
        self.queue.append((self.fulfill_novelai_request(ctx, prompt, preset, requester, callback), ctx))
        if not self.queue_task or self.queue_task.done():
            self.queue_task = asyncio.create_task(self.consume_queue())

    def get_loading_message(self):
        message = f"`Position in queue: {len(self.queue) + 1}`" if self.queue_task and not self.queue_task.done() else "`Generating image...`"
        return self.loading_emoji + message

    @app_commands.command(name="novelai",
                          description="Generate anime images with NovelAI v3.")
    @app_commands.describe(prompt="Gets added to your base prompt (/novelaidefaults)",
                           negative_prompt="Gets added to your base negative prompt (/novelaidefaults)",
                           seed="Random number that determines image generation.",
                           **PARAMETER_DESCRIPTIONS)
    @app_commands.choices(**PARAMETER_CHOICES)
    async def novelai(self,
                      ctx: discord.Interaction,
                      prompt: str,
                      negative_prompt: Optional[str],
                      seed: Optional[int],
                      resolution: Optional[str],
                      guidance: Optional[app_commands.Range[float, 0.0, 10.0]],
                      guidance_rescale: Optional[app_commands.Range[float, 0.0, 1.0]],
                      sampler: Optional[ImageSampler],
                      sampler_version: Optional[str],
                      noise_schedule: Optional[str],
                      decrisper: Optional[bool],
                      ):
        result = await self.prepare_novelai_request(
            ctx, prompt, negative_prompt, seed, resolution, guidance, guidance_rescale,
            sampler, sampler_version, noise_schedule, decrisper
        )
        if not result:
            return
        prompt, preset = result

        message = self.get_loading_message()
        self.queue_add(ctx, prompt, preset)
        await ctx.response.send_message(content=message)  # noqa

    @app_commands.command(name="novelai-img2img",
                          description="Convert img2img with NovelAI v3.")
    @app_commands.describe(image="The image you want to use as a base.",
                           strength="How much you want the image to change. 0.7 is default.",
                           noise="Adds new detail to your image. 0 is default.",
                           prompt="Gets added to your base prompt (/novelaidefaults)",
                           negative_prompt="Gets added to your base negative prompt (/novelaidefaults)",
                           seed="Random number that determines image generation.",
                           **PARAMETER_DESCRIPTIONS_IMG2IMG)
    @app_commands.choices(**PARAMETER_CHOICES_IMG2IMG)
    async def novelai_img(self,
                          ctx: discord.Interaction,
                          image: discord.Attachment,
                          strength: app_commands.Range[float, 0.0, 1.0],
                          noise: app_commands.Range[float, 0.0, 1.0],
                          prompt: str,
                          negative_prompt: Optional[str],
                          seed: Optional[int],
                          guidance: Optional[app_commands.Range[float, 0.0, 10.0]],
                          guidance_rescale: Optional[app_commands.Range[float, 0.0, 1.0]],
                          sampler: Optional[ImageSampler],
                          sampler_version: Optional[str],
                          noise_schedule: Optional[str],
                          decrisper: Optional[bool],
                          ):
        if "image" not in image.content_type or not image.width or not image.height:
            return await ctx.response.send_message("Attachment must be a valid image.", ephemeral=True)  # noqa
        width, height = scale_to_size(image.width, image.height, MAX_FREE_IMAGE_SIZE)
        resolution = f"{round_to_nearest(width, 64)},{round_to_nearest(height, 64)}"

        result = await self.prepare_novelai_request(
            ctx, prompt, negative_prompt, seed, resolution, guidance, guidance_rescale,
            sampler, sampler_version, noise_schedule, decrisper
        )
        if not result:
            return
        await ctx.response.defer()  # noqa

        prompt, preset = result
        preset.strength = strength
        preset.noise = noise
        fp = io.BytesIO()
        await image.save(fp)
        if image.width*image.height > MAX_UPLOADED_IMAGE_SIZE:
            try:
                width, height = scale_to_size(image.width, image.height, MAX_UPLOADED_IMAGE_SIZE)
                resized_image = Image.open(fp).resize((width, height), Image.Resampling.LANCZOS)
                fp = io.BytesIO()
                resized_image.save(fp, "PNG")
                fp.seek(0)
            except:
                log.exception("Resizing image")
                return await ctx.followup.send(":warning: Failed to resize image. Please try sending a smaller image.")
        preset.image = base64.b64encode(fp.read()).decode()

        message = self.get_loading_message()
        self.queue_add(ctx, prompt, preset)
        await ctx.edit_original_response(content=message)

    async def prepare_novelai_request(self,
                                      ctx: discord.Interaction,
                                      prompt: str,
                                      negative_prompt: Optional[str],
                                      seed: Optional[int],
                                      resolution: Optional[str],
                                      guidance: Optional[app_commands.Range[float, 0.0, 10.0]],
                                      guidance_rescale: Optional[app_commands.Range[float, 0.0, 1.0]],
                                      sampler: Optional[ImageSampler],
                                      sampler_version: Optional[str],
                                      noise_schedule: Optional[str],
                                      decrisper: Optional[bool],
                                      ) -> Optional[Tuple[str, ImagePreset]]:
        if not self.api and not await self.try_create_api():
            return await ctx.response.send_message(  # noqa
                "NovelAI username and password not set. The bot owner needs to set them like this:\n"
                "[p]set api novelai username,USERNAME\n[p]set api novelai password,PASSWORD")

        if not await self.bot.is_owner(ctx.user):
            cooldown = await self.config.server_cooldown() if ctx.guild else await self.config.dm_cooldown()
            if self.generating.get(ctx.user.id, False):
                content = "Your current image must finish generating before you can request another one."
                return await ctx.response.send_message(content, ephemeral=True)  # noqa
            if ctx.user.id in self.last_img and (datetime.utcnow() - self.last_img[ctx.user.id]).seconds < cooldown:
                eta = self.last_img[ctx.user.id] + timedelta(seconds=cooldown)
                content = f"You may use this command again <t:{calendar.timegm(eta.utctimetuple())}:R>."
                if not ctx.guild:
                    content += " (You can use it more frequently inside a server)"
                return await ctx.response.send_message(content, ephemeral=True)  # noqa

        base_prompt = await self.config.user(ctx.user).base_prompt()
        if base_prompt:
            prompt = f"{prompt.strip(' ,')}, {base_prompt}" if prompt else base_prompt
        base_neg = await self.config.user(ctx.user).base_negative_prompt()
        if base_neg:
            negative_prompt = f"{negative_prompt.strip(' ,')}, {base_neg}" if negative_prompt else base_neg
        resolution = resolution or await self.config.user(ctx.user).resolution()

        if ctx.guild and not ctx.channel.nsfw and NSFW_TERMS.search(prompt):
            return await ctx.response.send_message(":warning: You may not generate NSFW images in non-NSFW channels.")  # noqa

        if not ctx.guild and TOS_TERMS.search(prompt):
            return await ctx.response.send_message(  # noqa
                ":warning: To abide by Discord terms of service, the prompt you chose may not be used in private.\n"
                "You may use this command in a server, where your generations may be reviewed by a moderator."
            )

        if NSFW_TERMS.search(prompt) and TOS_TERMS.search(prompt):
            return await ctx.response.send_message(  # noqa
                ":warning: To abide by Discord terms of service, the prompt you chose may not be used."
            )

        if ctx.guild and not ctx.channel.nsfw and await self.config.guild(ctx.guild).nsfw_filter():
            prompt = "rating:general, " + prompt

        preset = ImagePreset()
        preset.n_samples = 1
        try:
            preset.resolution = tuple(int(num) for num in resolution.split(","))
        except:
            preset.resolution = (1024, 1024)
        preset.uc = negative_prompt or DEFAULT_NEGATIVE_PROMPT
        preset.uc_preset = UCPreset.Preset_None
        preset.quality_toggle = False
        preset.sampler = sampler or ImageSampler(await self.config.user(ctx.user).sampler())
        preset.scale = guidance if guidance is not None else await self.config.user(ctx.user).guidance()
        preset.cfg_rescale = guidance_rescale if guidance_rescale is not None else await self.config.user(ctx.user).guidance_rescale()
        preset.decrisper = decrisper if decrisper is not None else await self.config.user(ctx.user).decrisper()
        preset.noise_schedule = noise_schedule or await self.config.user(ctx.user).noise_schedule()
        if "recommended" in preset.noise_schedule:
            preset.noise_schedule = "exponential" if "2m" in str(preset.sampler) else "native"
        if "ddim" in str(preset.sampler) or "ancestral" in str(preset.sampler) and preset.noise_schedule == "karras":
            preset.noise_schedule = "native"
        if seed is not None and seed > 0:
            preset.seed = seed
        preset.uncond_scale = 1.0
        if "ddim" not in str(preset.sampler):
            sampler_version = sampler_version or await self.config.user(ctx.user).sampler_version()
            preset.smea = "SMEA" in sampler_version
            preset.smea_dyn = "DYN" in sampler_version
        return prompt, preset

    async def fulfill_novelai_request(self,
                                      ctx: discord.Interaction,
                                      prompt: str,
                                      preset: ImagePreset,
                                      requester: Optional[int] = None,
                                      callback: Optional[Coroutine] = None):
        try:
            try:
                for retry in range(4):
                    try:
                        async with self.api as wrapper:
                            action = ImageGenerationType.IMG2IMG if preset._settings.get("image", None) else ImageGenerationType.NORMAL  # noqa
                            model = ImageModel.Inpainting_Anime_v3 if action == ImageGenerationType.INPAINTING else ImageModel.Anime_v3
                            async for _, img in wrapper.api.high_level.generate_image(prompt, model, preset, action):
                                image_bytes = img
                            break
                    except NovelAIError as error:
                        if error.status not in (500, 520, 408, 522, 524) or retry == 3:
                            raise
                        log.warning("NovelAI encountered an error." if error.status in (500, 520) else "Timed out.")
                        if retry == 1:
                            await ctx.edit_original_response(content=self.loading_emoji + "`Generating image...` :warning:")
                        await asyncio.sleep(1)
            except Exception as error:
                view = RetryView(self, prompt, preset)
                if isinstance(error, discord.errors.NotFound):
                    raise
                if isinstance(error, NovelAIError):
                    if error.status == 401:
                        return await ctx.edit_original_response(content=":warning: Failed to authenticate NovelAI account.")
                    elif error.status == 402:
                        return await ctx.edit_original_response(content=":warning: The subscription and/or credits have run out for this NovelAI account.")
                    elif error.status in (500, 520, 408, 522, 524):
                        content = "NovelAI seems to be experiencing an outage, and multiple retries have failed. " \
                                  "Please be patient and try again soon."
                        view = None
                    elif error.status == 429:
                        content = "Bot is not allowed to generate multiple images at the same time. Please wait a minute."
                        view = None
                        callback = None
                    elif error.status == 400:
                        content = "Failed to generate image: " + (error.message or "A validation error occured.")
                    elif error.status == 409:
                        content = "Failed to generate image: " + (error.message or "A conflict error occured.")
                    else:
                        content = f"Failed to generate image: Error {error.status}."
                    log.warning(content)
                else:
                    content = "Failed to generate image! Contact the bot owner if the problem persists."
                    log.error(f"Generating image: {type(error).__name__} - {error}")
                msg = await ctx.edit_original_response(content=f":warning: {content}", view=view)
                if view:
                    asyncio.create_task(self.delete_button_after(msg, view))
                return
            finally:
                self.generating[ctx.user.id] = False
                self.last_img[ctx.user.id] = datetime.utcnow()

            name = md5(image_bytes).hexdigest() + ".png"
            file = discord.File(io.BytesIO(image_bytes), name)
            image = Image.open(io.BytesIO(image_bytes))
            seed = json.loads(image.info["Comment"])["seed"]
            view = ImageView(self, prompt, preset, seed)
            content = f"{'Reroll' if callback else 'Retry'} requested by <@{requester}>" if requester and ctx.guild else None
            msg = await ctx.edit_original_response(content=content, attachments=[file], view=view, allowed_mentions=discord.AllowedMentions.none())

            asyncio.create_task(self.delete_button_after(msg, view))
            imagescanner = self.bot.get_cog("ImageScanner")
            if imagescanner:
                if ctx.channel.id in imagescanner.scan_channels:  # noqa
                    img_info = imagescanner.convert_novelai_info(image.info)  # noqa
                    imagescanner.image_cache[msg.id] = ({1: img_info}, {1: image_bytes})  # noqa
                    await msg.add_reaction("🔎")
        except discord.errors.NotFound:
            pass
        except:
            log.exception("Fulfilling request")
        finally:
            if callback:
                try:
                    await callback
                except:
                    pass

    @staticmethod
    async def delete_button_after(msg: discord.Message, view: Union[ImageView, RetryView]):
        await asyncio.sleep(VIEW_TIMEOUT)
        if not view.deleted:
            await msg.edit(view=None)

    @app_commands.command(name="novelaidefaults",
                          description="Views or updates your personal default values for /novelai")
    @app_commands.describe(base_prompt="Gets added after each prompt. \"none\" to delete, \"default\" to reset.",
                           base_negative_prompt="Gets added after each negative prompt. \"none\" to delete, \"default\" to reset.",
                           **PARAMETER_DESCRIPTIONS)
    @app_commands.choices(**PARAMETER_CHOICES)
    async def novelaidefaults(self,
                              ctx: discord.Interaction,
                              base_prompt: Optional[str],
                              base_negative_prompt: Optional[str],
                              resolution: Optional[str],
                              guidance: Optional[app_commands.Range[float, 0.0, 10.0]],
                              guidance_rescale: Optional[app_commands.Range[float, 0.0, 1.0]],
                              sampler: Optional[str],
                              sampler_version: Optional[str],
                              noise_schedule: Optional[str],
                              decrisper: Optional[bool],
                              ):
        if base_prompt is not None:
            base_prompt = base_prompt.strip(" ,")
            if base_prompt.lower() == "none":
                base_prompt = None
            elif base_prompt.lower() == "default":
                base_prompt = DEFAULT_PROMPT
            await self.config.user(ctx.user).base_prompt.set(base_prompt)
        if base_negative_prompt is not None:
            base_negative_prompt = base_negative_prompt.strip(" ,")
            if base_negative_prompt.lower() == "none":
                base_negative_prompt = None
            elif base_negative_prompt.lower() == "default":
                base_negative_prompt = DEFAULT_NEGATIVE_PROMPT
            await self.config.user(ctx.user).base_negative_prompt.set(base_negative_prompt)
        if resolution is not None:
            await self.config.user(ctx.user).resolution.set(resolution)
        if guidance is not None:
            await self.config.user(ctx.user).guidance.set(guidance)
        if guidance_rescale is not None:
            await self.config.user(ctx.user).guidance_rescale.set(guidance_rescale)
        if sampler is not None:
            await self.config.user(ctx.user).sampler.set(sampler)
        if sampler_version is not None:
            await self.config.user(ctx.user).sampler_version.set(sampler_version)
        if noise_schedule is not None:
            await self.config.user(ctx.user).noise_schedule.set(noise_schedule)
        if decrisper is not None:
            await self.config.user(ctx.user).decrisper.set(decrisper)

        embed = discord.Embed(title="NovelAI default settings", color=0xffffff)
        prompt = str(await self.config.user(ctx.user).base_prompt())
        neg = str(await self.config.user(ctx.user).base_negative_prompt())
        embed.add_field(name="Base prompt", value=prompt[:1000] + "..." if len(prompt) > 1000 else prompt, inline=False)
        embed.add_field(name="Base negative prompt", value=neg[:1000] + "..." if len(neg) > 1000 else neg, inline=False)
        embed.add_field(name="Resolution", value=RESOLUTION_TITLES[await self.config.user(ctx.user).resolution()])
        embed.add_field(name="Guidance", value=f"{await self.config.user(ctx.user).guidance():.1f}")
        embed.add_field(name="Guidance Rescale", value=f"{await self.config.user(ctx.user).guidance_rescale():.2f}")
        embed.add_field(name="Sampler", value=SAMPLER_TITLES[await self.config.user(ctx.user).sampler()])
        embed.add_field(name="Sampler Version", value=await self.config.user(ctx.user).sampler_version())
        embed.add_field(name="Noise Schedule", value=await self.config.user(ctx.user).noise_schedule())
        embed.add_field(name="Decrisper", value=f"{await self.config.user(ctx.user).decrisper()}")
        await ctx.response.send_message(embed=embed, ephemeral=True)  # noqa

    @commands.group()
    async def novelaiset(self, _):
        """Configure /novelai bot-wide."""
        pass

    @novelaiset.command()
    @commands.is_owner()
    async def servercooldown(self, ctx: commands.Context, seconds: Optional[int]):
        """Time in seconds between a user's generation ends and they can start a new one, inside a server."""
        if seconds is None:
            seconds = await self.config.server_cooldown()
        else:
            await self.config.server_cooldown.set(max(0, seconds))
        await ctx.reply(f"Users will need to wait {max(0, seconds)} seconds between generations inside a server.")

    @novelaiset.command()
    @commands.is_owner()
    async def dmcooldown(self, ctx: commands.Context, seconds: Optional[int]):
        """Time in seconds between a user's generation ends and they can start a new one, in DMs with the bot."""
        if seconds is None:
            seconds = await self.config.dm_cooldown()
        else:
            await self.config.dm_cooldown.set(max(0, seconds))
        await ctx.reply(f"Users will need to wait {max(0, seconds)} seconds between generations in DMs with the bot.")

    @novelaiset.command()
    @commands.guild_only()
    @commands.admin()
    async def nsfw_filter(self, ctx: commands.Context):
        """Toggles the NSFW filter for /novelai"""
        new = not await self.config.guild(ctx.guild).nsfw_filter()
        await self.config.guild(ctx.guild).nsfw_filter.set(new)
        if new:
            await ctx.reply("NSFW filter enabled in non-nsfw channels. Note that this is not perfect.")
        else:
            await ctx.reply("NSFW filter disabled. Images may more easily be NSFW by accident.")

    @novelaiset.command()
    @commands.is_owner()
    async def loadingemoji(self, ctx: commands.Context, emoji: Optional[discord.Emoji]):
        """Add your own Loading custom emoji with this command."""
        if emoji is None:
            self.loading_emoji = ""
            await self.config.loading_emoji.set(self.loading_emoji)
            await ctx.reply(f"No emoji will appear when showing position in queue.")
            return
        try:
            await ctx.react_quietly(emoji)
        except:
            await ctx.reply("I don't have access to that emoji. I must be in the same server to use it.")
        else:
            self.loading_emoji = str(emoji) + " "
            await self.config.loading_emoji.set(self.loading_emoji)
            await ctx.reply(f"{emoji} will now appear when showing position in queue.")
