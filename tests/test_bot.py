import asyncio
import copy
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from telegram.error import BadRequest, Forbidden, TimedOut

import bot
import billing
import db
import texts


URL = "https://youtu.be/dQw4w9WgXcQ"
OFFER = "https://example.com/offer"


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original_path = db.DB_PATH
        db.DB_PATH = Path(self.temp.name) / "test.db"
        db._db_lock = asyncio.Lock()
        bot._user_locks.clear()
        self.config = patch.multiple(bot, ADMIN_IDS={99}, YOOKASSA_SHOP_ID="test-shop",
                                     YOOKASSA_SECRET_KEY="test-only-placeholder", FOLLOWUP_ENABLED=True,
                                     PROJECT_KEY="creative_brain", OFFER_VERSION="1", CHANNEL_ID=-1001234567890,
                                     YOOKASSA_RETURN_URL="https://example.com/return")
        self.config.start()
        await db.init_db()
        await db.upsert_user(1, "test", "Test")
        await db.set_lead_magnet_url(URL)
        await db.set_offer_url(OFFER)
        self.messages = {}
        self.deleted = set()
        self.last_active_id = None
        self.next_id = 1

        async def send(chat_id, text, **kwargs):
            mid = self.next_id
            self.next_id += 1
            self.messages[mid] = dict(chat_id=chat_id, text=text, **kwargs)
            self.last_active_id = mid
            return NS(message_id=mid)

        async def edit(chat_id, message_id, text, **kwargs):
            if message_id in self.deleted or message_id not in self.messages:
                raise BadRequest('message to edit not found')
            self.messages[message_id] = dict(chat_id=chat_id, text=text, **kwargs)
            self.last_active_id = message_id

        async def delete(chat_id, message_id):
            self.deleted.add(message_id)

        self.telegram = NS(
            send_message=AsyncMock(side_effect=send),
            edit_message_text=AsyncMock(side_effect=edit),
            delete_message=AsyncMock(side_effect=delete),
            edit_message_reply_markup=AsyncMock(),
            create_chat_invite_link=AsyncMock(return_value=NS(invite_link="https://t.me/+test-invite")),
            revoke_chat_invite_link=AsyncMock(),
            ban_chat_member=AsyncMock(),
            unban_chat_member=AsyncMock(),
            approve_chat_join_request=AsyncMock(),
            decline_chat_join_request=AsyncMock(),
        )
        self.ctx = NS(bot=self.telegram, args=[], user_data={})
        self.user = NS(id=1, username="test", first_name="Test", last_name=None)
        self.chat = NS(id=1, type="private")
        self.update = NS(effective_user=self.user, effective_chat=self.chat,
                         effective_message=NS(message_id=9000, text="/start"))
        self.http = MagicMock()
        self.http.__aenter__ = AsyncMock(return_value=self.http)
        self.http.__aexit__ = AsyncMock(return_value=False)
        self.http.post = AsyncMock(return_value=self.response(self.payment()))
        self.http.get = AsyncMock(return_value=self.response(self.payment()))
        self.provider_payments = {}
        self.http.post.side_effect = self.provider_post
        self.http.get.side_effect = self.provider_get
        self.client_patch = patch.object(bot.httpx, "AsyncClient", return_value=self.http)
        self.client_patch.start()

    async def asyncTearDown(self):
        self.client_patch.stop()
        self.config.stop()
        await db.close_db()
        db.DB_PATH = self.original_path
        self.temp.cleanup()

    def response(self, value):
        return NS(raise_for_status=lambda: None, json=lambda: value)

    async def provider_post(self, url, **kwargs):
        value=copy.deepcopy(self.http.post.return_value.json())
        order=await db.get_checkout(kwargs['headers']['Idempotence-Key'])
        if order and order['checkout_group_id']!=order['id']:
            route=order['payment_route']
            value.update(id='alt-'+order['id'],metadata=kwargs['json']['metadata'],amount=kwargs['json']['amount'])
            value['confirmation']={'confirmation_url':'https://example.com/pay-'+route}
            if route=='sbp':
                value['payment_method']={'type':'sbp'}
            else:
                value.pop('payment_method',None)
        elif order and kwargs['json']['metadata'].get('quiz_token'):
            value.update(id='quiz-'+order['id'],metadata=kwargs['json']['metadata'],amount=kwargs['json']['amount'])
            value['confirmation']={'confirmation_url':'https://example.com/pay-quiz-sbp'}
            value['payment_method']={'type':'sbp'}
        self.provider_payments[value['id']]=value
        return self.response(value)

    async def provider_get(self, url, **kwargs):
        pid=url.rsplit('/',1)[1]
        configured=self.http.get.return_value.json()
        if pid.startswith(('alt-','quiz-')) and configured['id']!=pid:
            return self.response(self.provider_payments[pid])
        return self.http.get.return_value

    def payment(self, pid="p1", status="pending", plan=None, project="creative_brain"):
        plan = plan or bot.CURRENT_TARIFF
        return {
            "id": pid, "status": status, "paid": status == "succeeded",
            "amount": {"value": f"{plan.amount_rub:.2f}", "currency": "RUB"},
            "metadata": {"project_key": project, "user_id": "1", "kind": "channel_access",
                         "tariff_code": plan.code, "days": str(plan.days)},
            "confirmation": {"confirmation_url": "https://example.com/pay"},
        }

    async def store_payment(self, pid="p1", status="pending", plan=None, project="creative_brain"):
        plan = plan or bot.CURRENT_TARIFF
        value = self.payment(pid, status, plan, project)
        await db.create_payment(1, pid, plan.code, plan.amount_rub, "https://example.com/pay",
                                value["metadata"], value)
        return value

    async def press(self, data, user=None, message_id=None):
        user = user or self.user
        if message_id is None:
            candidates=[mid for mid,value in self.messages.items() if value['chat_id']==user.id and mid not in self.deleted]
            message_id=max(candidates) if candidates else None
        message=self.messages.get(message_id,{})
        query = NS(from_user=user, message=NS(chat_id=user.id, chat=NS(type="private"),
                                           message_id=message_id,text=message.get('text'),
                                           reply_markup=message.get('reply_markup')),
                   data=data, answer=AsyncMock())
        await bot.router(NS(callback_query=query), self.ctx)

    def last(self):
        return self.messages[self.last_active_id]

    def buttons(self):
        return [b for row in self.last()["reply_markup"].inline_keyboard for b in row]

    async def sql(self, sql, params=()):
        con = await db.connection()
        cur = await con.execute(sql, params)
        await con.commit()
        return await cur.fetchall()

    async def age_lead(self, hours=23, uid=1):
        await db.record_event(uid, "lead_magnet_sent")
        await self.sql("UPDATE funnel_events SET created_at = ? WHERE tg_user_id = ? AND event_name = 'lead_magnet_sent'",
                       ((datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(), uid))

    async def test_full_new_user_funnel_and_personal_invite(self):
        self.ctx.args = ["instagram_creative"]
        await bot.start(self.update, self.ctx)
        self.assertEqual(self.messages[1]["text"], texts.WELCOME)
        self.assertNotIn("link_preview_options", self.messages[1])
        self.assertEqual(self.telegram.send_message.await_count,1)
        self.assertEqual((await db.get_lesson_message(1))['lesson_message_id'],0)
        lesson_button=next(b for b in self.buttons() if b.text=='▶️ Бесплатный урок')
        await self.press(lesson_button.callback_data)
        lesson = dict(self.messages[2])
        self.assertIn(URL, lesson["text"])
        preview = lesson["link_preview_options"]
        self.assertTrue(preview.prefer_large_media)
        self.assertFalse(preview.show_above_text)
        self.assertFalse(preview.is_disabled)
        self.assertFalse(await db.has_accepted_offer(1))
        await self.press("user:product")
        self.assertEqual(self.last()["text"], texts.PRODUCT)
        await self.press("user:buy")
        self.assertIn(texts.OFFER, self.last()["text"])
        self.assertEqual(self.buttons()[0].url, OFFER)
        self.assertEqual(self.buttons()[1].text, "Через СБП")
        self.assertEqual(self.buttons()[1].url, "https://example.com/pay")
        self.assertIsNone(self.buttons()[1].callback_data)
        self.assertFalse(await db.has_accepted_offer(1))
        payload = self.http.post.call_args_list[0].kwargs["json"]
        self.assertEqual(payload["amount"], {"value": "400.00", "currency": "RUB"})
        self.assertEqual(payload["metadata"]["days"], "30")
        self.assertEqual(payload["confirmation"]["return_url"], "https://example.com/return")
        self.assertIn("Креативному мозгу", payload["description"])
        self.assertNotIn("save_payment_method", payload)
        self.http.get.return_value = self.response(self.payment(status="succeeded"))
        await self.press("user:check_payment:p1")
        sub = await db.get_subscription(1)
        self.assertAlmostEqual((db.parse_dt(sub["access_until"]) - db.utcnow()).total_seconds(), 30 * 86400, delta=5)
        args = self.telegram.create_chat_invite_link.call_args.kwargs
        self.assertEqual(args["chat_id"], bot.CHANNEL_ID)
        self.assertTrue(args["creates_join_request"])
        self.assertNotIn("member_limit", args)
        self.assertAlmostEqual((args["expire_date"] - db.utcnow()).total_seconds(), 3600, delta=5)
        self.assertEqual(len(self.buttons()), 2)
        self.assertEqual(self.buttons()[0].url, "https://t.me/+test-invite")
        self.assertNotIn("https://t.me/", self.last()["text"])
        self.assertEqual(self.messages[2], lesson)
        self.assertIn(1,self.deleted)
        self.assertNotIn(2,self.deleted)
        events = [row["event_name"] for row in await db.get_user_path(1)]
        self.assertEqual(events, ["start", "lead_magnet_sent", "product_opened", "buy_clicked",
                                  "payment_created", "payment_created", "offer_accepted", "payment_succeeded", "access_granted"])
        success_id = max(self.messages)
        sent = self.telegram.send_message.await_count
        await bot.start(self.update, self.ctx)
        self.assertIn('активен до',self.last()['text'])
        self.assertEqual(self.telegram.send_message.await_count,sent+1)
        self.assertIn(success_id,self.deleted)

    async def test_repeat_start_preserves_first_source_without_duplicate_users(self):
        self.ctx.args = ["youtube_creative"]
        await bot.start(self.update, self.ctx)
        self.ctx.args = ["reels_creative"]
        await bot.start(self.update, self.ctx)
        self.ctx.args = []
        await bot.start(self.update, self.ctx)
        rows = await self.sql("SELECT * FROM users WHERE tg_user_id = 1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["first_source"], "youtube_creative")
        self.assertEqual(len(await self.sql("SELECT * FROM payments")), 0)
        self.assertEqual(len([r for r in await db.get_user_path(1) if r["event_name"] == "start"]), 3)

    async def test_active_start_and_early_renewal(self):
        original = await db.grant_access(1, 12)
        await bot.start(self.update, self.ctx)
        self.assertIn(texts.ACTIVE_ACCESS.format(date=bot.format_dt(original["access_until"])), self.last()["text"])
        self.assertEqual(self.buttons()[0].callback_data, "user:access")
        await db.accept_offer(1)
        self.http.post.return_value = self.response(self.payment())
        await self.press("user:buy")
        await self.press("user:accept_offer_and_pay")
        self.assertEqual(self.http.post.await_count, 2)
        self.http.get.return_value = self.response(self.payment(status="succeeded"))
        await self.press("user:check_payment:p1")
        self.assertEqual(db.parse_dt((await db.get_subscription(1))["access_until"]),
                         db.parse_dt(original["access_until"]) + timedelta(days=30))
        self.assertIn("renewal_started", [r["event_name"] for r in await db.get_user_path(1)])

    async def test_repeated_check_and_concurrent_finalization_are_idempotent(self):
        await self.store_payment(status="succeeded")
        results = await asyncio.gather(*[
            db.finalize_payment_once("p1", 1, "creative_brain", "channel_access", "creative_30", 30)
            for _ in range(5)
        ], db.record_event(1, "start"))
        self.assertEqual(sum(not r["already_finalized"] for r in results[:-1]), 1)
        before = (await db.get_subscription(1))["access_until"]
        await self.press("user:check_payment:p1")
        await self.press("user:check_payment:p1")
        self.assertEqual((await db.get_subscription(1))["access_until"], before)
        self.assertEqual(self.telegram.create_chat_invite_link.await_count, 1)
        self.assertEqual((await db.stats())["revenue_rub"], 400)
        self.assertEqual(len(await self.sql("SELECT 1 FROM funnel_events WHERE event_name = 'payment_succeeded'")), 1)

    async def test_pending_and_waiting_for_capture_do_not_grant(self):
        await self.store_payment()
        for status in ("pending", "waiting_for_capture"):
            value = self.payment(status=status)
            value["paid"] = True
            self.http.get.return_value = self.response(value)
            await self.press("user:check_payment:p1")
            self.assertIsNone(await db.get_subscription(1))
            self.assertTrue(self.last()["text"].startswith(texts.PAYMENT_PENDING))
            self.assertNotIn('нажмите Ещё',self.last()["text"])
            self.assertIsNone(self.last().get('entities'))
            self.assertEqual(self.buttons()[0].text, "✅ Проверить ещё раз")
        result = await db.finalize_payment_once("p1", 1, "creative_brain", "channel_access", "creative_30", 30)
        self.assertFalse(result["ok"])

    async def test_legacy_invoices_survive_project_change_and_keep_duration(self):
        plan = bot.PAYMENT_PLANS["m3"]
        await self.store_payment(plan=plan, project="previous_project")
        self.http.get.return_value = self.response(self.payment(status="succeeded", plan=plan, project="previous_project"))
        await self.press("user:check_payment:p1")
        self.assertAlmostEqual((db.parse_dt((await db.get_subscription(1))["access_until"]) - db.utcnow()).days, 89)
        self.assertEqual(json.loads((await db.get_payment("p1"))["metadata_json"])["project_key"], "previous_project")

    async def test_tampered_payment_identity_amount_and_metadata_rejected(self):
        await self.store_payment()
        for field in ("id", "amount", "metadata", "paid"):
            value = self.payment(status="succeeded")
            value[field] = {"id": "wrong", "amount": {"value": "1.00", "currency": "RUB"},
                            "metadata": {"project_key": "wrong"}, "paid": False}[field]
            self.http.get.return_value = self.response(value)
            await self.press("user:check_payment:p1")
            self.assertIsNone(await db.get_subscription(1))
        await self.press("user:check_payment:unknown")
        self.assertIsNone(await db.get_subscription(1))

    async def test_yookassa_errors_and_malformed_json_are_contained(self):
        await db.accept_offer(1)
        for exc in (httpx.ConnectError("test"), ValueError("json"), TypeError("shape")):
            self.http.post.side_effect = exc
            await self.press("user:buy")
            await self.press("user:accept_offer_and_pay")
            self.assertIn("Проверяю результат", self.last()["text"])
        await self.store_payment()
        self.http.get.side_effect = httpx.ReadTimeout("test")
        await self.press("user:check_payment:p1")
        self.assertIsNone(await db.get_subscription(1))
        self.assertIn("Подтверждение пока недоступно", self.last()["text"])

    async def test_canceled_payment_can_create_new_invoice(self):
        await db.accept_offer(1)
        await self.store_payment()
        self.http.get.return_value = self.response(self.payment(status="canceled"))
        await self.press("user:check_payment:p1")
        self.http.post.return_value = self.response(self.payment(pid="p2"))
        await self.press("user:buy")
        await self.press("user:accept_offer_and_pay")
        self.assertIsNotNone(await db.get_payment("p2"))

    async def test_double_buy_reuses_invoice_and_legacy_buttons_only_open_product(self):
        await db.accept_offer(1)
        self.http.post.return_value = self.response(self.payment())
        await self.press("user:buy")
        await self.press("user:accept_offer_and_pay")
        self.http.get.return_value = self.response(self.payment())
        await self.press("user:buy")
        await self.press("user:accept_offer_and_pay")
        self.assertEqual(self.http.post.await_count, 2)
        for route in ("user:tariff:m1_first", "user:tariff:m1", "user:tariff:m3", "user:review:0"):
            await self.press(route)
            self.assertEqual(self.last()["text"], texts.PRODUCT)
        self.assertEqual(self.http.post.await_count, 2)

    async def test_offer_missing_stale_or_changed_blocks_payment(self):
        await db.set_setting("auto_renew_enabled","1")
        await self.sql("DELETE FROM settings WHERE key = 'offer_url'")
        await self.press("user:buy")
        await self.press("user:accept_offer_and_pay")
        self.http.post.assert_not_awaited()
        await db.set_offer_url(OFFER)
        await self.press("user:buy")
        await db.set_offer_url(OFFER + "/new")
        await self.press("user:accept_offer_and_pay")
        self.http.post.assert_not_awaited()
        self.assertFalse(await db.has_accepted_offer(1))
        self.http.post.return_value = self.response(self.payment())
        await self.press("user:accept_offer_and_pay")
        self.assertTrue(await db.has_accepted_offer(1))

    async def test_invite_failure_retry_does_not_add_second_period(self):
        await self.store_payment()
        self.http.get.return_value = self.response(self.payment(status="succeeded"))
        self.telegram.create_chat_invite_link.side_effect = Forbidden("missing rights")
        await self.press("user:check_payment:p1")
        before = (await db.get_subscription(1))["access_until"]
        self.assertIn("Доступ сохранён", self.last()["text"])
        self.telegram.create_chat_invite_link.side_effect = None
        await self.press("user:check_payment:p1")
        self.assertEqual((await db.get_subscription(1))["access_until"], before)
        self.assertTrue(self.buttons()[0].url)

    async def test_expired_invite_is_replaced_without_extending_subscription(self):
        await self.store_payment(status="succeeded")
        result = await db.finalize_payment_once("p1", 1, "creative_brain", "channel_access", "creative_30", 30)
        await db.save_invite_link(1, "https://t.me/+expired", "p1", (db.utcnow() - timedelta(hours=1)).isoformat())
        await self.press("user:check_payment:p1")
        self.assertEqual(self.buttons()[0].url, "https://t.me/+test-invite")
        self.assertEqual((await db.get_subscription(1))["access_until"], result["access_until"])

    async def test_expiration_removes_from_env_channel_and_allows_purchase_again(self):
        await db.grant_access(1, 1)
        await self.sql("UPDATE subscriptions SET access_until = ?", ((db.utcnow() - timedelta(seconds=1)).isoformat(),))
        await bot.expire_subscriptions_job(self.ctx)
        self.telegram.ban_chat_member.assert_awaited_once_with(bot.CHANNEL_ID, 1)
        self.assertEqual((await db.get_subscription(1))["status"], "expired")
        self.assertEqual(self.last()["text"], texts.EXPIRED)
        await bot.expire_subscriptions_job(self.ctx)
        self.assertEqual(self.telegram.ban_chat_member.await_count, 1)
        await db.accept_offer(1)
        self.http.post.return_value = self.response(self.payment())
        await self.press("user:buy")
        await self.press("user:accept_offer_and_pay")
        self.http.get.return_value = self.response(self.payment(status="succeeded"))
        await self.press("user:check_payment:p1")
        self.assertTrue(bot.active_subscription_until(await db.get_subscription(1)))

    async def test_expiration_permission_failure_is_retried(self):
        await db.grant_access(1, 1)
        await self.sql("UPDATE subscriptions SET access_until = ?", ((db.utcnow() - timedelta(seconds=1)).isoformat(),))
        self.telegram.ban_chat_member.side_effect = Forbidden("rights")
        await bot.expire_subscriptions_job(self.ctx)
        self.assertEqual((await db.get_subscription(1))["status"], "active")
        self.telegram.ban_chat_member.side_effect = None
        await bot.expire_subscriptions_job(self.ctx)
        self.assertEqual((await db.get_subscription(1))["status"], "expired")

    async def test_reminder_once_and_reset_after_extension(self):
        await db.grant_access(1, 3)
        await bot.renewal_notices_job(self.ctx)
        await bot.renewal_notices_job(self.ctx)
        self.assertEqual(self.telegram.send_message.await_count, 1)
        self.assertEqual(self.buttons()[0].callback_data, "user:buy")
        await db.grant_access(1, 30)
        self.assertIsNone((await db.get_subscription(1))["renewal_notice_sent_at"])
        await bot.renewal_notices_job(self.ctx)
        self.assertEqual(self.telegram.send_message.await_count, 1)

    async def test_followup_once_across_restart_and_repeated_lesson(self):
        await self.age_lead()
        await db.record_event(1, "lead_magnet_sent")
        await asyncio.gather(bot.followup_job(self.ctx), bot.followup_job(self.ctx))
        self.assertEqual(self.telegram.send_message.await_count, 1)
        await db.close_db()
        await db.init_db()
        await bot.followup_job(self.ctx)
        self.assertEqual(self.telegram.send_message.await_count, 1)
        self.assertEqual(len(await self.sql("SELECT 1 FROM funnel_events WHERE event_name='followup_sent'")), 1)

    async def test_followup_disabled_and_not_too_early_or_too_late(self):
        await self.age_lead(19)
        await bot.followup_job(self.ctx)
        await self.age_lead(25)
        await bot.followup_job(self.ctx)
        await self.age_lead(23)
        with patch.object(bot, "FOLLOWUP_ENABLED", False):
            await bot.followup_job(self.ctx)
        self.telegram.send_message.assert_not_awaited()

    async def test_followup_skips_local_paid_and_active_users(self):
        await self.age_lead()
        await self.store_payment(status="succeeded")
        await db.upsert_user(2)
        await self.age_lead(uid=2)
        await db.grant_access(2, 30)
        await bot.followup_job(self.ctx)
        self.telegram.send_message.assert_not_awaited()

    async def test_followup_checks_all_remote_pending_payments(self):
        await self.age_lead()
        await self.store_payment("old")
        await self.store_payment("new")
        self.http.get.side_effect = [
            self.response(self.payment("old", "succeeded")),
            self.response(self.payment("new", "pending")),
        ]
        await bot.followup_job(self.ctx)
        self.telegram.send_message.assert_not_awaited()
        self.assertEqual(self.http.get.await_count, 2)
        self.assertEqual((await db.get_payment("old"))["status"], "succeeded")

    async def test_followup_remote_error_skips_and_timeout_never_repeats(self):
        await self.age_lead()
        await self.store_payment()
        self.http.get.side_effect = httpx.ConnectError("offline")
        await bot.followup_job(self.ctx)
        self.telegram.send_message.assert_not_awaited()
        self.http.get.side_effect = self.provider_get
        self.http.get.return_value = self.response(self.payment(status="canceled"))
        self.telegram.send_message.side_effect = TimedOut()
        await bot.followup_job(self.ctx)
        await bot.followup_job(self.ctx)
        self.assertEqual(self.telegram.send_message.await_count, 1)
        self.assertEqual(len(await self.sql("SELECT 1 FROM funnel_events WHERE event_name='followup_sent'")), 0)

    async def test_admin_url_edit_test_clear_and_legacy_alias(self):
        self.user.id = 99
        self.chat.id = 99
        self.update.effective_message.text = "/set_intro_video"
        await bot.admin_shortcut(self.update, self.ctx)
        self.assertEqual((await db.get_admin_state(99))["state"], "lead_magnet_url")
        self.update.effective_message.text = "javascript:alert(1)"
        await bot.handle_admin_input(self.update, self.ctx)
        self.assertEqual(await db.get_lead_magnet_url(), URL)
        self.update.effective_message.text = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        await bot.handle_admin_input(self.update, self.ctx)
        self.assertEqual(await db.get_lead_magnet_url(), self.update.effective_message.text)
        await self.press("admin:lead_test")
        self.assertIn(await db.get_lead_magnet_url(),self.last()['text'])
        self.assertFalse(self.last()['link_preview_options'].is_disabled)
        self.assertEqual(await db.get_user_path(99), [])
        await self.press("admin:lead_clear")
        self.assertIsNone(await db.get_lead_magnet_url())

    async def test_admin_grant_revoke_and_authorization(self):
        await self.press("admin:grant")
        self.assertIsNone(await db.get_admin_state(1))
        admin_user = NS(id=99, username="admin", first_name="Admin", last_name=None)
        await self.press("admin:grant", admin_user)
        update = NS(effective_user=admin_user, effective_chat=NS(id=99),
                    effective_message=NS(text="1 30", message_id=999))
        await bot.handle_admin_input(update, self.ctx)
        self.assertTrue(bot.active_subscription_until(await db.get_subscription(1)))
        await self.press("admin:revoke", admin_user)
        update.effective_message.text = "1"
        self.telegram.ban_chat_member.side_effect = Forbidden("rights")
        await bot.handle_admin_input(update, self.ctx)
        self.assertEqual((await db.get_subscription(1))["status"], "active")
        self.telegram.ban_chat_member.side_effect = None
        await bot.handle_admin_input(update, self.ctx)
        self.assertEqual((await db.get_subscription(1))["status"], "revoked")
        self.telegram.ban_chat_member.assert_awaited_with(bot.CHANNEL_ID, 1)

    async def test_fallback_does_not_delete_user_text(self):
        self.update.effective_message.text = "Привет!"
        await bot.fallback_message(self.update, self.ctx)
        self.assertEqual(self.last()["text"], texts.FALLBACK)
        self.telegram.delete_message.assert_not_awaited()
        await db.grant_access(1, 30)
        await bot.fallback_message(self.update, self.ctx)
        self.assertIn("активен до", self.last()["text"])

    async def test_plain_preview_fallback_and_missing_lesson(self):
        with patch.object(bot, "LinkPreviewOptions", None):
            await bot.send_lead_magnet(1, 1, self.ctx,welcome=False)
        self.assertFalse(self.last()["disable_web_page_preview"])
        await db.clear_lead_magnet_url()
        await bot.send_lead_magnet(1, 1, self.ctx,welcome=False)
        events = [r for r in await db.get_user_path(1) if r["event_name"] == "lead_magnet_sent"]
        self.assertEqual(len(events), 1)

    async def test_large_preview_api_rejection_falls_back(self):
        original_send = self.telegram.send_message.side_effect
        count = 0
        async def reject_first(*args, **kwargs):
            nonlocal count
            count += 1
            if "link_preview_options" in kwargs:
                raise BadRequest("unsupported link_preview_options")
            return await original_send(*args, **kwargs)
        self.telegram.send_message.side_effect = reject_first
        await bot.send_lead_magnet(1, 1, self.ctx,welcome=False)
        self.assertFalse(self.last()["disable_web_page_preview"])

    async def test_url_validation(self):
        for url in (URL, "http://youtube.com/watch?v=dQw4w9WgXcQ", "https://youtube.com/shorts/dQw4w9WgXcQ"):
            self.assertTrue(bot.valid_youtube_url(url))
        for url in ("javascript:alert(1)", "https://youtu.be.evil/dQw4w9WgXcQ", "https://youtube.com/",
                    "https://user:pass@youtu.be/dQw4w9WgXcQ", "https://youtu.be/abc def", "https://[broken"):
            self.assertFalse(bot.valid_youtube_url(url))

    async def test_stats_use_unique_cohorts_and_revenue_only_once(self):
        await db.record_start(1, "instagram_creative")
        await db.record_start(1, "youtube_creative")
        await db.record_event(1, "product_opened")
        await self.store_payment(status="succeeded")
        await db.finalize_payment_once("p1", 1, "creative_brain", "channel_access", "creative_30", 30)
        stats = await db.funnel_stats()
        self.assertEqual(stats["events"]["start"]["total"], 2)
        self.assertEqual(stats["events"]["start"]["users"], 1)
        self.assertEqual(stats["conversions"], {"start": 1, "product_opened": 1})
        self.assertEqual(stats["sources"], [{"source": "instagram_creative", "starts": 1, "paid": 1}])
        await bot.show_stats(99, 99, self.ctx)
        self.assertIn("100.0%", self.last()["text"])
        self.assertIn("400 ₽", self.last()["text"])

    async def test_join_revokes_personal_invite(self):
        await db.grant_access(1, 30)
        await db.save_invite_link(1, "https://t.me/+join", expire_at=(db.utcnow() + timedelta(hours=1)).isoformat())
        update = NS(chat_member=NS(chat=NS(id=bot.CHANNEL_ID),
                                    invite_link=NS(invite_link="https://t.me/+join"),
                                    new_chat_member=NS(status="member", user=NS(id=1))))
        await bot.channel_member_updated(update, self.ctx)
        self.telegram.revoke_chat_invite_link.assert_awaited_once_with(bot.CHANNEL_ID, "https://t.me/+join")
        self.assertEqual((await db.get_invite_link("https://t.me/+join"))["used"], 1)

    async def test_admin_menu_stats_and_user_path_routes(self):
        admin_user = NS(id=99, username="admin", first_name="Admin", last_name=None)
        await self.press("admin:menu", admin_user)
        self.assertEqual(len(self.buttons()), 11)
        await self.press("admin:stats", admin_user)
        self.assertIn("Воронка", self.last()["text"])
        await db.record_start(1, "shorts_creative")
        await self.press("admin:user_path", admin_user)
        update = NS(effective_user=admin_user, effective_chat=NS(id=99),
                    effective_message=NS(text="1", message_id=999))
        await bot.handle_admin_input(update, self.ctx)
        self.assertIn("shorts_creative", self.last()["text"])
        await self.press("admin:offer", admin_user)
        update.effective_message.text = "https://example.com/new-offer"
        await bot.handle_admin_input(update, self.ctx)
        self.assertEqual(await db.get_offer_url(), update.effective_message.text)

    async def test_non_admin_cannot_open_commands_callbacks_or_use_stale_admin_state(self):
        for command in ("/admin", "/stats", "/grant", "/revoke", "/set_offer", "/set_lead_magnet"):
            self.update.effective_message.text = command
            await (bot.admin if command == "/admin" else bot.admin_shortcut)(self.update, self.ctx)
        for route in ("admin:menu", "admin:stats", "admin:grant", "admin:revoke", "admin:offer",
                      "admin:lead_magnet", "admin:lead_set", "admin:lead_clear", "admin:user_path"):
            await self.press(route)
        self.telegram.send_message.assert_not_awaited()
        self.telegram.edit_message_text.assert_not_awaited()
        self.assertEqual(await db.get_lead_magnet_url(), URL)
        self.assertEqual(await db.get_offer_url(), OFFER)
        self.assertIsNone(await db.get_admin_state(1))
        await db.set_admin_state(1, "grant")
        self.update.effective_message.text = "1 30"
        self.assertFalse(await bot.handle_admin_input(self.update, self.ctx))
        self.assertIsNone(await db.get_subscription(1))
        self.telegram.ban_chat_member.assert_not_awaited()

    async def test_admin_command_works_and_group_admin_input_is_blocked(self):
        self.user.id = self.chat.id = 99
        self.update.effective_message.text = "/admin"
        await bot.admin(self.update, self.ctx)
        self.assertEqual(len(self.buttons()), 11)
        await db.set_admin_state(99, "grant")
        self.chat.id = -100123
        self.chat.type = "supergroup"
        self.update.effective_message.text = "1 30"
        self.assertFalse(await bot.handle_admin_input(self.update, self.ctx))
        self.assertIsNone(await db.get_subscription(1))
        before = self.telegram.send_message.await_count
        await bot.show_admin_menu(self.chat.id, 99, self.ctx)
        await bot.show_stats(self.chat.id, 99, self.ctx)
        self.assertEqual(before, self.telegram.send_message.await_count)

    async def test_admin_shortcut_on_every_user_screen_and_exit_from_input(self):
        def assert_menu():
            self.assertEqual(sum(b.callback_data == "admin:menu" for b in self.buttons()), 1)
        with patch.object(bot, "ADMIN_IDS", {1}):
            await bot.start(self.update, self.ctx)
            assert_menu()
            await self.press("user:product")
            assert_menu()
            await self.press("user:buy")
            assert_menu()
            self.http.post.return_value = self.response(self.payment())
            await self.press("user:accept_offer_and_pay")
            assert_menu()
            self.http.get.return_value = self.response(self.payment(status="succeeded"))
            await self.press("user:check_payment:p1")
            assert_menu()
            await bot.start(self.update, self.ctx)
            assert_menu()
            await bot.send_subscription_renewal_notice(1, (await db.get_subscription(1))["access_until"], self.ctx)
            assert_menu()
            await bot.send_subscription_expired_notice(1, self.ctx)
            assert_menu()
            await db.clear_lead_magnet_url()
            await bot.send_lead_magnet(1, 1, self.ctx)
            assert_menu()
            await self.press("admin:lead_set")
            self.assertEqual((await db.get_admin_state(1))["state"], "lead_magnet_url")
            assert_menu()
            await self.press("admin:menu")
            self.assertIsNone(await db.get_admin_state(1))
            self.assertEqual(len(self.buttons()), 11)

    async def test_regular_user_gets_no_admin_button_and_sees_price_before_consent(self):
        await bot.start(self.update, self.ctx)
        await self.press("user:product")
        self.assertLess(len(self.last()["text"]), 750)
        self.assertEqual(self.buttons()[0].text, "Я хочу")
        self.assertNotIn("400", self.last()["text"])
        await self.press("user:buy")
        self.assertIn("400", self.last()["text"])
        self.assertIn("30", self.last()["text"])
        self.assertEqual(sum("400" in b.text for b in self.buttons()), 0)
        self.assertEqual(self.buttons()[1].text, "Через СБП")
        self.assertEqual(self.buttons()[1].url, "https://example.com/pay")
        self.assertEqual(self.buttons()[2].text, "Другие способы")
        self.assertEqual(self.buttons()[2].url, "https://example.com/pay-all")
        self.assertEqual(len(self.buttons()), 4)
        for message in self.messages.values():
            markup = message.get("reply_markup")
            if markup:
                self.assertFalse(any((b.callback_data or "").startswith("admin:")
                                     for row in markup.inline_keyboard for b in row))

    async def test_revoked_payment_cannot_restore_access(self):
        await self.store_payment(status="succeeded")
        await db.finalize_payment_once("p1", 1, "creative_brain", "channel_access", "creative_30", 30)
        await db.revoke_access(1)
        await self.press("user:check_payment:p1")
        self.telegram.create_chat_invite_link.assert_not_awaited()
        self.assertEqual((await db.get_subscription(1))["status"], "revoked")

    async def test_invalid_start_parameter_uses_direct(self):
        self.ctx.args = ["invalid/source"]
        await bot.start(self.update, self.ctx)
        row = (await self.sql("SELECT first_source FROM users WHERE tg_user_id = 1"))[0]
        self.assertEqual(row["first_source"], "direct")

    async def test_all_legacy_plans_keep_original_amounts_and_durations(self):
        days = 0
        total = 0
        for code in ("m1_first", "m1", "m3"):
            plan = bot.PAYMENT_PLANS[code]
            await self.store_payment(code, plan=plan, project="previous_project")
            self.http.get.return_value = self.response(self.payment(code, "succeeded", plan, "previous_project"))
            await self.press(f"user:check_payment:{code}")
            days += plan.days
            total += plan.amount_rub
            self.assertAlmostEqual((db.parse_dt((await db.get_subscription(1))["access_until"]) - db.utcnow()).total_seconds(),
                                   days * 86400, delta=5)
            self.assertEqual((await db.stats())["revenue_rub"], total)

    async def test_finalize_failure_rolls_back_access_and_events(self):
        await self.store_payment(status="succeeded")
        with patch.object(db, "_insert_funnel_event", side_effect=RuntimeError("storage failure")):
            with self.assertRaises(RuntimeError):
                await db.finalize_payment_once("p1", 1, "creative_brain", "channel_access", "creative_30", 30)
        self.assertIsNone(await db.get_subscription(1))
        self.assertIsNone((await db.get_payment("p1"))["finalized_at"])
        self.assertEqual(await db.get_user_path(1), [])

    async def test_migration_preserves_a_copy_of_all_existing_tables(self):
        await db.close_db()
        original = Path(self.temp.name) / "original.db"
        schema = Path(__file__).with_name("fixtures").joinpath("previous_schema.sql").read_text(encoding="utf-8")
        with closing(sqlite3.connect(original)) as con, con:
            con.executescript(schema)
            con.execute("INSERT INTO users(tg_user_id, username, accepted_offer_at, created_at, updated_at) VALUES(1,'kept','2025-01-01','2025-01-01','2025-01-01')")
            con.execute("INSERT INTO payments(tg_user_id,payment_id,plan_id,amount_rub,created_at) VALUES(1,'old','m1',500,'2025-01-01')")
            con.execute("INSERT INTO subscriptions(tg_user_id,access_until,updated_at) VALUES(1,'2030-01-01','2025-01-01')")
            con.execute("INSERT INTO invite_links(tg_user_id,invite_link,created_at) VALUES(1,'preserved-link','2025-01-01')")
            con.execute("INSERT INTO settings VALUES('intro_video_file_id','preserved-file-id','2025-01-01')")
            con.execute("INSERT INTO review_photos(file_id,created_at) VALUES('preserved-photo','2025-01-01')")
            con.execute("INSERT INTO admin_states VALUES(1,'intro_video','{}','2025-01-01')")
            con.execute("CREATE TABLE unrelated(value TEXT)")
            con.execute("INSERT INTO unrelated VALUES('keep')")
            con.commit()
            snapshots = {}
            for table in [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name != 'sqlite_sequence'")]:
                columns = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
                snapshots[table] = (columns, con.execute(f"SELECT * FROM {table}").fetchall())
            db.DB_PATH = Path(self.temp.name) / "migration-copy.db"
            with closing(sqlite3.connect(db.DB_PATH)) as dest:
                con.backup(dest)
        for _ in range(2):
            await db.init_db()
            await db.close_db()
        with closing(sqlite3.connect(db.DB_PATH)) as con:
            for table, (columns, rows) in snapshots.items():
                self.assertEqual(con.execute(f"SELECT {','.join(columns)} FROM {table}").fetchall(), rows)
            self.assertEqual(con.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertIn("first_source", [r[1] for r in con.execute("PRAGMA table_info(users)")])

    async def test_incompatible_schema_is_rejected_without_erasing_data(self):
        await db.close_db()
        db.DB_PATH = Path(self.temp.name) / "incompatible.db"
        with closing(sqlite3.connect(db.DB_PATH)) as con, con:
            con.execute("CREATE TABLE users(legacy_id INTEGER, value TEXT)")
            con.execute("INSERT INTO users VALUES(7,'keep me')")
        with self.assertRaisesRegex(RuntimeError, "database preserved"):
            await db.init_db()
        with closing(sqlite3.connect(db.DB_PATH)) as con:
            self.assertEqual(con.execute("SELECT * FROM users").fetchall(), [(7, "keep me")])


    async def buy(self, auto=False, payment=None):
        if auto:
            await db.set_setting("auto_renew_enabled","1")
        self.http.post.return_value = self.response(payment or self.payment())
        await self.press("user:buy")
        if auto or await bot.auto_available():
            await self.press("user:accept_offer_auto" if auto else "user:accept_offer_and_pay")

    async def enable_auto(self):
        paid = self.payment(status="succeeded")
        paid["payment_method"] = {"saved": True, "id": "saved-test-method"}
        await self.buy(auto=True, payment=paid)
        self.assertEqual((await db.get_renewal(1))["enabled"], 1)
        self.http.post.reset_mock()
        return paid

    async def make_renewal_due(self, warned=True):
        until = (db.utcnow() + timedelta(hours=20)).isoformat()
        await self.sql("UPDATE subscriptions SET access_until=?,renewal_notice_sent_at=NULL WHERE tg_user_id=1", (until,))
        if warned:
            await self.sql("UPDATE renewal_contracts SET warned_until=?,warned_at=? WHERE tg_user_id=1",
                           (until, (db.utcnow()-timedelta(hours=13)).isoformat()))
        return until

    async def test_background_confirmation_sends_entry_without_button_and_only_once(self):
        await self.store_payment()
        self.http.get.return_value = self.response(self.payment(status="succeeded"))
        await bot.payment_reconciliation_job(self.ctx)
        until = (await db.get_subscription(1))["access_until"]
        self.assertIn("Оплата подтверждена", self.last()["text"])
        self.assertTrue(self.buttons()[0].url.startswith("https://t.me/"))
        self.assertIsNotNone((await db.get_payment("p1"))["access_notified_at"])
        calls = self.telegram.send_message.await_count
        await bot.payment_reconciliation_job(self.ctx)
        await db.close_db()
        await db.init_db()
        await bot.payment_reconciliation_job(self.ctx)
        self.assertEqual(self.telegram.send_message.await_count, calls)
        self.assertEqual((await db.get_subscription(1))["access_until"], until)
        self.assertEqual((await db.stats())["revenue_rub"], 400)

    async def test_default_checkout_uses_sbp_in_existing_one_time_yookassa_flow(self):
        await bot.init_storage(NS())
        self.assertFalse(await bot.auto_available())
        await self.press("user:buy")
        self.assertFalse(any(b.callback_data=="user:accept_offer_auto" for b in self.buttons()))
        self.assertEqual(self.buttons()[1].url,"https://example.com/pay")
        self.assertIsNone(self.buttons()[1].callback_data)
        self.assertFalse(await db.has_accepted_offer(1))
        request=self.http.post.call_args_list[0]
        self.assertEqual(request.args[0],"https://api.yookassa.ru/v3/payments")
        payload=request.kwargs["json"]
        self.assertEqual(set(payload),{"amount","capture","description","confirmation","metadata","payment_method_data"})
        self.assertEqual(payload['payment_method_data'],{'type':'sbp'})
        self.assertEqual(payload["amount"],{"value":"400.00","currency":"RUB"})
        self.assertIs(payload["capture"],True)
        self.assertEqual(payload["confirmation"]["type"],"redirect")
        self.assertIsNone(await db.get_renewal(1))

    async def test_direct_offer_repeated_click_and_restart_reuse_invoice_without_consent(self):
        await asyncio.gather(self.press("user:buy"),self.press("user:buy"))
        self.assertEqual(self.http.post.await_count,2)
        self.assertFalse(await db.has_accepted_offer(1))
        self.assertIsNone(await db.get_subscription(1))
        self.assertEqual([b.text for b in self.buttons()],["📄 Условия","Через СБП","Другие способы","◀️ Назад"])
        order=await db.get_checkout_by_payment("p1")
        self.assertEqual(order["offer_url"],OFFER)
        self.assertIsNotNone(order["offer_presented_at"])
        await db.close_db()
        await db.init_db()
        self.ctx.user_data={}
        await self.press("user:buy")
        self.assertEqual(self.http.post.await_count,2)
        self.assertEqual(len(await self.sql("SELECT * FROM checkout_requests")),2)
        self.assertFalse(any(r["event_name"]=="offer_accepted" for r in await db.get_user_path(1)))
        self.assertEqual(self.buttons()[1].url,"https://example.com/pay")

    async def test_direct_payment_keeps_presented_offer_and_records_acceptance_only_when_paid(self):
        await self.press("user:buy")
        await db.set_offer_url(OFFER+"/changed")
        with patch.object(bot,"OFFER_VERSION","2"):
            await self.press("user:buy")
            self.assertEqual(self.buttons()[0].url,OFFER)
            self.assertEqual(self.http.post.await_count,2)
            self.assertFalse(await db.has_accepted_offer(1))
            self.http.get.return_value=self.response(self.payment(status="succeeded"))
            await bot.payment_reconciliation_job(self.ctx)
        user=dict((await self.sql("SELECT * FROM users WHERE tg_user_id=1"))[0])
        self.assertEqual(user["accepted_offer_url"],OFFER)
        self.assertEqual(user["accepted_offer_version"],"1")
        events=[r for r in await db.get_user_path(1) if r["event_name"]=="offer_accepted"]
        self.assertEqual(len(events),1)
        self.assertEqual(json.loads(events[0]["payload_json"])["source"],"payment_after_offer")
        until=(await db.get_subscription(1))["access_until"]
        await bot.payment_reconciliation_job(self.ctx)
        self.assertEqual((await db.get_subscription(1))["access_until"],until)

    async def test_viewing_direct_offer_preserves_previous_renewal_until_manual_purchase(self):
        await self.enable_auto()
        await db.set_setting("auto_renew_enabled","0")
        self.http.post.return_value=self.response(self.payment("manual"))
        await self.press("user:buy")
        self.assertEqual((await db.get_renewal(1))["enabled"],1)
        self.assertNotIn("save_payment_method",self.http.post.call_args.kwargs["json"])
        self.http.get.return_value=self.response(self.payment("manual","succeeded"))
        await bot.payment_reconciliation_job(self.ctx)
        self.assertEqual((await db.get_renewal(1))["enabled"],0)
        self.assertEqual((await db.get_renewal(1))["disabled_reason"],"manual_payment")

    async def test_updated_default_copy_preserves_custom_admin_text(self):
        for key,previous in texts.PREVIOUS_DEFAULTS.items():
            await db.set_setting("text:"+key,previous)
            self.assertEqual(await bot.content(key),getattr(texts,key))
        custom="Мой авторский текст урока"
        await db.set_setting("text:LEAD_CAPTION",custom)
        self.assertEqual(await bot.content("LEAD_CAPTION"),custom)

    async def test_delivery_failure_retries_separately_without_blocking_new_payments(self):
        await self.store_payment(status="succeeded")
        self.telegram.create_chat_invite_link.side_effect = Forbidden("rights")
        await bot.payment_reconciliation_job(self.ctx)
        until = (await db.get_subscription(1))["access_until"]
        self.assertIsNone((await db.get_payment("p1"))["access_notified_at"])
        self.assertEqual(await db.access_notifications(), [])
        await self.sql("UPDATE payments SET delivery_attempt_at=?", ((db.utcnow()-timedelta(minutes=6)).isoformat(),))
        self.telegram.create_chat_invite_link.side_effect = None
        await bot.payment_reconciliation_job(self.ctx)
        self.assertIsNotNone((await db.get_payment("p1"))["access_notified_at"])
        self.assertEqual((await db.get_subscription(1))["access_until"], until)
        self.http.get.assert_not_awaited()

    async def test_unknown_creation_recovers_same_key_and_body_after_restart(self):
        self.http.post.side_effect = httpx.ReadTimeout("unknown")
        await self.buy()
        first = self.http.post.call_args.kwargs
        order = await db.pending_checkout(1)
        self.assertEqual(order["id"], first["headers"]["Idempotence-Key"])
        self.assertIsNone(order["payment_id"])
        await db.close_db()
        await db.init_db()
        self.http.post.side_effect = self.provider_post
        self.http.post.return_value = self.response(self.payment())
        self.http.get.return_value = self.response(self.payment())
        await bot.payment_reconciliation_job(self.ctx)
        second = self.http.post.call_args_list[1].kwargs
        self.assertEqual(first["headers"], second["headers"])
        self.assertEqual(first["json"], second["json"])
        self.assertEqual(len(await self.sql("SELECT * FROM checkout_requests")), 2)
        self.assertEqual(len(await self.sql("SELECT * FROM payments")), 2)
        self.assertTrue(any(b.url == "https://example.com/pay" for b in self.buttons()))

    async def test_old_unknown_request_never_reissued_outside_idempotence_window(self):
        order = await db.prepare_checkout(1, bot.CURRENT_TARIFF, bot.payment_payload(1, bot.CURRENT_TARIFF))
        await self.sql("UPDATE checkout_requests SET created_at=?", ((db.utcnow()-timedelta(hours=24)).isoformat(),))
        with self.assertRaises(billing.PaymentUncertain):
            await billing.submit_checkout(await db.get_checkout(order["id"]), "shop", "key")
        self.http.post.assert_not_awaited()
        self.assertIsNotNone((await db.get_checkout(order["id"]))["checked_at"])
        await self.buy()
        self.http.post.assert_not_awaited()
        self.assertEqual(len(await self.sql("SELECT * FROM checkout_requests")), 1)

    async def test_legacy_multiple_pending_invoices_block_new_checkout(self):
        await self.store_payment("old")
        await self.store_payment("recent")
        self.http.get.side_effect = [self.response(self.payment("old", "canceled")),
                                     self.response(self.payment("recent", "pending"))]
        await self.buy()
        self.http.post.assert_not_awaited()
        self.assertEqual(len(await self.sql("SELECT * FROM payments")), 2)

    async def test_quote_is_immutable_after_admin_changes_price_and_duration(self):
        await self.buy()
        await db.save_tariff("creative_30", "Новый", 700, 60, True, 99)
        self.http.get.return_value = self.response(self.payment(status="succeeded"))
        await bot.payment_reconciliation_job(self.ctx)
        self.assertAlmostEqual((db.parse_dt((await db.get_subscription(1))["access_until"])-db.utcnow()).total_seconds(),
                               30*86400, delta=5)
        self.assertEqual((await db.stats())["revenue_rub"], 400)

    async def test_published_multi_tariff_button_flow_and_hidden_tariffs(self):
        await db.save_tariff("quarter","Три месяца",990,90,True,99)
        await self.press("user:product")
        self.assertEqual(self.last()["text"],texts.PRODUCT)
        self.assertEqual(len(self.buttons()),3)
        self.assertEqual(next(b.text for b in self.buttons() if b.callback_data=="user:choose:quarter"),"Три месяца · 990 ₽")
        self.http.post.assert_not_awaited()
        self.ctx.user_data['selected_tariff']='creative_30'
        await self.press("user:tariffs")
        self.http.post.assert_not_awaited()
        self.assertTrue(any(b.callback_data=="user:choose:quarter" for b in self.buttons()))
        plan=bot.Tariff("quarter","Три месяца",990,90)
        self.http.post.return_value=self.response(self.payment(plan=plan))
        await self.press("user:choose:quarter")
        self.assertIn("990",self.last()["text"])
        self.assertIn("90",self.last()["text"])
        self.assertEqual(self.buttons()[1].url,"https://example.com/pay")
        self.assertEqual(self.http.post.call_args.kwargs["json"]["amount"]["value"],"990.00")
        self.http.get.return_value=self.response(self.payment(status="succeeded",plan=plan))
        await bot.payment_reconciliation_job(self.ctx)
        self.assertAlmostEqual((db.parse_dt((await db.get_subscription(1))["access_until"])-db.utcnow()).total_seconds(),90*86400,delta=5)
        await self.press("user:activity")
        self.assertIn("не сохраняется",self.last()["text"])
        for tariff in await db.list_tariffs():
            await db.save_tariff(tariff["code"],tariff["title"],tariff["amount_rub"],tariff["days"],False,99)
        await self.press("user:tariffs")
        self.assertIn("приостановлены",self.last()["text"])
        self.assertEqual([b.callback_data for b in self.buttons()],['user:product'])

    async def test_expired_invite_does_not_block_recovery_when_provider_cannot_revoke_it(self):
        await self.store_payment(status="succeeded")
        await billing.finalize(await db.get_payment("p1"))
        await db.save_invite_link(1,"https://t.me/+expired","p1",(db.utcnow()-timedelta(hours=1)).isoformat())
        self.telegram.revoke_chat_invite_link.side_effect=BadRequest("invite not found")
        await self.press("user:check_payment:p1")
        self.telegram.revoke_chat_invite_link.assert_not_awaited()
        self.telegram.create_chat_invite_link.assert_awaited_once()
        self.assertEqual(self.buttons()[0].url,"https://t.me/+test-invite")

    async def test_stale_tariff_price_requires_showing_new_terms(self):
        await db.set_setting("auto_renew_enabled","1")
        await self.press("user:buy")
        await db.save_tariff("creative_30", "Новый", 500, 60, True, 99)
        await self.press("user:accept_offer_auto")
        self.http.post.assert_not_awaited()
        self.assertIn("500", self.last()["text"])
        self.assertIn("60", self.last()["text"])

    async def test_auto_requires_explicit_consent_and_saved_method(self):
        await self.buy(auto=True)
        self.assertIs(self.http.post.call_args.kwargs["json"]["save_payment_method"], True)
        self.assertEqual((await db.get_renewal(1))["enabled"], 0)
        self.http.get.return_value = self.response(self.payment(status="succeeded"))
        await bot.payment_reconciliation_job(self.ctx)
        self.assertEqual((await db.get_renewal(1))["enabled"], 0)
        self.assertEqual((await db.get_renewal(1))["disabled_reason"], "method_not_saved")
        self.assertIn("не включено", self.last()["text"])

    async def test_manual_payment_never_enables_recurring_even_if_method_saved(self):
        paid = self.payment(status="succeeded")
        paid["payment_method"] = {"saved": True, "id": "saved"}
        await self.buy(payment=paid)
        self.assertNotIn("save_payment_method", self.http.post.call_args.kwargs["json"])
        self.assertIsNone(await db.get_renewal(1))

    async def test_cancel_during_initial_payment_cannot_be_undone_by_late_success(self):
        await self.buy(auto=True)
        await bot.cancel_subscription(self.update, self.ctx)
        paid = self.payment(status="succeeded")
        paid["payment_method"] = {"saved": True, "id": "saved"}
        self.http.get.return_value = self.response(paid)
        await bot.payment_reconciliation_job(self.ctx)
        self.assertTrue(bot.active_subscription_until(await db.get_subscription(1)))
        self.assertEqual((await db.get_renewal(1))["enabled"], 0)
        self.assertIsNotNone((await db.get_renewal(1))["disabled_at"])

    async def test_recurring_cycle_is_one_charge_and_one_extension(self):
        await self.enable_auto()
        until = await self.make_renewal_due()
        self.http.post.return_value = self.response(self.payment("renewal", "succeeded"))
        await asyncio.gather(bot.automatic_renewals_job(self.ctx), bot.automatic_renewals_job(self.ctx))
        self.assertEqual(self.http.post.await_count, 1)
        payload = self.http.post.call_args.kwargs["json"]
        self.assertEqual(payload["payment_method_id"], "saved-test-method")
        self.assertNotIn("confirmation", payload)
        self.assertNotIn("payment_method_data", payload)
        self.assertEqual(db.parse_dt((await db.get_subscription(1))["access_until"]), db.parse_dt(until)+timedelta(days=30))
        self.assertEqual(len(await self.sql("SELECT * FROM checkout_requests WHERE cycle_until IS NOT NULL")), 1)
        await bot.payment_reconciliation_job(self.ctx)
        self.assertEqual((await db.stats())["revenue_rub"], 800)
        self.assertIn("Оплата подтверждена", self.last()["text"])

    async def test_recurring_decline_disables_charge_and_sends_manual_button_once(self):
        await self.enable_auto()
        until = await self.make_renewal_due()
        self.http.post.return_value = self.response(self.payment("declined", "canceled"))
        await bot.automatic_renewals_job(self.ctx)
        calls = self.telegram.send_message.await_count
        await bot.automatic_renewals_job(self.ctx)
        self.assertEqual(self.http.post.await_count, 1)
        self.assertEqual(self.telegram.send_message.await_count, calls)
        self.assertEqual((await db.get_renewal(1))["enabled"], 0)
        self.assertEqual((await db.get_subscription(1))["access_until"], until)
        self.assertEqual(self.buttons()[0].callback_data, "user:buy")

    async def test_recurring_timeout_never_starts_manual_or_second_cycle(self):
        await self.enable_auto()
        await self.make_renewal_due()
        self.http.post.side_effect = httpx.ReadTimeout("unknown charge")
        before = self.telegram.send_message.await_count
        await bot.automatic_renewals_job(self.ctx)
        first = self.http.post.call_args.kwargs
        await bot.automatic_renewals_job(self.ctx)
        self.assertEqual(self.http.post.await_count, 1)
        self.assertEqual(self.telegram.send_message.await_count, before)
        self.http.post.side_effect = self.provider_post
        self.http.post.return_value = self.response(self.payment("recovered", "succeeded"))
        await bot.payment_reconciliation_job(self.ctx)
        self.assertEqual(first["headers"], self.http.post.call_args.kwargs["headers"])
        self.assertEqual(first["json"], self.http.post.call_args.kwargs["json"])
        self.assertEqual((await db.stats())["revenue_rub"], 800)

    async def test_recurring_change_or_hide_tariff_stops_without_charging(self):
        await self.enable_auto()
        await self.make_renewal_due()
        await db.save_tariff("creative_30", "Новый", 500, 30, True, 99)
        await bot.automatic_renewals_job(self.ctx)
        self.http.post.assert_not_awaited()
        self.assertEqual((await db.get_renewal(1))["disabled_reason"], "tariff_changed")
        self.assertIn("изменились", self.last()["text"])

    async def test_master_off_and_user_cancel_prevent_unknown_autopost_retry(self):
        await self.enable_auto()
        await self.make_renewal_due()
        self.http.post.side_effect = httpx.ReadTimeout("unknown")
        await bot.automatic_renewals_job(self.ctx)
        await db.set_admin_setting("auto_renew_enabled", "0", 99)
        await bot.payment_reconciliation_job(self.ctx)
        self.assertEqual(self.http.post.await_count, 1)
        await bot.show_active_access(1, 1, self.ctx)
        self.assertIn("приостановлено", self.last()["text"])
        await db.set_admin_setting("auto_renew_enabled", "1", 99)
        await bot.cancel_subscription(self.update, self.ctx)
        await bot.payment_reconciliation_job(self.ctx)
        self.assertEqual(self.http.post.await_count, 1)

    async def test_recurring_requires_delivered_warning_and_cancellation_window(self):
        await self.enable_auto()
        until = await self.make_renewal_due(warned=False)
        await bot.automatic_renewals_job(self.ctx)
        self.http.post.assert_not_awaited()
        await bot.renewal_notices_job(self.ctx)
        contract = await db.get_renewal(1)
        self.assertEqual(contract["warned_until"], until)
        await bot.automatic_renewals_job(self.ctx)
        self.http.post.assert_not_awaited()
        self.assertIn("400", self.last()["text"])
        self.assertIn("12 часов", self.last()["text"])

    async def test_recurring_warning_timeout_never_authorizes_charge(self):
        await self.enable_auto()
        await self.make_renewal_due(warned=False)
        self.telegram.send_message.side_effect = TimedOut()
        await bot.renewal_notices_job(self.ctx)
        await bot.automatic_renewals_job(self.ctx)
        self.assertIsNone((await db.get_renewal(1))["warned_at"])
        self.http.post.assert_not_awaited()

    async def test_manual_pending_invoice_blocks_automatic_charge(self):
        await self.enable_auto()
        await self.make_renewal_due()
        await self.store_payment("manual")
        await bot.automatic_renewals_job(self.ctx)
        self.http.post.assert_not_awaited()

    async def test_expiry_reconciles_paid_invoice_before_removing_member(self):
        await db.grant_access(1, 1)
        await self.sql("UPDATE subscriptions SET access_until=?", ((db.utcnow()-timedelta(minutes=1)).isoformat(),))
        await self.store_payment()
        self.http.get.return_value = self.response(self.payment(status="succeeded"))
        await bot.expire_subscriptions_job(self.ctx)
        self.telegram.ban_chat_member.assert_not_awaited()
        self.assertTrue(bot.active_subscription_until(await db.get_subscription(1)))

    async def test_join_request_checks_owner_subscription_and_reuse_before_approval(self):
        await db.grant_access(1, 30)
        await db.save_invite_link(1, "https://t.me/+owned", expire_at=(db.utcnow()+timedelta(hours=1)).isoformat())
        def request(uid):
            return NS(chat_join_request=NS(chat=NS(id=bot.CHANNEL_ID),from_user=NS(id=uid),
                                          invite_link=NS(invite_link="https://t.me/+owned")))
        await bot.channel_join_request(request(2), self.ctx)
        self.telegram.approve_chat_join_request.assert_not_awaited()
        self.telegram.decline_chat_join_request.assert_awaited_once_with(bot.CHANNEL_ID, 2)
        await bot.channel_join_request(request(1), self.ctx)
        self.telegram.approve_chat_join_request.assert_awaited_once_with(bot.CHANNEL_ID, 1)
        await bot.channel_join_request(request(1), self.ctx)
        self.assertEqual(self.telegram.approve_chat_join_request.await_count, 1)
        self.assertEqual((await db.get_invite_link("https://t.me/+owned"))["used"], 1)

    async def test_revoked_and_expired_subscriptions_cannot_join(self):
        await db.grant_access(1, 1)
        await db.save_invite_link(1, "https://t.me/+owned", expire_at=(db.utcnow()+timedelta(hours=1)).isoformat())
        update = NS(chat_join_request=NS(chat=NS(id=bot.CHANNEL_ID),from_user=NS(id=1),
                    invite_link=NS(invite_link="https://t.me/+owned")))
        await db.revoke_access(1)
        await bot.channel_join_request(update, self.ctx)
        self.telegram.approve_chat_join_request.assert_not_awaited()
        self.telegram.decline_chat_join_request.assert_awaited_once()

    async def test_activity_deduplicates_messages_ignores_edits_bots_and_wrong_groups(self):
        await db.grant_access(1, 30)
        await db.set_admin_setting("activity_chat_id", -1009, 99)
        message = NS(from_user=NS(id=1,is_bot=False),sender_chat=None,chat=NS(id=-1009,type="supergroup"),
                     text="private content",document=None,message_id=5,date=db.utcnow())
        for _ in range(2):
            await bot.group_activity(NS(message=message), self.ctx)
        message.message_id = 6
        message.document = NS(file_id="not-stored")
        await bot.group_activity(NS(message=message), self.ctx)
        await bot.group_activity(NS(message=None,edited_message=message), self.ctx)
        message.message_id = 7
        message.from_user.is_bot = True
        await bot.group_activity(NS(message=message), self.ctx)
        message.from_user.is_bot = False
        message.chat.id = -2000
        await bot.group_activity(NS(message=message), self.ctx)
        stats = await db.activity_summary(1)
        self.assertEqual((stats["messages"],stats["files"]), (2,1))
        stored = [dict(r) for r in await self.sql("SELECT * FROM activity_events")]
        self.assertNotIn("private content", str(stored))
        self.assertNotIn("not-stored", str(stored))
        self.assertNotIn("0", bot.activity_phrase({"messages":0,"files":0}))

    async def test_admin_tariffs_settings_and_text_validation_survive_restart(self):
        self.user.id = self.chat.id = 99
        await self.press("admin:tariff_new")
        self.update.effective_message.text = "Три месяца\n900\n90"
        await bot.handle_admin_input(self.update, self.ctx)
        plans = await db.list_tariffs(True)
        added = next(p for p in plans if p["code"]!="creative_30")
        self.assertEqual(added["enabled"], 0)
        await self.press("user:product",user=NS(id=1,username="test",first_name="Test",last_name=None))
        self.assertEqual([b.text for b in self.buttons()],["Я хочу","◀️ Назад"])
        await self.press("admin:tariff_toggle:"+added["code"])
        await self.press("user:product",user=NS(id=1,username="test",first_name="Test",last_name=None))
        self.assertEqual(len(self.buttons()),3)
        self.assertEqual(next(b.text for b in self.buttons() if b.callback_data=="user:choose:"+added["code"]),"Три месяца · 900 ₽")
        await self.press("admin:setting:reminder_days")
        self.update.effective_message.text = "1"
        await bot.handle_admin_input(self.update, self.ctx)
        self.assertIsNone(await db.get_setting("reminder_days"))
        self.update.effective_message.text = "5"
        await bot.handle_admin_input(self.update, self.ctx)
        await self.press("admin:text:RENEWAL_NOTICE")
        self.update.effective_message.text = "До {date.__class__} {activity}"
        await bot.handle_admin_input(self.update, self.ctx)
        self.assertIsNone(await db.get_setting("text:RENEWAL_NOTICE"))
        self.update.effective_message.text = "До {date}.\n{activity}\nПродолжим?"
        await bot.handle_admin_input(self.update, self.ctx)
        await db.close_db()
        await db.init_db()
        self.assertEqual(await db.get_setting("reminder_days"), "5")
        self.assertEqual(len(await db.list_tariffs()), 2)
        self.assertEqual(await bot.content("RENEWAL_NOTICE",date="завтра",activity="Спасибо"), "До завтра.\nСпасибо\nПродолжим?")
        self.assertGreaterEqual(len(await self.sql("SELECT * FROM admin_audit")), 4)
        await self.press("admin:tariff_edit:"+added["code"]+":amount_rub")
        self.update.effective_message.text = "1100"
        await bot.handle_admin_input(self.update,self.ctx)
        await self.press("user:product",user=NS(id=1,username="test",first_name="Test",last_name=None))
        self.assertEqual(next(b.text for b in self.buttons() if b.callback_data=="user:choose:"+added["code"]),"Три месяца · 1100 ₽")
        await self.press("admin:tariff_toggle:"+added["code"])
        await self.press("user:product",user=NS(id=1,username="test",first_name="Test",last_name=None))
        self.assertEqual([b.text for b in self.buttons()],["Я хочу","◀️ Назад"])
        self.http.post.assert_not_awaited()

    async def test_nonadmin_cannot_change_extended_settings(self):
        for route in ("admin:tariff_new","admin:tariff_toggle:creative_30","admin:text_reset:WELCOME",
                      "admin:toggle:auto_renew_enabled","admin:setting:reminder_days","admin:diagnostics"):
            await self.press(route)
        self.telegram.send_message.assert_not_awaited()
        self.assertEqual(len(await self.sql("SELECT * FROM admin_audit")), 0)
        self.assertEqual(len(await db.list_tariffs()), 1)

    async def test_removed_goal_buttons_return_to_product_and_marketing_optout_works(self):
        await self.press("user:product")
        self.assertEqual(self.buttons()[0].callback_data, "user:buy")
        self.assertEqual(len(self.buttons()), 2)
        await self.press("user:goal:work")
        self.assertEqual(self.last()["text"], texts.PRODUCT)
        self.assertEqual(self.buttons()[0].callback_data, "user:buy")
        self.assertFalse(any(r["event_name"]=="goal_selected" for r in await db.get_user_path(1)))
        await self.age_lead()
        await self.press("user:quiet")
        before = self.telegram.send_message.await_count
        await bot.followup_job(self.ctx)
        self.assertEqual(self.telegram.send_message.await_count, before)

    async def test_funnel_cohorts_use_first_start_and_unique_furthest_stage(self):
        await db.record_start(1, "test")
        await db.record_start(1, "repeat")
        await db.record_event(1, "product_opened")
        await db.record_event(1, "product_opened")
        await db.upsert_user(2)
        await db.record_start(2, "old")
        await self.sql("UPDATE funnel_events SET created_at=? WHERE tg_user_id=2",
                       ((db.utcnow()-timedelta(days=60)).isoformat(),))
        await db.record_start(2, "again")
        await db.record_event(2, "payment_succeeded")
        report = await db.funnel_breakdown(30)
        self.assertEqual(report["cohort"], 1)
        self.assertEqual(report["reached"]["product_opened"], 1)
        self.assertEqual(report["stopped"]["product_opened"], 1)
        self.assertEqual(report["reached"]["payment_succeeded"], 0)

    async def test_failed_screen_replacement_preserves_previous_message(self):
        await bot.render_screen(1, 1, self.ctx,text="old")
        current = await db.get_current_message(1)
        self.telegram.edit_message_text.side_effect = BadRequest("cannot edit")
        self.telegram.send_message.side_effect = TimedOut()
        with self.assertRaises(TimedOut):
            await bot.render_screen(1, 1, self.ctx,text="new")
        self.telegram.delete_message.assert_not_awaited()
        self.assertEqual(await db.get_current_message(1), current)

    async def test_declined_cycle_recovers_notification_after_process_stops(self):
        await self.enable_auto()
        until=await self.make_renewal_due()
        order=await db.prepare_checkout(1,bot.CURRENT_TARIFF,
                  bot.payment_payload(1,bot.CURRENT_TARIFF,method_id="saved-test-method"),cycle_until=until)
        self.http.post.return_value=self.response(self.payment("declined","canceled"))
        await billing.submit_checkout(order,"shop","key")
        self.assertEqual((await db.get_renewal(1))["enabled"],1)
        self.http.post.reset_mock()
        await bot.automatic_renewals_job(self.ctx)
        self.http.post.assert_not_awaited()
        self.assertEqual((await db.get_renewal(1))["enabled"],0)
        self.assertEqual(self.buttons()[0].callback_data,"user:buy")

    async def test_definitive_provider_rejection_allows_manual_checkout(self):
        request=httpx.Request("POST",billing.API_URL)
        response=httpx.Response(400,request=request)
        self.http.post.side_effect=httpx.HTTPStatusError("rejected",request=request,response=response)
        await self.buy(auto=True)
        self.assertIsNone(await db.pending_checkout(1))
        self.assertEqual((await db.get_renewal(1))["enabled"],0)
        self.assertEqual(self.buttons()[0].callback_data,"user:accept_offer_and_pay")
        self.http.post.side_effect=self.provider_post
        self.http.post.return_value=self.response(self.payment())
        await self.press("user:accept_offer_and_pay")
        self.assertNotIn("save_payment_method",self.http.post.call_args.kwargs["json"])
        self.assertIsNotNone(await db.get_payment("p1"))

    async def test_historical_paid_records_do_not_trigger_new_entry_notifications(self):
        await db.close_db()
        db.DB_PATH=Path(self.temp.name)/"old-history.db"
        schema=Path(__file__).with_name("fixtures").joinpath("previous_schema.sql").read_text(encoding="utf-8")
        with closing(sqlite3.connect(db.DB_PATH)) as con, con:
            con.executescript(schema)
            con.execute("INSERT INTO payments(tg_user_id,payment_id,plan_id,amount_rub,status,finalized_at,created_at) VALUES(1,'historical','m1',500,'succeeded','2025-01-01','2025-01-01')")
        await db.init_db()
        self.assertEqual(await db.access_notifications(),[])
        self.assertEqual((await db.get_payment("historical"))["access_notified_at"],"2025-01-01")

    async def test_onboarding_link_after_approved_entry_and_support_terms_navigation(self):
        await db.grant_access(1,30)
        await db.save_invite_link(1,"https://t.me/+owned",expire_at=(db.utcnow()+timedelta(hours=1)).isoformat())
        await db.set_admin_setting("onboarding_url","https://t.me/c/123/1",99)
        update=NS(chat_join_request=NS(chat=NS(id=bot.CHANNEL_ID),from_user=NS(id=1),
                                      invite_link=NS(invite_link="https://t.me/+owned")))
        await bot.channel_join_request(update,self.ctx)
        self.assertEqual(self.buttons()[0].url,"https://t.me/c/123/1")
        await bot.support(self.update,self.ctx)
        self.assertTrue(any(b.callback_data=="user:auto_off" for b in self.buttons()))
        await db.set_admin_setting("support_url","https://t.me/help_example",99)
        await bot.support(self.update,self.ctx)
        self.assertEqual(self.buttons()[0].url,"https://t.me/help_example")
        await bot.terms(self.update,self.ctx)
        self.assertEqual(self.buttons()[0].url,OFFER)
        self.http.post.assert_not_awaited()

    async def test_admin_tariff_edits_and_diagnostics_buttons(self):
        self.user.id=self.chat.id=99
        for field,valid,invalid in (("amount_rub","650","-1"),("days","45","0"),("title","Название","x"*71)):
            await self.press("admin:tariff_edit:creative_30:"+field)
            self.update.effective_message.text=invalid
            await bot.handle_admin_input(self.update,self.ctx)
            self.assertIsNotNone(await db.get_admin_state(99))
            self.update.effective_message.text=valid
            await bot.handle_admin_input(self.update,self.ctx)
            self.assertIsNone(await db.get_admin_state(99))
        tariff=(await db.list_tariffs())[0]
        self.assertEqual((tariff["title"],tariff["amount_rub"],tariff["days"]),("Название",650,45))
        self.telegram.get_me=AsyncMock(return_value=NS(id=500))
        self.telegram.get_chat_member=AsyncMock(return_value=NS(status="administrator",can_invite_users=True,can_restrict_members=True))
        self.telegram.get_chat=AsyncMock(return_value=NS(title="Учебная группа"))
        await db.set_admin_setting("activity_chat_id",-100999,99)
        await self.press("admin:diagnostics")
        self.assertIn("Приглашения: да",self.last()["text"])
        self.assertIn("Учебная группа",self.last()["text"])
        self.http.post.assert_not_awaited()
        self.http.get.assert_not_awaited()
        for route in ("admin:settings","admin:toggle:followup_enabled","admin:toggle:auto_renew_enabled",
                      "admin:stats:7","admin:stats:0","admin:texts","admin:text_reset:WELCOME","admin:tariffs"):
            await self.press(route)
            self.assertTrue(self.buttons())

    async def test_edited_long_unicode_lead_and_url_stay_inside_telegram_limit(self):
        self.user.id=self.chat.id=99
        await self.press("admin:text:LEAD_CAPTION")
        self.update.effective_message.text="🧠"*1001
        await bot.handle_admin_input(self.update,self.ctx)
        self.assertIsNone(await db.get_setting("text:LEAD_CAPTION"))
        self.update.effective_message.text="🧠"*1000
        await bot.handle_admin_input(self.update,self.ctx)
        url="https://example.com/"+"a"*2020
        await db.set_lead_magnet_url(url)
        await bot.send_lead_magnet(99,99,self.ctx,welcome=False,track=False)
        self.assertLessEqual(bot.telegram_length(self.last()["text"]),4096)
        self.assertIn(url,self.last()["text"])


    async def assert_legacy_followup_target(self, action):
        await bot.render_screen(1,1,self.ctx,text='Предыдущее меню')
        old_id=self.last_active_id
        notice=await self.telegram.send_message(1,texts.FOLLOWUP)
        sent=self.telegram.send_message.await_count
        await self.press(action,message_id=notice.message_id)
        self.assertEqual(self.telegram.edit_message_text.call_args.kwargs['message_id'],notice.message_id)
        self.assertEqual((await db.get_current_message(1))['current_message_id'],notice.message_id)
        self.assertEqual(self.telegram.send_message.await_count,sent)
        self.assertIn(old_id,self.deleted)

    async def test_legacy_followup_program_button_edits_the_pressed_message(self):
        await self.assert_legacy_followup_target('user:product')
        self.assertEqual(self.last()['text'],texts.PRODUCT)

    async def test_legacy_followup_quiet_button_edits_the_pressed_message(self):
        await self.assert_legacy_followup_target('user:quiet')
        self.assertIn('отключены',self.last()['text'])
        self.assertEqual((await self.sql('SELECT marketing_opt_out FROM users WHERE tg_user_id=1'))[0][0],1)

    async def test_followup_becomes_the_menu_and_preserves_the_video(self):
        await bot.start(self.update,self.ctx)
        await self.press('user:open_lesson')
        lesson=await db.get_lesson_message(1)
        snapshot=dict(self.messages[lesson['lesson_message_id']])
        await self.age_lead()
        await bot.followup_job(self.ctx)
        notice_id=self.last_active_id
        self.assertEqual((await db.get_current_message(1))['current_message_id'],notice_id)
        sent=self.telegram.send_message.await_count
        await self.press('user:product',message_id=notice_id)
        self.assertEqual(self.telegram.edit_message_text.call_args.kwargs['message_id'],notice_id)
        self.assertEqual(self.telegram.send_message.await_count,sent)
        self.assertEqual(self.messages[lesson['lesson_message_id']],snapshot)
        self.assertNotIn(lesson['lesson_message_id'],self.deleted)
        self.assertEqual(set(self.messages)-self.deleted,{lesson['lesson_message_id'],notice_id})

    async def test_forward_back_reuses_menu_and_restart_refreshes_without_clutter(self):
        await bot.start(self.update,self.ctx)
        await self.press('user:open_lesson')
        lesson=await db.get_lesson_message(1)
        lesson_id=lesson['lesson_message_id']
        snapshot=dict(self.messages[lesson_id])
        await self.press('user:product',message_id=lesson_id)
        menu_id=self.last_active_id
        self.assertGreater(menu_id,lesson_id)
        for _ in range(3):
            await self.press('user:lesson')
            self.assertIn(URL,self.last()['text'])
            self.assertEqual(self.last()['reply_markup'].inline_keyboard[-1][0].callback_data,'user:welcome')
            await self.press('user:welcome')
            self.assertEqual(self.last()['text'],texts.WELCOME)
            await self.press('user:lead_magnet')
            await self.press('user:product')
            await self.press('user:buy')
            await self.press('user:product')
        self.assertEqual(self.http.post.await_count,2)
        self.assertEqual(self.messages[lesson_id],snapshot)
        self.assertNotIn(lesson_id,self.deleted)
        sent=self.telegram.send_message.await_count
        await db.close_db()
        await db.init_db()
        self.ctx.user_data={}
        await bot.start(self.update,self.ctx)
        await self.press('user:open_lesson')
        await self.press('user:product')
        new_menu=(await db.get_current_message(1))['current_message_id']
        new_lesson=(await db.get_lesson_message(1))['lesson_message_id']
        self.assertNotEqual(new_menu,menu_id)
        self.assertEqual(self.telegram.send_message.await_count,sent+3)
        self.assertEqual(self.messages[new_lesson],snapshot)
        self.assertIn(lesson_id,self.deleted)
        self.assertEqual(set(self.messages)-self.deleted,{new_lesson,new_menu})

    async def test_legacy_lesson_is_preserved_and_registered_on_first_navigation(self):
        await bot.render_screen(1,1,self.ctx,text=texts.WELCOME)
        lesson=await self.telegram.send_message(1,texts.LEAD_CAPTION+'\n\n'+URL)
        snapshot=dict(self.messages[lesson.message_id])
        await self.press('user:product',message_id=lesson.message_id)
        self.assertEqual(self.messages[lesson.message_id],snapshot)
        self.assertNotIn(lesson.message_id,self.deleted)
        self.assertGreater(self.last_active_id,lesson.message_id)
        self.assertEqual((await db.get_lesson_message(1))['lesson_message_id'],lesson.message_id)

    async def test_native_video_callback_preserves_media_even_without_a_saved_url(self):
        video=await self.telegram.send_message(1,'video fixture')
        await db.set_current_message(1,1,video.message_id,'video')
        query=NS(from_user=self.user,data='user:product',answer=AsyncMock(),
                 message=NS(chat_id=1,chat=NS(type='private'),message_id=video.message_id,
                            video=NS(file_id='video-file'),text=None,caption='Урок'))
        await bot.router(NS(callback_query=query),self.ctx)
        self.assertNotIn(video.message_id,self.deleted)
        self.assertGreater(self.last_active_id,video.message_id)

    async def test_repeated_start_refreshes_lesson_only_after_its_button(self):
        await bot.start(self.update,self.ctx)
        await self.press('user:open_lesson')
        lesson=await db.get_lesson_message(1)
        sent=self.telegram.send_message.await_count
        await bot.start(self.update,self.ctx)
        self.assertEqual(self.telegram.send_message.await_count,sent+1)
        self.assertEqual(await db.get_lesson_message(1),lesson)
        self.assertNotIn(lesson['lesson_message_id'],self.deleted)
        await self.press('user:open_lesson')
        self.assertGreater((await db.get_current_message(1))['current_message_id'],lesson['lesson_message_id'])
        visible=set(self.messages)-self.deleted
        self.assertEqual(sum(URL in self.messages[mid]['text'] for mid in visible),1)
        self.assertEqual(len(visible),2)
        self.assertIn(lesson['lesson_message_id'],self.deleted)

    async def test_click_on_an_older_live_menu_adopts_it_and_retires_the_other_menu(self):
        await bot.render_screen(1,1,self.ctx,text='Старое меню')
        old_id=self.last_active_id
        newer=await self.telegram.send_message(1,'Новое меню')
        await db.set_current_message(1,1,newer.message_id,'text')
        sent=self.telegram.send_message.await_count
        await self.press('user:product',message_id=old_id)
        self.assertEqual(self.messages[old_id]['text'],texts.PRODUCT)
        self.assertEqual((await db.get_current_message(1))['current_message_id'],old_id)
        self.assertIn(newer.message_id,self.deleted)
        self.assertEqual(self.telegram.send_message.await_count,sent)

    async def test_deleted_callback_message_is_replaced_only_once(self):
        await bot.render_screen(1,1,self.ctx,text='Старое меню')
        old_id=self.last_active_id
        await bot.render_screen(1,1,self.ctx,text='Новое меню',force_new=True)
        previous_id=self.last_active_id
        sent=self.telegram.send_message.await_count
        await self.press('user:product',message_id=old_id)
        self.assertEqual(self.telegram.send_message.await_count,sent+1)
        self.assertIn(previous_id,self.deleted)
        self.assertEqual(set(self.messages)-self.deleted,{self.last_active_id})

    async def test_transport_failure_does_not_clone_the_menu_or_leak_callback_context(self):
        await bot.render_screen(1,1,self.ctx,text='Меню')
        current=await db.get_current_message(1)
        sent=self.telegram.send_message.await_count
        self.telegram.edit_message_text.side_effect=TimedOut()
        with self.assertRaises(TimedOut):
            await self.press('user:product')
        self.assertEqual(self.telegram.send_message.await_count,sent)
        self.assertEqual(await db.get_current_message(1),current)
        self.assertIsNone(bot._callback_query.get())

    async def test_unchanged_pressed_message_still_becomes_the_saved_menu(self):
        await bot.render_screen(1,1,self.ctx,text='Прежнее меню')
        old_id=self.last_active_id
        notice=await self.telegram.send_message(1,texts.PRODUCT)
        self.telegram.edit_message_text.side_effect=BadRequest('Message is not modified')
        await self.press('user:product',message_id=notice.message_id)
        self.assertEqual((await db.get_current_message(1))['current_message_id'],notice.message_id)
        self.assertIn(old_id,self.deleted)

    async def test_undeletable_old_menu_loses_its_inline_buttons(self):
        await bot.render_screen(1,1,self.ctx,text='Прежнее меню')
        old_id=self.last_active_id
        notice=await self.telegram.send_message(1,texts.FOLLOWUP)
        self.telegram.delete_message.side_effect=Forbidden('message cannot be deleted')
        await self.press('user:product',message_id=notice.message_id)
        self.telegram.edit_message_reply_markup.assert_awaited_once_with(chat_id=1,message_id=old_id,reply_markup=None)
        self.assertEqual((await db.get_current_message(1))['current_message_id'],notice.message_id)

    async def test_missing_lesson_has_back_navigation_without_duplicate_messages(self):
        await db.clear_lead_magnet_url()
        await bot.start(self.update,self.ctx)
        menu_id=self.last_active_id
        for action in ('user:product','user:lesson','user:welcome','user:lead_magnet'):
            await self.press(action)
            self.assertTrue(any('Назад' in b.text for b in self.buttons()))
        self.assertEqual(self.telegram.send_message.await_count,1)
        self.assertEqual((await db.get_current_message(1))['current_message_id'],menu_id)

    async def test_concurrent_callbacks_keep_each_users_message_target(self):
        other=NS(id=2,username='other',first_name='Other',last_name=None)
        await db.upsert_user(2)
        await bot.render_screen(1,1,self.ctx,text='Меню 1')
        await bot.render_screen(2,2,self.ctx,text='Меню 2')
        first=await self.telegram.send_message(1,texts.FOLLOWUP)
        second=await self.telegram.send_message(2,texts.FOLLOWUP)
        await asyncio.gather(self.press('user:product',message_id=first.message_id),
                             self.press('user:quiet',user=other,message_id=second.message_id))
        self.assertEqual((await db.get_current_message(1))['current_message_id'],first.message_id)
        self.assertEqual((await db.get_current_message(2))['current_message_id'],second.message_id)
        self.assertEqual(self.messages[first.message_id]['text'],texts.PRODUCT)
        self.assertIn('отключены',self.messages[second.message_id]['text'])

    async def test_admin_menu_navigation_does_not_create_new_messages(self):
        admin_user=NS(id=99,username='admin',first_name='Admin',last_name=None)
        await self.press('admin:menu',user=admin_user)
        menu_id=self.last_active_id
        for action in ('admin:settings','admin:menu','admin:tariffs','admin:menu'):
            await self.press(action,user=admin_user)
        self.assertEqual(self.telegram.send_message.await_count,1)
        self.assertEqual((await db.get_current_message(99))['current_message_id'],menu_id)

    async def test_long_admin_statistics_are_paginated_in_one_message(self):
        await db.upsert_user(99)
        stats=await db.funnel_stats()
        stats['sources']=[{'source':'source_'+str(i)+'x'*50,'starts':1,'paid':0} for i in range(150)]
        with patch.object(db,'funnel_stats',AsyncMock(return_value=stats)):
            await bot.show_stats(99,99,self.ctx)
            menu_id=self.last_active_id
            first=self.last()['text']
            next_page=next(b.callback_data for b in self.buttons() if b.text=='Страница →')
            await self.press(next_page,user=NS(id=99,username='admin',first_name='Admin',last_name=None))
            self.assertNotEqual(self.last()['text'],first)
            self.assertEqual(self.last_active_id,menu_id)
            self.assertEqual(self.telegram.send_message.await_count,1)

    async def test_start_recovers_when_client_hides_history_but_edits_still_succeed(self):
        await bot.start(self.update,self.ctx)
        await self.press('user:open_lesson')
        await self.press('user:product')
        hidden=set(self.messages)
        await bot.start(self.update,self.ctx)
        visible=set(self.messages)-self.deleted-hidden
        self.assertEqual(len(visible),1)
        self.assertTrue(any(self.messages[mid]['text']==texts.WELCOME for mid in visible))
        self.assertFalse(any(URL in self.messages[mid]['text'] for mid in visible))
        await self.press('user:open_lesson')
        visible=set(self.messages)-self.deleted-hidden
        self.assertTrue(any(URL in self.messages[mid]['text'] for mid in visible))

    async def test_start_recovers_when_both_menu_and_lesson_are_deleted(self):
        await bot.start(self.update,self.ctx)
        await self.press('user:open_lesson')
        await self.press('user:product')
        self.deleted.update(self.messages)
        await bot.start(self.update,self.ctx)
        visible=set(self.messages)-self.deleted
        self.assertEqual(len(visible),1)
        self.assertEqual(self.last()['text'],texts.WELCOME)
        await self.press('user:open_lesson')
        visible=set(self.messages)-self.deleted
        self.assertTrue(any(URL in self.messages[mid]['text'] for mid in visible))

    async def test_seven_starts_after_clear_keep_one_greeting_until_lesson_button(self):
        await bot.start(self.update,self.ctx)
        await self.press('user:open_lesson')
        await self.press('user:product')
        hidden=set(self.messages)
        for _ in range(7):
            await bot.start(self.update,self.ctx)
            visible=set(self.messages)-self.deleted-hidden
            self.assertEqual(len(visible),1)
            self.assertEqual(sum(URL in self.messages[mid]['text'] for mid in visible),0)
            self.assertTrue(any(self.messages[mid]['text']==texts.WELCOME for mid in visible))
        await self.press('user:open_lesson')
        await self.press('user:product')
        menu_id=(await db.get_current_message(1))['current_message_id']
        sent=self.telegram.send_message.await_count
        for action in ('user:lesson','user:welcome','user:lead_magnet','user:product'):
            await self.press(action)
        self.assertEqual((await db.get_current_message(1))['current_message_id'],menu_id)
        self.assertEqual(self.telegram.send_message.await_count,sent)

    async def test_lesson_delivery_and_followup_start_only_after_welcome_button(self):
        await bot.start(self.update,self.ctx)
        self.assertEqual(self.telegram.send_message.await_count,1)
        self.assertEqual(self.last()['text'],texts.WELCOME)
        self.assertNotIn(URL,self.last()['text'])
        self.assertEqual([row['event_name'] for row in await db.get_user_path(1)],['start'])
        self.assertEqual(await db.followup_candidates(db.utcnow()+timedelta(hours=23)),[])
        button=next(b for b in self.buttons() if b.text=='▶️ Бесплатный урок')
        self.assertIsNone(button.url)
        await self.press(button.callback_data)
        self.assertEqual(self.telegram.send_message.await_count,2)
        self.assertIn(URL,self.last()['text'])
        self.assertTrue(self.last()['link_preview_options'].prefer_large_media)
        self.assertEqual([row['event_name'] for row in await db.get_user_path(1)],['start','lead_magnet_sent'])
        self.assertEqual(await db.followup_candidates(db.utcnow()+timedelta(hours=23)),[1])

    async def test_fresh_welcome_button_recovers_preview_after_server_restart(self):
        await bot.start(self.update,self.ctx)
        await self.press('user:open_lesson')
        old_lesson=await db.get_lesson_message(1)
        hidden=set(self.messages)
        await bot.start(self.update,self.ctx)
        button=next(b for b in self.buttons() if b.text=='▶️ Бесплатный урок')
        await db.close_db()
        await db.init_db()
        self.ctx.user_data={}
        await self.press(button.callback_data)
        lesson=await db.get_lesson_message(1)
        self.assertNotEqual(lesson['lesson_message_id'],old_lesson['lesson_message_id'])
        self.assertNotIn(lesson['lesson_message_id'],hidden)
        preview=self.messages[lesson['lesson_message_id']]['link_preview_options']
        self.assertEqual(preview.url,URL)
        self.assertFalse(preview.is_disabled)
        self.assertTrue(preview.prefer_large_media)
        self.assertIn(old_lesson['lesson_message_id'],self.deleted)

    async def test_failed_lesson_delivery_keeps_greeting_available_for_retry(self):
        await bot.start(self.update,self.ctx)
        greeting=self.last_active_id
        button=next(b for b in self.buttons() if b.text=='▶️ Бесплатный урок')
        send=self.telegram.send_message.side_effect
        self.telegram.send_message.side_effect=TimedOut()
        with self.assertRaises(TimedOut):
            await self.press(button.callback_data)
        self.assertNotIn(greeting,self.deleted)
        self.assertEqual((await db.get_lesson_message(1))['lesson_message_id'],0)
        self.assertEqual([row['event_name'] for row in await db.get_user_path(1)],['start'])
        self.telegram.send_message.side_effect=send
        await self.press(button.callback_data)
        self.assertIn(URL,self.last()['text'])

    async def test_start_keeps_the_command_when_first_response_cannot_be_sent(self):
        self.telegram.send_message.side_effect=TimedOut()
        with self.assertRaises(TimedOut):
            await bot.start(self.update,self.ctx)
        self.assertNotIn(self.update.effective_message.message_id,self.deleted)
        self.assertIsNone(bot._command_screen.get())

    async def test_active_start_recovers_after_client_clear_without_changing_access(self):
        await db.grant_access(1,30)
        await bot.start(self.update,self.ctx)
        subscription=await db.get_subscription(1)
        hidden=set(self.messages)
        await bot.start(self.update,self.ctx)
        visible=set(self.messages)-self.deleted-hidden
        self.assertEqual(len(visible),1)
        self.assertIn('активен до',self.messages[next(iter(visible))]['text'])
        self.assertEqual(await db.get_subscription(1),subscription)

    async def test_other_entry_commands_recover_after_client_clear(self):
        with patch.object(bot,'ADMIN_IDS',{1}):
            for command,handler in (('/help',bot.support),('/terms',bot.terms),('/admin',bot.admin)):
                self.update.effective_message.text=command
                await handler(self.update,self.ctx)
                hidden=set(self.messages)
                await handler(self.update,self.ctx)
                self.assertEqual(len(set(self.messages)-self.deleted-hidden),1)

    async def test_checkout_has_two_distinct_direct_urls_with_only_sbp_restricted(self):
        payment=self.payment()
        payment['payment_method']={'type':'sbp'}
        payment['confirmation']['confirmation_url']='https://yoomoney.ru/checkout/payments/sbp?orderId=sbp-fixture'
        self.http.post.return_value=self.response(payment)
        await self.press('user:buy')
        sbp,other=[call.kwargs for call in self.http.post.call_args_list]
        self.assertEqual(sbp['json']['payment_method_data'],{'type':'sbp'})
        self.assertNotIn('payment_method_data',other['json'])
        self.assertNotIn('payment_method_id',other['json'])
        self.assertNotIn('payment_token',other['json'])
        self.assertNotIn('save_payment_method',other['json'])
        self.assertNotEqual(sbp['headers'],other['headers'])
        for payload in (sbp['json'],other['json']):
            self.assertEqual(payload['amount'],{'value':'400.00','currency':'RUB'})
            self.assertTrue(payload['capture'])
            self.assertEqual(payload['confirmation'],{'type':'redirect','return_url':'https://example.com/return'})
        self.assertEqual(self.buttons()[1].url,payment['confirmation']['confirmation_url'])
        self.assertEqual(self.buttons()[1].text,'Через СБП')
        self.assertEqual(self.buttons()[2].text,'Другие способы')
        self.assertEqual(self.buttons()[2].url,'https://example.com/pay-all')
        self.assertIsNone(self.buttons()[2].callback_data)
        self.assertNotIn('нажмите Ещё',self.last()['text'])
        self.assertIsNone(self.last()['entities'])
        self.assertIsNone(await db.get_subscription(1))

    async def test_sbp_initial_auto_setup_requires_explicit_opt_in(self):
        await db.set_setting('auto_renew_enabled','1')
        await self.press('user:buy')
        self.http.post.assert_not_awaited()
        await self.press('user:accept_offer_auto')
        request=self.http.post.call_args.kwargs['json']
        self.assertEqual(request['payment_method_data'],{'type':'sbp'})
        self.assertTrue(request['save_payment_method'])
        self.assertNotIn('payment_method_id',request)

    async def test_legacy_smart_checkout_keeps_original_quote_and_adds_sbp_without_hint(self):
        payload=bot.payment_payload(1,bot.CURRENT_TARIFF)
        payload.pop('payment_method_data')
        order=await db.prepare_checkout(1,bot.CURRENT_TARIFF,payload,direct_offer=(OFFER,bot.OFFER_VERSION))
        await billing.submit_checkout(order,'shop','key')
        original=(await db.get_checkout(order['id']))['request_json']
        await self.press('user:buy')
        self.assertNotIn('нажмите Ещё',self.last()['text'])
        self.assertIsNone(self.last()['entities'])
        self.assertEqual(self.buttons()[1].url,'https://example.com/pay-sbp')
        self.assertEqual(self.buttons()[2].url,'https://example.com/pay')
        await db.close_db()
        await db.init_db()
        await self.press('user:buy')
        self.assertEqual((await db.get_checkout(order['id']))['request_json'],original)
        self.assertEqual(self.http.post.await_count,2)

    async def test_checkout_preserves_custom_text_and_does_not_add_sbp_hint(self):
        await self.store_payment()
        row=await db.get_payment('p1')
        original='🧠 Условия <оферты> & цена 400 ₽'
        await bot.render_checkout_screen(1,1,self.ctx,row=row,text=original)
        message=self.last()
        self.assertEqual(message['text'],original)
        self.assertIsNone(message['entities'])
        self.assertEqual(next(b.url for b in self.buttons() if b.text=='Другие способы'),'https://example.com/pay')

    async def test_legacy_payment_check_has_no_sbp_hint(self):
        await self.store_payment()
        payment=self.payment()
        payment['payment_method']={'type':'sbp'}
        self.http.get.return_value=self.response(payment)
        await self.press('user:check_payment:p1')
        self.assertNotIn('нажмите Ещё',self.last()['text'])
        self.assertIsNone(self.last()['entities'])

    async def test_unknown_legacy_request_keeps_original_method_and_idempotence_key(self):
        payload=bot.payment_payload(1,bot.CURRENT_TARIFF)
        payload.pop('payment_method_data')
        order=await db.prepare_checkout(1,bot.CURRENT_TARIFF,payload,direct_offer=(OFFER,bot.OFFER_VERSION))
        self.http.post.side_effect=httpx.ReadTimeout('unknown outcome')
        await self.press('user:buy')
        await db.close_db()
        await db.init_db()
        self.http.post.side_effect=self.provider_post
        await self.press('user:buy')
        for call in self.http.post.call_args_list[:2]:
            self.assertEqual(call.kwargs['json'],payload)
            self.assertEqual(call.kwargs['headers']['Idempotence-Key'],order['id'])
        self.assertEqual(len(await self.sql('SELECT 1 FROM checkout_requests')),2)
        alternative=self.http.post.call_args_list[2].kwargs
        self.assertEqual(alternative['json']['payment_method_data'],{'type':'sbp'})
        self.assertNotEqual(alternative['headers']['Idempotence-Key'],order['id'])
        self.assertNotIn('нажмите Ещё',self.last()['text'])

    async def test_smart_payment_success_grants_once_and_retires_both_payment_buttons(self):
        await self.press('user:buy')
        choices=await db.checkout_choices('p1')
        other=next(row for row in choices if row['payment_route']=='all')
        pid=other['payment_id']
        value=copy.deepcopy(self.provider_payments[pid])
        value.update(status='succeeded',paid=True)
        self.provider_payments[pid]=value
        # Checking the SBP invoice also discovers a completed Smart Payment.
        await self.press('user:check_payment:p1')
        until=(await db.get_subscription(1))['access_until']
        self.assertAlmostEqual((db.parse_dt(until)-db.utcnow()).total_seconds(),30*86400,delta=5)
        self.assertFalse(any(b.text in {'Через СБП','Другие способы'} for b in self.buttons()))
        self.assertEqual((await db.stats())['revenue_rub'],400)
        self.assertEqual((await db.get_payment(pid))['status'],'succeeded')
        await self.press('user:check_payment:'+pid)
        await bot.payment_reconciliation_job(self.ctx)
        self.assertEqual((await db.get_subscription(1))['access_until'],until)
        self.assertEqual(self.telegram.create_chat_invite_link.await_count,1)

    async def test_canceled_sbp_invoice_does_not_hide_successful_other_payment(self):
        await self.press('user:buy')
        self.http.get.return_value=self.response(self.payment(status='canceled'))
        for pid,value in self.provider_payments.items():
            if pid.startswith('alt-'):
                value.update(status='succeeded',paid=True)
        await self.press('user:check_payment:p1')
        self.assertIn('Оплата подтверждена',self.last()['text'])
        self.assertAlmostEqual((db.parse_dt((await db.get_subscription(1))['access_until'])-db.utcnow()).total_seconds(),30*86400,delta=5)
        self.assertEqual((await db.stats())['revenue_rub'],400)

    async def test_unknown_alternative_request_recovers_same_key_and_body_after_restart(self):
        async def post(url,**kwargs):
            if 'payment_method_data' not in kwargs['json']:
                raise httpx.ReadTimeout('alternative creation unknown')
            return await self.provider_post(url,**kwargs)
        self.http.post.side_effect=post
        await self.press('user:buy')
        original=self.http.post.call_args_list[1].kwargs
        self.assertEqual(self.buttons()[1].text,'Через СБП')
        self.assertIsNotNone(self.buttons()[1].url)
        callback=self.buttons()[2].callback_data
        self.assertTrue(callback.startswith('user:pay_options:'))
        self.assertLessEqual(len(callback.encode()),64)
        await db.close_db()
        await db.init_db()
        self.ctx.user_data={}
        self.http.post.side_effect=self.provider_post
        await self.press(callback)
        recovered=self.http.post.call_args_list[2].kwargs
        self.assertEqual(original['headers'],recovered['headers'])
        self.assertEqual(original['json'],recovered['json'])
        self.assertEqual(next(b.url for b in self.buttons() if b.text=='Другие способы'),'https://example.com/pay-all')
        self.assertEqual(len(await self.sql('SELECT * FROM checkout_requests')),2)
        await self.press('user:buy')
        self.assertEqual(self.http.post.await_count,3)

    async def test_known_payment_is_finalized_even_if_alternative_post_is_unknown(self):
        async def post(url,**kwargs):
            if 'payment_method_data' not in kwargs['json']:
                raise httpx.ReadTimeout('alternative creation unknown')
            return await self.provider_post(url,**kwargs)
        self.http.post.side_effect=post
        await self.press('user:buy')
        self.http.get.return_value=self.response(self.payment(status='succeeded'))
        await bot.payment_reconciliation_job(self.ctx)
        until=(await db.get_subscription(1))['access_until']
        self.assertIn('Оплата подтверждена',self.last()['text'])
        self.assertEqual((await db.stats())['revenue_rub'],400)
        await bot.payment_reconciliation_job(self.ctx)
        self.assertEqual((await db.get_subscription(1))['access_until'],until)
        self.assertEqual(self.telegram.create_chat_invite_link.await_count,1)

    async def test_pair_uses_frozen_quote_and_terms_when_admin_changes_both(self):
        await self.press('user:buy')
        snapshot=[(row['id'],row['request_json'],row['offer_url']) for row in await db.checkout_choices('p1')]
        await db.save_tariff('creative_30','Новая цена',700,60,True,99)
        await db.set_offer_url(OFFER+'/new')
        with patch.object(bot,'OFFER_VERSION','2'):
            await self.press('user:buy')
        self.assertIn('400',self.last()['text'])
        self.assertIn('30',self.last()['text'])
        self.assertEqual(self.buttons()[0].url,OFFER)
        self.assertEqual([(row['id'],row['request_json'],row['offer_url']) for row in await db.checkout_choices('p1')],snapshot)
        self.assertEqual(self.http.post.await_count,2)
        self.assertFalse(await db.has_accepted_offer(1))

    async def test_option_retry_cannot_read_or_change_another_users_quote(self):
        await self.press('user:buy')
        order=await db.get_checkout_by_payment('p1')
        await db.upsert_user(2,'other','Other')
        before=self.http.post.await_count
        await self.press('user:pay_options:'+order['id'],user=NS(id=2,username='other',first_name='Other',last_name=None))
        self.assertEqual(self.http.post.await_count,before)
        self.assertEqual(self.last()['text'],texts.PRODUCT)
        self.assertFalse(any(b.url=='https://example.com/pay' for b in self.buttons()))

    async def test_two_confirmed_payments_are_accounted_without_losing_paid_days(self):
        await self.press('user:buy')
        self.http.get.return_value=self.response(self.payment(status='succeeded'))
        for pid,value in self.provider_payments.items():
            if pid.startswith('alt-'):
                value.update(status='succeeded',paid=True)
        await bot.payment_reconciliation_job(self.ctx)
        until=(await db.get_subscription(1))['access_until']
        self.assertAlmostEqual((db.parse_dt(until)-db.utcnow()).total_seconds(),60*86400,delta=5)
        self.assertEqual((await db.stats())['revenue_rub'],800)
        await bot.payment_reconciliation_job(self.ctx)
        self.assertEqual((await db.get_subscription(1))['access_until'],until)

    async def test_alternative_is_not_created_for_saved_or_opted_in_auto_payment(self):
        await self.buy(auto=True)
        self.assertIsNone(await db.prepare_alternative_checkout('p1'))
        self.assertEqual(self.http.post.await_count,1)
        self.assertEqual([b.text for b in self.buttons() if b.url],['Через СБП'])


    async def offer_quiz(self):
        await bot.start(self.update,self.ctx)
        await self.press('user:open_lesson')
        await self.age_lead()
        await bot.followup_job(self.ctx)
        quiz=await db.get_lesson_quiz(1)
        self.assertIsNotNone(quiz)
        return quiz,self.last_active_id

    async def assert_quiz_result(self,answer,percent,amount):
        quiz,notice=await self.offer_quiz()
        lesson=await db.get_lesson_message(1)
        original=dict(self.messages[lesson['lesson_message_id']])
        sends=self.telegram.send_message.await_count
        await self.press('user:quiz:'+quiz['token']+':'+answer,message_id=notice)
        self.assertEqual(self.last_active_id,notice)
        self.assertEqual(self.telegram.send_message.await_count,sends)
        self.assertEqual([b.text for b in self.buttons()],['Через СБП','Другие способы'])
        self.assertTrue(all(b.url and b.callback_data is None for b in self.buttons()))
        self.assertIn(str(percent)+'%',self.last()['text'])
        self.assertIn('400 ₽',self.last()['text'])
        self.assertIn('К оплате: '+str(amount)+' ₽',self.last()['text'])
        self.assertIn(OFFER,self.last()['text'])
        self.assertEqual(self.messages[lesson['lesson_message_id']],original)
        posts=[call.kwargs for call in self.http.post.call_args_list]
        self.assertEqual(len(posts),2)
        self.assertEqual(posts[0]['json']['payment_method_data'],{'type':'sbp'})
        self.assertNotIn('payment_method_data',posts[1]['json'])
        self.assertNotEqual(posts[0]['headers'],posts[1]['headers'])
        for call in posts:
            self.assertEqual(call['json']['amount'],{'value':f'{amount:.2f}','currency':'RUB'})
            self.assertEqual(call['json']['metadata']['discount_percent'],str(percent))
            self.assertEqual(call['json']['metadata']['quiz_token'],quiz['token'])
            self.assertEqual(call['json']['metadata']['days'],'30')
            self.assertNotIn('save_payment_method',call['json'])
        self.assertIsNone(await db.get_subscription(1))
        return await db.get_lesson_quiz(1)

    async def test_quiz_question_has_exactly_three_answers_and_never_creates_payment(self):
        quiz,_=await self.offer_quiz()
        self.assertIn('Как тебе бесплатный урок',self.last()['text'])
        self.assertIn('фундаментальным',self.last()['text'])
        self.assertIn('30%',self.last()['text'])
        self.assertEqual([b.text for b in self.buttons()],['Форма','Функция','Правило'])
        self.assertTrue(all(len(b.callback_data.encode())<=64 for b in self.buttons()))
        self.assertIsNone(quiz['answer'])
        self.http.post.assert_not_awaited()

    async def test_quiz_rule_awards_thirty_percent_and_two_direct_payment_buttons(self):
        quiz=await self.assert_quiz_result('rule',30,280)
        self.assertEqual(quiz['discount_percent'],30)
        self.assertIn('Верно!',self.last()['text'])
        self.assertNotIn(URL,self.last()['text'])
        self.assertNotIn('link_preview_options',self.last())

    async def test_quiz_form_awards_fifteen_percent_and_replays_the_video(self):
        quiz=await self.assert_quiz_result('form',15,340)
        self.assertEqual(quiz['discount_percent'],15)
        text=self.last()['text']
        self.assertIn('Отнюдь нет, дружок',text)
        self.assertIn('«Правило»',text)
        self.assertLess(text.index(URL),text.index('утешительного приза'))
        self.assertEqual(self.last()['link_preview_options'].url,URL)
        self.assertFalse(self.last()['link_preview_options'].is_disabled)

    async def test_quiz_function_also_awards_fifteen_percent(self):
        await self.assert_quiz_result('function',15,340)

    async def test_quiz_first_wrong_answer_cannot_be_changed_to_rule_after_restart(self):
        quiz=await self.assert_quiz_result('form',15,340)
        original=[copy.deepcopy(call.kwargs) for call in self.http.post.call_args_list]
        await db.close_db()
        await db.init_db()
        self.ctx.user_data={}
        await self.press('user:quiz:'+quiz['token']+':rule')
        self.assertEqual((await db.get_lesson_quiz(1))['answer'],'form')
        self.assertIn('15%',self.last()['text'])
        self.assertEqual([call.kwargs for call in self.http.post.call_args_list],original)
        self.assertEqual(len(await self.sql("SELECT * FROM funnel_events WHERE event_name='quiz_answered'")),1)

    async def test_concurrent_quiz_answers_create_only_one_discounted_pair(self):
        quiz,notice=await self.offer_quiz()
        await asyncio.gather(self.press('user:quiz:'+quiz['token']+':form',message_id=notice),
                             self.press('user:quiz:'+quiz['token']+':rule',message_id=notice))
        saved=await db.get_lesson_quiz(1)
        self.assertIn(saved['answer'],{'form','rule'})
        self.assertEqual(self.http.post.await_count,2)
        expected='280.00' if saved['answer']=='rule' else '340.00'
        self.assertEqual({call.kwargs['json']['amount']['value'] for call in self.http.post.call_args_list},{expected})

    async def test_quiz_token_cannot_be_used_by_another_user_or_before_invitation(self):
        quiz,_=await self.offer_quiz()
        other=NS(id=2,username='other',first_name='Other',last_name=None)
        await db.upsert_user(2)
        await self.press('user:quiz:'+quiz['token']+':rule',user=other)
        self.assertIsNone(await db.get_lesson_quiz(2))
        self.assertIsNone((await db.get_lesson_quiz(1))['answer'])
        await self.press('user:quiz:'+'0'*32+':rule')
        self.assertIsNone((await db.get_lesson_quiz(1))['answer'])
        self.http.post.assert_not_awaited()

    async def test_quiz_discounted_pair_keeps_price_duration_and_offer_after_admin_changes(self):
        quiz=await self.assert_quiz_result('rule',30,280)
        snapshot=quiz['quote_json']
        await db.save_tariff('creative_30','Новый тариф',700,60,True,99)
        await db.set_offer_url(OFFER+'/changed')
        with patch.object(bot,'OFFER_VERSION','2'):
            await self.press('user:buy')
        self.assertEqual((await db.get_lesson_quiz(1))['quote_json'],snapshot)
        self.assertIn('К оплате: 280 ₽',self.last()['text'])
        self.assertIn('30 дней',self.last()['text'])
        self.assertIn(OFFER,self.last()['text'])
        self.assertNotIn(OFFER+'/changed',self.last()['text'])
        self.assertEqual(self.http.post.await_count,2)

    async def test_quiz_uses_exact_kopecks_for_new_admin_price(self):
        await db.save_tariff('creative_30','Цена с копейками после скидки',999,30,True,99)
        quiz,_=await self.offer_quiz()
        await self.press('user:quiz:'+quiz['token']+':rule')
        self.assertEqual({call.kwargs['json']['amount']['value'] for call in self.http.post.call_args_list},{'699.30'})
        self.assertIn('К оплате: 699,30 ₽',self.last()['text'])
        own=[value for key,value in self.provider_payments.items() if key.startswith('quiz-')][0]
        own.update(status='succeeded',paid=True)
        await bot.payment_reconciliation_job(self.ctx)
        self.assertEqual((await db.stats())['revenue_rub'],699.30)

    async def test_quiz_replaces_known_full_price_invoices_without_rewriting_them(self):
        quiz,notice=await self.offer_quiz()
        await self.press('user:buy')
        before=[dict(row) for row in await self.sql('SELECT * FROM checkout_requests')]
        self.assertEqual(self.http.post.await_count,2)
        await self.press('user:quiz:'+quiz['token']+':rule',message_id=notice)
        self.assertEqual(self.http.post.await_count,4)
        for row in before:
            current=await db.get_checkout(row['id'])
            self.assertEqual(current['request_json'],row['request_json'])
            self.assertEqual(current['id'],row['id'])
            self.assertEqual(current['status'],'superseded')
            self.assertEqual((await db.get_payment(row['payment_id']))['status'],'pending')
        self.assertEqual([call.kwargs['json']['amount']['value'] for call in self.http.post.call_args_list],['400.00','400.00','280.00','280.00'])
        await self.press('user:check_payment:p1')
        self.assertEqual([b.text for b in self.buttons()],['Через СБП','Другие способы'])
        self.assertIn('К оплате: 280 ₽',self.last()['text'])

    async def test_unknown_quiz_post_recovers_same_key_without_automatic_extra_messages(self):
        quiz,_=await self.offer_quiz()
        self.http.post.side_effect=httpx.ReadTimeout('unknown')
        await self.press('user:quiz:'+quiz['token']+':form')
        first=copy.deepcopy(self.http.post.call_args.kwargs)
        self.assertEqual(first['json']['amount']['value'],'340.00')
        self.assertTrue(all(b.callback_data for b in self.buttons()))
        sends=self.telegram.send_message.await_count
        await db.close_db()
        await db.init_db()
        self.ctx.user_data={}
        self.http.post.side_effect=self.provider_post
        await bot.payment_reconciliation_job(self.ctx)
        second=self.http.post.call_args.kwargs
        self.assertEqual(first['headers'],second['headers'])
        self.assertEqual(first['json'],second['json'])
        self.assertEqual(self.telegram.send_message.await_count,sends)
        await self.press('user:quiz_pay:'+quiz['token'])
        self.assertTrue(all(b.url for b in self.buttons()))
        self.assertEqual(self.http.post.await_count,3)

    async def test_unknown_old_invoice_is_resolved_before_new_discount_request(self):
        quiz,_=await self.offer_quiz()
        self.http.post.side_effect=httpx.ReadTimeout('old outcome unknown')
        await self.press('user:buy')
        original=copy.deepcopy(self.http.post.call_args.kwargs)
        await self.press('user:quiz:'+quiz['token']+':function')
        self.assertTrue(all(b.callback_data for b in self.buttons()))
        for call in self.http.post.call_args_list:
            self.assertEqual(call.kwargs['headers'],original['headers'])
            self.assertEqual(call.kwargs['json']['amount']['value'],'400.00')
        self.http.post.side_effect=self.provider_post
        await self.press('user:quiz_pay:'+quiz['token'])
        values=[call.kwargs['json']['amount']['value'] for call in self.http.post.call_args_list]
        self.assertEqual(values,['400.00','400.00','400.00','340.00','340.00'])
        self.assertIn('К оплате: 340 ₽',self.last()['text'])

    async def test_quiz_success_through_other_methods_grants_access_once_and_redeems_discount(self):
        quiz=await self.assert_quiz_result('rule',30,280)
        for pid,value in self.provider_payments.items():
            if pid.startswith('alt-'):
                value.update(status='succeeded',paid=True)
        await bot.payment_reconciliation_job(self.ctx)
        saved=await db.get_lesson_quiz(1)
        self.assertTrue(saved['redeemed_payment_id'].startswith('alt-'))
        until=(await db.get_subscription(1))['access_until']
        self.assertAlmostEqual((db.parse_dt(until)-db.utcnow()).total_seconds(),30*86400,delta=5)
        self.assertEqual((await db.stats())['revenue_rub'],280)
        self.assertTrue(await db.has_accepted_offer(1))
        self.assertIsNone(await db.get_renewal(1))
        await self.press('user:quiz:'+quiz['token']+':rule')
        await bot.payment_reconciliation_job(self.ctx)
        self.assertEqual((await db.get_subscription(1))['access_until'],until)
        self.assertEqual(self.http.post.await_count,2)
        self.assertFalse(any(b.text in {'Через СБП','Другие способы'} for b in self.buttons()))

    async def test_quiz_rejects_wrong_provider_amount_and_never_grants_access(self):
        await self.assert_quiz_result('form',15,340)
        root=next(value for pid,value in self.provider_payments.items() if pid.startswith('quiz-'))
        root.update(status='succeeded',paid=True,amount={'value':'1.00','currency':'RUB'})
        await bot.payment_reconciliation_job(self.ctx)
        self.assertIsNone(await db.get_subscription(1))
        self.assertIsNone((await db.get_lesson_quiz(1))['redeemed_payment_id'])

    async def test_quiz_definitive_rejection_keeps_the_discount_for_a_new_request(self):
        quiz,_=await self.offer_quiz()
        request=httpx.Request('POST',billing.API_URL)
        self.http.post.side_effect=httpx.HTTPStatusError('rejected',request=request,response=httpx.Response(400,request=request))
        await self.press('user:quiz:'+quiz['token']+':function')
        first=copy.deepcopy(self.http.post.call_args.kwargs)
        self.assertIsNone(await db.pending_checkout(1))
        self.assertEqual((await db.get_lesson_quiz(1))['discount_percent'],15)
        self.assertTrue(all(b.callback_data for b in self.buttons()))
        self.http.post.side_effect=self.provider_post
        await self.press('user:quiz_pay:'+quiz['token'])
        second=self.http.post.call_args_list[1].kwargs
        self.assertEqual(first['json'],second['json'])
        self.assertNotEqual(first['headers'],second['headers'])
        self.assertIn('К оплате: 340 ₽',self.last()['text'])

    async def test_purchase_at_old_price_does_not_trap_later_renewal_in_quiz(self):
        quiz,notice=await self.offer_quiz()
        await self.press('user:buy')
        await self.press('user:quiz:'+quiz['token']+':rule',message_id=notice)
        self.http.get.return_value=self.response(self.payment(status='succeeded'))
        for value in self.provider_payments.values():
            if value['id']!='p1':
                value.update(status='canceled',paid=False)
        await bot.payment_reconciliation_job(self.ctx)
        self.assertTrue(bot.active_subscription_until(await db.get_subscription(1)))
        await self.sql("UPDATE subscriptions SET access_until=?,status='expired' WHERE tg_user_id=1",((db.utcnow()-timedelta(days=1)).isoformat(),))
        self.http.post.return_value=self.response(self.payment('renewal_new'))
        await self.press('user:buy')
        self.assertEqual(self.http.post.call_args_list[-2].kwargs['json']['amount']['value'],'400.00')
        self.assertNotIn('quiz_token',self.http.post.call_args_list[-2].kwargs['json']['metadata'])
        self.assertNotIn('Твоя скидка',self.last()['text'])

    async def test_quiz_is_not_reminded_again_if_ignored_or_answered_without_payment(self):
        quiz,_=await self.offer_quiz()
        for _ in range(2):
            await bot.start(self.update,self.ctx)
            await self.press('user:open_lesson')
            await self.age_lead()
            sends=self.telegram.send_message.await_count
            await bot.followup_job(self.ctx)
            self.assertEqual(self.telegram.send_message.await_count,sends)
        await self.press('user:quiz:'+quiz['token']+':form')
        sends=self.telegram.send_message.await_count
        await bot.followup_job(self.ctx)
        self.assertEqual(self.telegram.send_message.await_count,sends)
        self.assertEqual(len(await self.sql('SELECT * FROM followup_deliveries')),1)
        self.assertEqual(len(await self.sql('SELECT * FROM lesson_quizzes')),1)


class ApplicationTests(unittest.TestCase):
    def test_admin_env_precedence_and_fail_closed(self):
        cases = [
            ({}, set()),
            ({"ADMIN_ID": "99"}, {99}),
            ({"ADMIN_ID": "99", "ADMIN_IDS": "1,2", "ADMINS": "3"}, {99}),
            ({"ADMIN_ID": "", "ADMIN_IDS": "99,100"}, {99, 100}),
            ({"ADMIN_ID": " ", "ADMIN_IDS": "", "ADMINS": "99"}, {99}),
            ({"ADMIN_ID": "not-an-id", "ADMIN_IDS": "1"}, set()),
            ({"ADMIN_ID": "0 -42"}, set()),
        ]
        for env, expected in cases:
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True):
                self.assertEqual(bot.parse_admin_ids(), expected)

    def test_handlers_and_financial_jobs_are_registered(self):
        with patch.object(bot, "BOT_TOKEN", "123456:TEST_ONLY_PLACEHOLDER"):
            app = bot.build_application()
        self.assertEqual({job.name for job in app.job_queue.jobs()},
                         {"renewal_notices", "expire_subscriptions", "lead_followup", "payment_reconciliation", "automatic_renewals"})
        self.assertTrue(app.error_handlers)
        commands = {command for handler in app.handlers[0] for command in getattr(handler, "commands", [])}
        self.assertTrue({"start", "admin", "set_lead_magnet", "set_intro_video", "grant", "revoke", "stats"}.issubset(commands))
        self.assertNotIn("reviews", commands)


if __name__ == "__main__":
    unittest.main()
