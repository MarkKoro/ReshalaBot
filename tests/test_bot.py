import asyncio
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
        self.next_id = 1

        async def send(chat_id, text, **kwargs):
            mid = self.next_id
            self.next_id += 1
            self.messages[mid] = dict(chat_id=chat_id, text=text, **kwargs)
            return NS(message_id=mid)

        async def edit(chat_id, message_id, text, **kwargs):
            self.messages[message_id] = dict(chat_id=chat_id, text=text, **kwargs)

        self.telegram = NS(
            send_message=AsyncMock(side_effect=send),
            edit_message_text=AsyncMock(side_effect=edit),
            delete_message=AsyncMock(),
            create_chat_invite_link=AsyncMock(return_value=NS(invite_link="https://t.me/+test-invite")),
            revoke_chat_invite_link=AsyncMock(),
            ban_chat_member=AsyncMock(),
            unban_chat_member=AsyncMock(),
        )
        self.ctx = NS(bot=self.telegram, args=[], user_data={})
        self.user = NS(id=1, username="test", first_name="Test", last_name=None)
        self.chat = NS(id=1, type="private")
        self.update = NS(effective_user=self.user, effective_chat=self.chat,
                         effective_message=NS(message_id=9000, text="/start"))
        self.http = MagicMock()
        self.http.__aenter__ = AsyncMock(return_value=self.http)
        self.http.__aexit__ = AsyncMock(return_value=False)
        self.http.post = AsyncMock()
        self.http.get = AsyncMock()
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

    async def press(self, data, user=None):
        user = user or self.user
        query = NS(from_user=user, message=NS(chat_id=user.id, chat=NS(type="private")),
                   data=data, answer=AsyncMock())
        await bot.router(NS(callback_query=query), self.ctx)

    def last(self):
        return self.messages[max(self.messages)]

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
        lesson = dict(self.messages[1])
        self.assertIn(texts.WELCOME, lesson["text"])
        preview = lesson["link_preview_options"]
        self.assertTrue(preview.prefer_large_media)
        self.assertTrue(preview.show_above_text)
        self.assertFalse(preview.is_disabled)
        self.assertFalse(await db.has_accepted_offer(1))
        await self.press("user:product")
        self.assertEqual(self.last()["text"], texts.PRODUCT)
        await self.press("user:buy")
        self.assertEqual(self.last()["text"], texts.OFFER)
        self.assertEqual(self.buttons()[0].url, OFFER)
        self.http.post.return_value = self.response(self.payment())
        await self.press("user:accept_offer_and_pay")
        self.assertEqual(self.last()["text"], texts.PAYMENT_CREATED)
        payload = self.http.post.call_args.kwargs["json"]
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
        self.assertEqual(args["member_limit"], 1)
        self.assertAlmostEqual((args["expire_date"] - db.utcnow()).total_seconds(), 3600, delta=5)
        self.assertEqual(len(self.buttons()), 1)
        self.assertEqual(self.buttons()[0].url, "https://t.me/+test-invite")
        self.assertNotIn("https://t.me/", self.last()["text"])
        self.assertEqual(self.messages[1], lesson)
        self.telegram.delete_message.assert_not_awaited()
        events = [row["event_name"] for row in await db.get_user_path(1)]
        self.assertEqual(events, ["start", "lead_magnet_sent", "product_opened", "buy_clicked",
                                  "offer_accepted", "payment_created", "payment_succeeded", "access_granted"])
        success_id = max(self.messages)
        success = dict(self.messages[success_id])
        await bot.start(self.update, self.ctx)
        self.assertEqual(self.messages[success_id], success)

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
        self.assertEqual(self.last()["text"], texts.ACTIVE_ACCESS.format(date=bot.format_dt(original["access_until"])))
        self.assertEqual(self.buttons()[0].callback_data, "user:buy")
        await db.accept_offer(1)
        self.http.post.return_value = self.response(self.payment())
        await self.press("user:buy")
        self.assertEqual(self.http.post.await_count, 1)
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
            self.assertEqual(self.last()["text"], texts.PAYMENT_PENDING)
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
            self.assertIn("Не получилось", self.last()["text"])
        await self.store_payment()
        self.http.get.side_effect = httpx.ReadTimeout("test")
        await self.press("user:check_payment:p1")
        self.assertIsNone(await db.get_subscription(1))
        self.assertIn("Не получилось проверить", self.last()["text"])

    async def test_canceled_payment_can_create_new_invoice(self):
        await db.accept_offer(1)
        await self.store_payment()
        self.http.get.return_value = self.response(self.payment(status="canceled"))
        await self.press("user:check_payment:p1")
        self.http.post.return_value = self.response(self.payment(pid="p2"))
        await self.press("user:buy")
        self.assertIsNotNone(await db.get_payment("p2"))

    async def test_double_buy_reuses_invoice_and_legacy_buttons_only_open_product(self):
        await db.accept_offer(1)
        self.http.post.return_value = self.response(self.payment())
        await self.press("user:buy")
        await self.press("user:buy")
        self.assertEqual(self.http.post.await_count, 1)
        for route in ("user:tariff:m1_first", "user:tariff:m1", "user:tariff:m3", "user:tariffs", "user:review:0"):
            await self.press(route)
            self.assertEqual(self.last()["text"], texts.PRODUCT)
        self.assertEqual(self.http.post.await_count, 1)

    async def test_offer_missing_stale_or_changed_blocks_payment(self):
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
        self.assertIn("Оплата сохранена", self.last()["text"])
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
        self.http.get.side_effect = None
        self.http.get.return_value = self.response(self.payment())
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
        self.update.effective_message.text = "https://youtube.com.evil.example/watch?v=dQw4w9WgXcQ"
        await bot.handle_admin_input(self.update, self.ctx)
        self.assertEqual(await db.get_lead_magnet_url(), URL)
        self.update.effective_message.text = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        await bot.handle_admin_input(self.update, self.ctx)
        self.assertEqual(await db.get_lead_magnet_url(), self.update.effective_message.text)
        await self.press("admin:lead_test")
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
            await bot.send_lead_magnet(1, 1, self.ctx)
        self.assertFalse(self.last()["disable_web_page_preview"])
        await db.clear_lead_magnet_url()
        await bot.send_lead_magnet(1, 1, self.ctx)
        events = [r for r in await db.get_user_path(1) if r["event_name"] == "lead_magnet_sent"]
        self.assertEqual(len(events), 1)

    async def test_large_preview_api_rejection_falls_back(self):
        original_send = self.telegram.send_message.side_effect
        count = 0
        async def reject_first(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 1:
                raise BadRequest("unsupported link_preview_options")
            return await original_send(*args, **kwargs)
        self.telegram.send_message.side_effect = reject_first
        await bot.send_lead_magnet(1, 1, self.ctx)
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
        self.assertEqual(len(self.buttons()), 6)
        await self.press("admin:stats", admin_user)
        self.assertIn("Start", self.last()["text"])
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
        self.assertEqual(len(self.buttons()), 6)
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
            self.assertEqual(len(self.buttons()), 6)

    async def test_regular_user_gets_no_admin_button_and_price_only_at_checkout(self):
        await bot.start(self.update, self.ctx)
        await self.press("user:product")
        self.assertLess(len(self.last()["text"]), 450)
        self.assertEqual(self.buttons()[0].text, "Поехали →")
        self.assertNotIn("400", self.last()["text"])
        await self.press("user:buy")
        self.assertNotIn("400", self.last()["text"])
        self.assertFalse(any("400" in b.text for b in self.buttons()))
        self.http.post.return_value = self.response(self.payment())
        await self.press("user:accept_offer_and_pay")
        self.assertNotIn("400", self.last()["text"])
        self.assertEqual(sum("400" in b.text for b in self.buttons()), 1)
        self.assertEqual(self.buttons()[0].text, "💳 Оплатить 400 ₽")
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

    def test_handlers_and_three_jobs_are_registered(self):
        with patch.object(bot, "BOT_TOKEN", "123456:TEST_ONLY_PLACEHOLDER"):
            app = bot.build_application()
        self.assertEqual({job.name for job in app.job_queue.jobs()},
                         {"renewal_notices", "expire_subscriptions", "lead_followup"})
        self.assertTrue(app.error_handlers)
        commands = {command for handler in app.handlers[0] for command in getattr(handler, "commands", [])}
        self.assertTrue({"start", "admin", "set_lead_magnet", "set_intro_video", "grant", "revoke", "stats"}.issubset(commands))
        self.assertNotIn("reviews", commands)


if __name__ == "__main__":
    unittest.main()
