import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from acobot import bot


class Q:
    def __init__(self,data):
        self.data=data
        self.from_user=type("U",(),{"id":bot.ADMIN_ID,"username":None})()
        self.answer=AsyncMock()
        self.edit_message_text=AsyncMock()


@pytest.mark.asyncio
async def test_sell_second_confirmation_rechecks_floor_before_executor(monkeypatch):
    key="sell-e2e"
    offer={"mode":"sell","id":"order-1","chain":"ethereum","contract":"0x"+"22"*20,
           "token_id":"7","price_eth":1.2,"floor_eth":1.0,"expires":time.time()+60}
    bot.SNIPE_PENDING[key]=offer
    calls=[]
    monkeypatch.setattr(bot, "CFG", type("Config", (), {"chains":{"ethereum":object()}})())
    monkeypatch.setattr(bot.snipeprice,"fetch_top_offer",lambda *a,**k: dict(offer,price_eth=1.2))
    monkeypatch.setattr(bot.wallets,"main_wallet",lambda _uid:{"address":"0x"+"11"*20})
    monkeypatch.setattr(bot.wallets,"signing_entries",lambda *_a,**_k:[(None,None,None,"fake-key")])
    monkeypatch.setattr(bot.snipeprice,"execute_sell",lambda *a,**k:(calls.append((a,k)) or (True,"sale submitted")))
    update=type("U",(),{"callback_query":Q(f"snb:sellc:{key}")})()
    await bot.on_snipe_cb(update,None)
    assert "confirm sale" in str(update.callback_query.edit_message_text.call_args)
    assert not calls
    update=type("U",(),{"callback_query":Q(f"snb:selly:{key}")})()
    await bot.on_snipe_cb(update,None)
    assert len(calls)==1 and calls[0][0][1]==1.0
    assert key not in bot.SNIPE_PENDING


@pytest.mark.asyncio
async def test_sell_second_confirmation_stops_if_live_offer_below_floor(monkeypatch):
    key="sell-below"
    offer={"mode":"sell","id":"order-2","chain":"ethereum","contract":"0x"+"22"*20,
           "token_id":"8","price_eth":1.2,"floor_eth":1.0,"expires":time.time()+60}
    bot.SNIPE_PENDING[key]=offer
    calls=[]
    monkeypatch.setattr(bot.snipeprice,"fetch_top_offer",lambda *a,**k: dict(offer,price_eth=.99))
    monkeypatch.setattr(bot.snipeprice,"execute_sell",lambda *a,**k:calls.append((a,k)))
    update=type("U",(),{"callback_query":Q(f"snb:selly:{key}")})()
    await bot.on_snipe_cb(update,None)
    assert not calls and key not in bot.SNIPE_PENDING
    assert "fell below your floor" in update.callback_query.edit_message_text.await_args.args[0]
