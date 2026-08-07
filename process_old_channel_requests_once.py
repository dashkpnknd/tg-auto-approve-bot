"""One-time handling of pending requests in the old 'Ai ТЕНДЕР СОФТ' channel."""
import asyncio
import contextlib
import json
import logging
import random
from collections import defaultdict

from telethon import functions, types
from telethon.extensions import html

from campaign_engine import client_for, send_campaign_message, store

RUN_ID = "oneoff_ai_tender_soft_20260807"
APPROVER_ID = "52f4c4540ed64d62"
CHANNEL_ID = -1004293815318
IMAGE_PATH = "third_ai_tender_soft.jpg"
LOG = logging.getLogger("oneoff_old_channel")

# These were the 15 requests approved in the original one-off run.  The two
# remaining requests have no source dialog and deliberately stay manual.
RECOVERED_APPROVED_IDS = {
    258885692, 403962196, 213034653, 1942278143, 384048134,
    841573975, 857883200, 7729589353, 241936661, 339342971,
    5186908756, 1843568683, 558860242, 1641301912, 568470813,
}


async def send_third_with_image(client, peer, content):
    if isinstance(content, dict) and content.get("html"):
        text, entities = html.parse(content["html"])
        return await client.send_file(peer, IMAGE_PATH, caption=text, formatting_entities=entities)
    return await client.send_file(peer, IMAGE_PATH, caption=content)


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with open("accounts.json", encoding="utf-8") as file:
        accounts = json.load(file)
    with open("campaigns.json", encoding="utf-8") as file:
        report_channel_id = json.load(file)["ai_tenders_20260806"]["report_channel_id"]

    clients = {}
    peers = defaultdict(dict)
    try:
        for account_id, account in accounts.items():
            client = client_for(account)
            await client.connect()
            if not await client.is_user_authorized():
                raise RuntimeError(f"Account {account_id} is not authorized")
            clients[account_id] = client

        approver = clients[APPROVER_ID]
        # Approval has already happened.  This recovery pass sends the third
        # message only and never touches join requests again.
        pending_ids = set(RECOVERED_APPROVED_IDS)
        owners = defaultdict(list)
        for account_id, client in clients.items():
            async for dialog in client.iter_dialogs(limit=3000):
                entity = dialog.entity
                if isinstance(entity, types.User) and entity.id in pending_ids:
                    owners[entity.id].append(account_id)
                    peers[account_id][entity.id] = entity

        ready = {user_id: account_ids[0] for user_id, account_ids in owners.items() if len(account_ids) == 1}
        manual_ids = pending_ids.difference(ready)
        for user_id in manual_ids:
            details = "dialog not found" if user_id not in owners else ",".join(owners[user_id])
            store.queue(RUN_ID, user_id, "sender_not_unique", details)

        await approver.send_message(report_channel_id, f"Восстановительная отправка 3-го сообщения начата\nОдобренных заявок: {len(pending_ids)}\nГотово к отправке: {len(ready)}\nВручную: {len(manual_ids)}")
        batches = defaultdict(list)
        for user_id, account_id in ready.items():
            batches[account_id].append(user_id)

        summary = {"sent": 0, "errors": 0}
        async def send_batch(account_id, user_ids):
            client = clients[account_id]
            messages = accounts[account_id].get("third_messages") or []
            if not messages:
                for user_id in user_ids:
                    store.queue(RUN_ID, user_id, "third_message_not_configured", account_id)
                    summary["errors"] += 1
                return
            for user_id in user_ids:
                row = store.bind(RUN_ID, user_id, account_id)
                if row["third_sent_at"]:
                    continue
                # The current one-time queue waits 20–30 minutes before every
                # new dialog, independently for each sender account.
                await asyncio.sleep(random.randint(1200, 1800))
                try:
                    peer = peers[account_id][user_id]
                    await send_third_with_image(client, peer, random.choice(messages))
                    with contextlib.suppress(Exception):
                        await client.send_read_acknowledge(peer)
                    if store.mark_once(RUN_ID, user_id, 3):
                        summary["sent"] += 1
                except Exception as exc:
                    summary["errors"] += 1
                    store.queue(RUN_ID, user_id, "third_message_error", str(exc)[:300])
                    LOG.exception("Third message failed for %s", user_id)

        await asyncio.gather(*(send_batch(account_id, user_ids) for account_id, user_ids in batches.items()))
        await approver.send_message(report_channel_id, f"Восстановительная отправка 3-го сообщения завершена\nОтправлено: {summary['sent']}\nОшибок: {summary['errors']}\nОставлено вручную: {len(manual_ids)}")
    finally:
        for client in clients.values():
            with contextlib.suppress(Exception):
                await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
