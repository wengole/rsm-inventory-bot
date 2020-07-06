import asyncio
import json
import logging
import math
import re
import sys
from collections import Counter
from datetime import datetime

import discord
import redis
from dhooks import Embed
from dynaconf import settings
from esipy import EsiApp, EsiSecurity, EsiClient
from esipy.cache import RedisCache
from esipy.events import AFTER_TOKEN_REFRESH

LOG_LEVEL = getattr(logging, settings.LOG_LEVEL.upper())
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(name)-25.25s[%(lineno)-6d] : %(funcName)-18.18s : %(levelname)-8s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)
client = discord.Client()
redis_client = redis.from_url(settings.REDIS_URL)
cache = RedisCache(redis_client)

esiapp = EsiApp(cache=cache).get_latest_swagger

# init the security object
esisecurity = EsiSecurity(
    redirect_uri=settings.ESI_CALLBACK,
    client_id=settings.ESI_CLIENT_ID,
    secret_key=settings.ESI_SECRET_KEY,
    headers={"User-Agent": settings.ESI_USER_AGENT},
)


millnames = ["", " k", " M", " B", " T"]


def millify(n):
    n = float(n)
    millidx = max(
        0,
        min(
            len(millnames) - 1, int(math.floor(0 if n == 0 else math.log10(abs(n)) / 3))
        ),
    )

    return f"{n / 10 ** (3 * millidx):.1f}{millnames[millidx]}"


def update_stored_tokens(
    access_token: str, refresh_token: str, expires_in: int, token_type: str, **kwargs
):
    redis_client.set(
        "esi_tokens",
        json.dumps(
            {
                "access_token": access_token,
                "expires_in": expires_in,
                "token_type": token_type,
                "refresh_token": refresh_token,
                "token_expiry": datetime.utcnow().timestamp() + expires_in,
            }
        ),
    )


tokens = redis_client.get("esi_tokens")
if tokens:
    tokens = json.loads(tokens)
    expiry = tokens.pop("token_expiry", 0)
    tokens["expires_in"] = int(expiry - datetime.utcnow().timestamp())
    if tokens["expires_in"] < 0:
        tokens["access_token"] = ""
else:
    tokens = {
        "access_token": "",
        "expires_in": -1,
        "refresh_token": settings.ESI_REFRESH_TOKEN,
    }
esisecurity.update_token(tokens)
if tokens["expires_in"] < 0:
    esisecurity.refresh()

update_stored_tokens(
    access_token=esisecurity.access_token,
    refresh_token=esisecurity.refresh_token,
    expires_in=int(esisecurity.token_expiry - datetime.utcnow().timestamp()),
    token_type="Bearer",
)

# init the client
esiclient = EsiClient(
    retry_requests=True,
    security=esisecurity,
    cache=cache,
    headers={"User-Agent": settings.ESI_USER_AGENT},
)
AFTER_TOKEN_REFRESH.add_receiver(update_stored_tokens)


@client.event
async def on_ready():
    guild = discord.utils.find(
        lambda g: g.id == settings.DISCORD_GUILD_ID, client.guilds
    )
    logger.info(f"{client.user} has connected to {guild.name}!")


@client.event
async def on_message(message: discord.Message):
    if client.user not in message.mentions:
        # Only respond to direct mentions
        return
    logger.info(message)
    system = re.sub(r"<@!\w+>", "", message.content)
    system_id = None
    if len(system) >= 3:
        response = esiclient.request(
            esiapp.op["get_search"](
                categories=["solar_system"], search=system, strict=False
            )
        )
        if response.status == 200 and response.data:
            system_id = getattr(response.data, "solar_system", None)
            if system_id:
                system_id = system_id[0]
    system_name = None
    if system_id is not None:
        response = esiclient.request(
            esiapp.op["get_universe_systems_system_id"](system_id=system_id)
        )
        if response.status == 200 and response.data:
            system_name = getattr(response.data, "name", None)

    channel: discord.TextChannel = message.channel
    await channel.send(f"Loading contracts...", delete_after=10)
    op = esiapp.op["get_corporations_corporation_id_contracts"](
        corporation_id=settings.CORP_ID
    )
    contracts = esiclient.request(op)
    if contracts.status == 403:
        esisecurity.refresh()
        contracts = esiclient.request(op)
    open_contracts = [x for x in contracts.data if x.status == "outstanding"]
    structure_ids = {
        x.start_location_id for x in contracts.data if x.start_location_id >= 100000000
    }
    ops = [
        esiapp.op["get_universe_structures_structure_id"](structure_id=x)
        for x in structure_ids
    ]
    structure_responses = {
        x[0]._p["path"]["structure_id"]: x[1] for x in esiclient.multi_request(ops)
    }
    response_errors = [x for x in structure_responses.values() if x.status != 200]
    if any(response_errors):
        logger.error(f"{len(response_errors)} errors")
        for response in response_errors:
            logger.error(f"{response.data.error}")
    structures = [
        int(k)
        for k, v in structure_responses.items()
        if v.data.solar_system_id == system_id
    ]
    if system_id:
        open_contracts = [
            x for x in open_contracts if x.start_location_id in structures
        ]
    open_contracts = {x.contract_id: x for x in open_contracts}
    contracts_to_load = []
    for contract_id in open_contracts.keys():
        contract = redis_client.get(f"parsed_contract_{contract_id}")
        if contract:
            open_contracts[contract_id] = json.loads(contract)
        else:
            contracts_to_load.append(contract_id)
    ship_lookup = {ship["id"]: ship for ship in settings.SHIPS}
    ops = [
        esiapp.op["get_corporations_corporation_id_contracts_contract_id_items"](
            corporation_id=settings.CORP_ID, contract_id=contract_id
        )
        for contract_id in contracts_to_load
    ]
    reqs_and_resps = esiclient.multi_request(ops)
    for req, response in reqs_and_resps:
        contract_id = int(req._p["path"]["contract_id"])
        if response.data is not None and response.status == 200:
            open_contracts[contract_id].items = response.data
            redis_client.set(
                f"parsed_contract_{contract_id}",
                json.dumps(open_contracts[contract_id], default=str),
            )
        elif response.data is None:
            logger.warning("No response data for {}", contract_id)
            open_contracts.pop(contract_id)
        else:
            logger.warning(
                "Response for {} was {response.status}: {response.data}",
                contract_id,
                response=response,
            )
            open_contracts.pop(contract_id)
    items = Counter(
        [
            x["type_id"]
            for contract in open_contracts.values()
            for x in contract.get("items", [])
        ]
    )
    ships = {ship_id: items[ship_id] for ship_id in ship_lookup.keys()}
    embed = Embed(
        description=f"Doctrine ships on contract in {system_name}",
        color=0x03FC73,
        timestamp="now",
    )
    embed.set_author(
        name="RSM Inventory",
        icon_url="https://images.evetech.net/corporations/1003900783/logo?size=32",
    )
    embed.set_thumbnail(url="https://images.evetech.net/types/597/render?size=64")
    for ship_id, count in ships.items():
        price = ship_lookup[ship_id].get("price", None) or settings.PRICE
        name = f"{ship_lookup[ship_id]['name']} {millify(price)}"
        embed.add_field(
            name=name, value=f"{count} of {ship_lookup[ship_id]['max']}",
        )
    embed_dict = embed.to_dict()
    await channel.send(content="", embed=discord.Embed.from_dict(embed_dict))


if __name__ == "__main__":
    logger.debug(f"Running")

    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(client.start(settings.DISCORD_BOT_TOKEN))
    except KeyboardInterrupt:
        loop.run_until_complete(client.logout())
        # cancel all tasks lingering
    finally:
        loop.close()
