"""One-time processing of already unread campaign replies.

Run manually once; it is intentionally not imported by the bot UI.
"""
import asyncio
import contextlib
import json
import logging
import random
from collections import defaultdict

from telethon import TelegramClient, types
from telethon.sessions import StringSession

from campaign_engine import client_for, send_campaign_message, store

CAMPAIGN_ID = "ai_tenders_20260806"
LOG = logging.getLogger("oneoff_old_replies")


async def report(client, chat_id, text):
    with contextlib.suppress(Exception):
        await client.send_message(chat_id, text)


async def scan_account(account_id, account):
    client = client_for(account)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError("session is not authorized")

    # Keep the client connected: a Telegram user id alone lacks the access hash
    # required to send a message after reconnecting.
    peers = {}
    async for dialog in client.iter_dialogs(limit=3000):
        entity = dialog.entity
        if isinstance(entity, types.User) and not getattr(entity, "bot", False) and dialog.unread_count:
            peers[entity.id] = entity
    return client, peers


async def send_from_account(account_id, account, client, peers, user_ids, summary):
    messages = account.get("second_messages") or []
    if not messages:
        summary["errors"] += len(user_ids)
        for user_id in user_ids:
            store.queue(CAMPAIGN_ID, user_id, "second_message_not_configured", account_id)
        return

    try:
        first_message = True
        for user_id in user_ids:
            binding = store.binding(CAMPAIGN_ID, user_id)
            if binding and binding["account_id"] != account_id:
                summary["skipped"] += 1
                store.queue(CAMPAIGN_ID, user_id, "multiple_sender_accounts", "existing different binding")
                continue
            if not binding:
                binding = store.bind(CAMPAIGN_ID, user_id, account_id)
            if binding["second_sent_at"]:
                summary["skipped"] += 1
                continue

            # This one-time backlog is intentionally much slower than normal
            # campaign replies: every new dialog waits 20–30 minutes.
            await asyncio.sleep(random.randint(1200, 1800))
            try:
                await send_campaign_message(client, peers[user_id], random.choice(messages))
                with contextlib.suppress(Exception):
                    await client.send_read_acknowledge(peers[user_id])
                if store.mark_once(CAMPAIGN_ID, user_id, 2):
                    summary["sent"] += 1
                else:
                    summary["skipped"] += 1
            except Exception as exc:
                summary["errors"] += 1
                store.queue(CAMPAIGN_ID, user_id, "second_message_error", str(exc)[:300])
                LOG.exception("Could not send to %s", user_id)
            finally:
                first_message = False
    except Exception as exc:
        summary["errors"] += len(user_ids)
        for user_id in user_ids:
            store.queue(CAMPAIGN_ID, user_id, "sender_account_unavailable", str(exc)[:300])
        LOG.exception("Account unavailable: %s", account_id)


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with open("accounts.json", encoding="utf-8") as file:
        accounts = json.load(file)
    with open("campaigns.json", encoding="utf-8") as file:
        campaign = json.load(file)[CAMPAIGN_ID]

    scanned = await asyncio.gather(*(scan_account(account_id, account) for account_id, account in accounts.items()))
    clients = {account_id: client for (account_id, _), (client, _) in zip(accounts.items(), scanned)}
    peers_by_account = {account_id: peers for (account_id, _), (_, peers) in zip(accounts.items(), scanned)}
    owners = defaultdict(list)
    for account_id, peers in peers_by_account.items():
        for user_id in peers:
            owners[user_id].append(account_id)

    batches = defaultdict(list)
    ambiguous = 0
    for user_id, account_ids in owners.items():
        if len(account_ids) == 1:
            batches[account_ids[0]].append(user_id)
        else:
            ambiguous += 1
            store.queue(CAMPAIGN_ID, user_id, "multiple_sender_accounts", ",".join(account_ids))

    approver = client_for(accounts[campaign["approver_account_id"]])
    await approver.connect()
    await report(approver, campaign["report_channel_id"], f"Разовая обработка старых ответов начата\nКандидатов: {sum(len(v) for v in batches.values())}\nНеоднозначных: {ambiguous}")
    summary = {"sent": 0, "skipped": 0, "errors": 0}
    try:
        await asyncio.gather(*(send_from_account(account_id, accounts[account_id], clients[account_id], peers_by_account[account_id], user_ids, summary) for account_id, user_ids in batches.items()))
        await report(approver, campaign["report_channel_id"], f"Разовая обработка старых ответов завершена\nОтправлено: {summary['sent']}\nПропущено: {summary['skipped']}\nОшибок: {summary['errors']}\nНеоднозначных: {ambiguous}")
    finally:
        await approver.disconnect()
        for client in clients.values():
            with contextlib.suppress(Exception):
                await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
