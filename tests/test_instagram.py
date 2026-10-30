import copy
import hashlib
import hmac
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import bot
import db
import instagram_automation as campaign
from instagram_gateway import Gateway, GraphError
import test_bot


class CampaignTests(unittest.TestCase):
    def test_only_creative_keyword_by_default_and_whole_words(self):
        self.assertEqual(campaign.DEFAULTS["keywords"], ["креатив"])
        for value in ("КРЕАТИВ!!!", "Можно креатив 🧠", "#креатив", "Креатив"):
            self.assertTrue(campaign.matches(value, ["креатив"]))
        for value in ("видео", "ссылка", "некреативный", "креативность", ""):
            self.assertFalse(campaign.matches(value, ["креатив"]))

    def test_keyword_validation_and_phrases(self):
        self.assertEqual(campaign.parse_keywords("КРЕАТИВ, креатив; новый урок\nВИДЕО"), ["креатив", "новый урок", "видео"])
        self.assertTrue(campaign.matches("Новый, урок!", ["новый урок"]))
        for value in ("a" * 41, ",".join("word" + str(n) for n in range(31))):
            with self.assertRaises(ValueError):
                campaign.parse_keywords(value)

    def test_urls_and_text_variants(self):
        self.assertEqual(campaign.validate_url("https://t.me/BioMozg_agent?start=instagram"), "https://t.me/BioMozg_agent?start=instagram")
        for value in ("http://t.me/test", "javascript:alert(1)", "https://127.0.0.1/", "https://user:password@example.com/", "https://example.local/", "https://example.com:8443/", "https://example.com/\nsecret", None):
            with self.assertRaises(ValueError):
                campaign.validate_url(value)
        self.assertEqual(campaign.variants("Первый\n---\nВторой"), ["Первый", "Второй"])
        for value in ("", "a" * 901, "a\n---\nb\n---\nc\n---\nd"):
            with self.assertRaises(ValueError):
                campaign.variants(value)

    def test_atomic_persistence_and_empty_list_disables(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            settings = copy.deepcopy(campaign.DEFAULTS)
            settings.update(enabled=True, keywords=[])
            campaign.save_settings(settings, path)
            loaded = campaign.load_settings(path)
            self.assertFalse(loaded["enabled"])
            before = path.read_bytes()
            settings["target_url"] = "http://localhost"
            with self.assertRaises(ValueError):
                campaign.save_settings(settings, path)
            self.assertEqual(path.read_bytes(), before)

    def test_variants_are_stable_and_stale_status_fails_closed(self):
        self.assertEqual(campaign.choose(["a", "b", "c"], "person"), campaign.choose(["a", "b", "c"], "person"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            path.write_text(json.dumps({"updated_at": time.time() - 121, "connected": True}))
            self.assertFalse(campaign.connection_status(path)["connected"])


class InstagramAdminTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = test_bot.BotTests(methodName="runTest")
        await self.fixture.asyncSetUp()
        self.config_path = Path(self.fixture.temp.name) / "instagram-settings.json"
        self.paths = patch.multiple(campaign, CONFIG_PATH=self.config_path,
                                    STATUS_PATH=Path(self.fixture.temp.name) / "status.json")
        self.paths.start()
        self.admin = NS(id=99, username="admin", first_name="Admin", last_name=None)
        self.update = NS(effective_user=self.admin, effective_chat=NS(id=99, type="private"),
                         effective_message=NS(message_id=9000, text=""))

    async def asyncTearDown(self):
        self.paths.stop()
        await self.fixture.asyncTearDown()

    async def press(self, route, user=None):
        await self.fixture.press(route, user=user or self.admin)

    async def enter(self, text):
        self.update.effective_message.text = text
        await bot.handle_admin_input(self.update, self.fixture.ctx)

    async def test_menu_and_preview_are_admin_only_and_never_send_instagram(self):
        for route in ("admin:instagram", "admin:instagram:toggle", "admin:instagram:edit:target_url",
                      "admin:instagram:keywords", "admin:instagram:public_toggle", "admin:instagram:preview"):
            await self.press(route, user=self.fixture.user)
        self.assertEqual(self.fixture.messages, {})
        self.assertFalse(self.config_path.exists())
        self.assertIsNone(await db.get_admin_state(1))
        await self.press("admin:menu")
        self.assertIn("admin:instagram", [b.callback_data for b in self.fixture.buttons()])
        await self.press("admin:instagram:preview")
        self.assertIn("тест ничего не отправляет", self.fixture.last()["text"])
        self.assertIn("https://t.me/BioMozg_agent", self.fixture.last()["text"])
        self.assertFalse(self.config_path.exists())
        self.fixture.http.post.assert_not_awaited()

    async def test_add_remove_replace_keywords_and_audit(self):
        await self.press("admin:instagram:edit:keywords_add")
        await self.enter("ВИДЕО, креатив")
        self.assertEqual(campaign.load_settings()["keywords"], ["креатив", "видео"])
        await self.press("admin:instagram:edit:keywords_remove")
        await self.enter("Видео")
        self.assertEqual(campaign.load_settings()["keywords"], ["креатив"])
        await self.press("admin:instagram:toggle")
        self.assertTrue(campaign.load_settings()["enabled"])
        await self.press("admin:instagram:edit:keywords")
        await self.enter("удалить")
        self.assertFalse(campaign.load_settings()["enabled"])
        self.assertEqual(campaign.load_settings()["keywords"], [])
        await self.press("admin:instagram:toggle")
        self.assertFalse(campaign.load_settings()["enabled"])
        self.assertEqual(len(await self.fixture.sql("SELECT * FROM admin_audit")), 4)

    async def test_link_text_and_button_validation_persist(self):
        await self.press("admin:instagram:edit:target_url")
        await self.enter("http://localhost")
        self.assertIsNotNone(await db.get_admin_state(99))
        self.assertFalse(self.config_path.exists())
        await self.enter("https://t.me/BioMozg_agent?start=ig")
        await self.press("admin:instagram:edit:opening")
        await self.enter("Первый\n---\nВторой")
        await self.press("admin:instagram:edit:link_label")
        await self.enter("x" * 21)
        self.assertIsNotNone(await db.get_admin_state(99))
        await self.enter("Смотреть видео")
        saved = campaign.load_settings()
        self.assertEqual(saved["opening"], ["Первый", "Второй"])
        self.assertEqual(saved["link_label"], "Смотреть видео")
        self.assertEqual(saved["target_url"], "https://t.me/BioMozg_agent?start=ig")

    async def test_stale_nonadmin_input_and_group_input_cannot_write(self):
        await db.set_admin_state(1, "instagram", {"key": "target_url"})
        self.fixture.update.effective_message.text = "https://example.com"
        self.assertFalse(await bot.handle_admin_input(self.fixture.update, self.fixture.ctx))
        await self.press("admin:instagram:edit:target_url")
        self.update.effective_chat = NS(id=-1009, type="supergroup")
        self.update.effective_message.text = "https://example.com"
        self.assertFalse(await bot.handle_admin_input(self.update, self.fixture.ctx))
        self.assertFalse(await bot.instagram_admin_input("https://example.com", {"payload": {"key": "target_url"}}, self.update, self.fixture.ctx))
        self.assertFalse(self.config_path.exists())
        self.assertEqual(await self.fixture.sql("SELECT * FROM admin_audit"), [])


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "secrets").mkdir()
        self.credentials = {"ig_user_id": "100", "app_id": "101", "api_version": "v24.0",
                            "access_token": "test-only", "app_secret": "test-secret", "runner_token": "test-runner"}
        (self.root / "secrets" / "credentials.json").write_text(json.dumps(self.credentials))
        self.path = self.root / "settings.json"
        settings = copy.deepcopy(campaign.DEFAULTS)
        settings["enabled"] = True
        campaign.save_settings(settings, self.path)
        self.gateway = Gateway(self.root, self.path)
        self.gateway.ready = Mock(return_value=True)
        self.gateway.connected = True
        self.gateway.graph = Mock(side_effect=self.graph)
        self.gateway.sync_comments = Mock()  # Message tests isolate the other API source.

    def tearDown(self):
        self.temp.cleanup()

    def graph(self, method, endpoint, payload=None):
        if method == "GET":
            from datetime import datetime, timezone
            return {"id": "300", "text": "КРЕАТИВ", "timestamp": datetime.now(timezone.utc).isoformat()}
        if endpoint.endswith("/replies"):
            return {"id": "reply-300"}
        return {"message_id": "sent-" + str(self.gateway.graph.call_count), "recipient_id": "200"}

    def comment(self, comment="300", person="200", text="Креатив", stamp=None):
        self.gateway.ingest({"object": "instagram", "entry": [{"id": "100", "time": stamp or time.time(),
            "changes": [{"field": "comments", "value": {"id": comment, "from": {"id": person}, "text": text}}]}]})

    def message(self, mid="inbound-1", person="200", text="Да", payload=campaign.OPT_IN, stamp=None):
        self.gateway.ingest({"object": "instagram", "entry": [{"id": "100", "messaging": [{
            "sender": {"id": person}, "timestamp": (stamp or time.time()) * 1000,
            "message": {"mid": mid, "text": text, "quick_reply": {"payload": payload}},
        }]}]})

    def rows(self):
        with sqlite3.connect(self.gateway.database) as con:
            return con.execute("SELECT id,status,result FROM events ORDER BY id").fetchall()

    def sync_fixture(self, messages, participants=None):
        credentials = dict(self.credentials, api_mode="business_system_user")
        (self.root / "secrets" / "credentials.json").write_text(json.dumps(credentials))
        self.gateway.update_person("200", opened_at=time.time() - 10)
        def graph(method, endpoint, payload=None):
            if method == "GET" and "/thread_owner?" in endpoint:
                return {"data": [{"thread_owner": {"app_id": "101"}}]}
            if method == "GET" and "/conversations?" in endpoint:
                return {"data": [{"id": "thread:one", "participants": {"data": [
                    {"id": person} for person in (participants or ["100", "200"])]}}]}
            if method == "GET" and "/messages?" in endpoint:
                return {"data": messages}
            return self.graph(method, endpoint, payload)
        self.gateway.graph.side_effect = graph

    @staticmethod
    def api_message(mid="actual-consent", text="Да, хочу видео", sender="200", recipient="100", stamp=None):
        from datetime import datetime, timezone
        return {"id": mid, "from": {"id": sender}, "to": {"data": [{"id": recipient}]},
                "created_time": datetime.fromtimestamp(stamp or time.time() - 1, timezone.utc).isoformat(),
                "message": text}

    def test_sync_recovers_actual_consent_without_webhook_and_deduplicates_later_webhook(self):
        self.sync_fixture([self.api_message()])
        self.gateway.tick()
        self.assertIn(("message:actual-consent", "sent", ""), self.rows())
        self.assertEqual(self.gateway.last_ingress, 0)  # API sync is not reported as a delivered webhook.
        self.assertEqual(self.gateway.sync_status["queued"], 1)
        self.message(mid="actual-consent")
        self.gateway.tick()
        sends = [c for c in self.gateway.graph.call_args_list if c.args[:2] == ("POST", "100/messages")]
        self.assertEqual(len(sends), 1)
        self.assertGreater(self.gateway.conversation("200")["link_sent_at"], 0)

    def test_sync_plain_check_does_not_send_and_uses_cached_thread_after_restart(self):
        self.sync_fixture([self.api_message(text="проверка")])
        self.gateway.tick()
        self.assertIn(("message:actual-consent", "skipped", "no_keyword_or_consent"), self.rows())
        self.assertFalse(any(c.args[0] == "POST" for c in self.gateway.graph.call_args_list))
        fresh = Gateway(self.root, self.path)
        fresh.graph = Mock(side_effect=self.gateway.graph.side_effect)
        fresh.sync_messages()
        fresh.graph.assert_not_called()  # The per-contact limit survives a restart.
        with sqlite3.connect(self.gateway.database) as con:
            con.execute("UPDATE message_sync SET checked_at=0")
        fresh.sync_messages()
        self.assertEqual(fresh.graph.call_count, 2)
        self.assertTrue(fresh.graph.call_args.args[1].startswith("thread%3Aone/messages?"))
        self.assertEqual(fresh.sync_status["queued"], 0)

    def test_sync_recognizes_the_opt_in_button_label_configured_in_admin(self):
        settings = campaign.load_settings(self.path)
        settings["opt_in_label"] = "Отправить ссылку"
        campaign.save_settings(settings, self.path)
        self.sync_fixture([self.api_message(text=settings["opt_in_label"])])
        self.gateway.tick()
        self.assertIn(("message:actual-consent", "sent", ""), self.rows())
        self.assertEqual(sum(c.args[0] == "POST" for c in self.gateway.graph.call_args_list), 1)

    def test_sync_requires_both_thread_participants_before_reading_bodies(self):
        self.sync_fixture([self.api_message()], participants=["100", "999"])
        self.gateway.tick()
        self.assertEqual(self.gateway.graph.call_count, 2)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.gateway.sync_status["last_error"], "conversation_not_found")

    def test_sync_does_not_read_or_answer_a_thread_owned_by_another_app(self):
        self.sync_fixture([self.api_message()])
        self.gateway.graph.side_effect = lambda *_args: {"data": [{"thread_owner": {"app_id": "999"}}]}
        self.gateway.tick()
        self.assertEqual(self.gateway.graph.call_count, 1)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.gateway.sync_status["last_error"], "not_thread_owner")

    def test_sync_rejects_foreign_recipients_old_and_future_messages(self):
        self.sync_fixture([self.api_message(mid="foreign", recipient="999"),
                           self.api_message(mid="old", stamp=time.time()-100),
                           self.api_message(mid="future", stamp=time.time()+100)])
        self.gateway.tick()
        self.assertEqual(self.rows(), [])
        self.assertFalse(any(c.args[0] == "POST" for c in self.gateway.graph.call_args_list))

    def test_sync_later_stop_prevents_sending_an_earlier_consent_even_while_paused(self):
        for paused in (False, True):
            with self.subTest(paused=paused):
                self.sync_fixture([self.api_message(mid="stop", text="Стоп"),
                                   self.api_message(stamp=time.time()-3)])
                self.gateway.update_person("200", opted_out=0)
                self.gateway.last_sync = 0
                with sqlite3.connect(self.gateway.database) as con:
                    con.execute("DELETE FROM message_sync")
                settings = campaign.load_settings(self.path)
                settings["enabled"] = not paused
                campaign.save_settings(settings, self.path)
                self.gateway.tick()
                self.assertTrue(self.gateway.conversation("200")["opted_out"])
                self.assertFalse(any(c.args[0] == "POST" for c in self.gateway.graph.call_args_list))

    def test_sync_known_bot_echo_is_safe_but_later_manual_reply_pauses_consent(self):
        self.gateway.remember_sent({"message_id": "our-opening"})
        self.sync_fixture([self.api_message(mid="manual", text="Отвечу лично", sender="100", recipient="200"),
                           self.api_message(stamp=time.time()-3),
                           self.api_message(mid="our-opening", text="Прислать?", sender="100", recipient="200", stamp=time.time()-5)])
        self.gateway.tick()
        self.assertGreater(self.gateway.conversation("200")["human_until"], time.time())
        self.assertFalse(any(c.args[0] == "POST" for c in self.gateway.graph.call_args_list))

    def test_sync_does_not_bypass_unknown_delivery_using_another_consent_id(self):
        self.sync_fixture([self.api_message()])
        self.gateway.enqueue("message:uncertain", "message", {"person": "200", "mid": "uncertain", "text": "Да", "timestamp": time.time()-2})
        self.gateway.finish("message:uncertain", "unknown")
        self.gateway.tick()
        self.assertIn(("message:actual-consent", "skipped", "delivery_requires_review"), self.rows())
        self.assertFalse(any(c.args[0] == "POST" for c in self.gateway.graph.call_args_list))

    def test_sync_does_not_read_contacts_outside_window_or_after_optout_or_human_reply(self):
        self.sync_fixture([])
        for fields in ({"opened_at": time.time()-86401}, {"opened_at": time.time()+100},
                       {"opened_at": time.time()-10, "opted_out": 1},
                       {"opted_out": 0, "human_until": time.time()+100}):
            self.gateway.update_person("200", **fields)
            self.gateway.sync_messages()
        self.gateway.graph.assert_not_called()

    def test_sync_meta_read_error_does_not_send_and_is_rate_limited(self):
        self.sync_fixture([])
        self.gateway.graph.side_effect = GraphError(code=403)
        self.gateway.tick()
        self.gateway.tick()
        self.assertEqual(self.gateway.graph.call_count, 1)
        self.assertEqual(self.gateway.sync_status["last_error"], "meta_403")
        self.assertEqual(self.rows(), [])

    def test_sync_global_limit_is_one_contact_per_tick(self):
        self.sync_fixture([])
        self.gateway.update_person("201", opened_at=time.time()-20)
        self.gateway.sync_messages()
        first = self.gateway.graph.call_count
        self.gateway.sync_messages()
        self.assertEqual(first, 3)
        self.assertEqual(self.gateway.graph.call_count, first)

    def test_sync_pending_link_is_due_after_five_seconds_and_precedes_completed_chat(self):
        self.sync_fixture([self.api_message()])
        self.gateway.update_person("201", opened_at=time.time()-20, link_sent_at=time.time()-10)
        with sqlite3.connect(self.gateway.database) as con:
            con.executemany("INSERT INTO message_sync(person,checked_at) VALUES(?,?)",
                            [("200",time.time()-6),("201",time.time()-100)])
        self.gateway.sync_messages()
        self.assertIn("recipient=200", self.gateway.graph.call_args_list[0].args[1])
        self.assertIn(("message:actual-consent", "pending", ""), self.rows())

    @staticmethod
    def api_comment(comment="400", person="200", text="Креатив", stamp=None, hidden=False):
        from datetime import datetime, timezone
        return {"id":comment,"from":{"id":person},"text":text,"hidden":hidden,
                "timestamp":datetime.fromtimestamp(stamp or time.time()-1,timezone.utc).isoformat()}

    def comment_sync_fixture(self, comments):
        del self.gateway.sync_comments
        (self.root/"secrets"/"credentials.json").write_text(json.dumps(dict(self.credentials,api_mode="business_system_user")))
        with sqlite3.connect(self.gateway.database) as con:
            con.execute("UPDATE comment_poll SET since=?,inventory_at=0,cursor=''", (time.time()-60,))
        def graph(method,endpoint,payload=None):
            if method=="GET" and "/media?" in endpoint:
                return {"data":[{"id":"900","comments_count":len(comments)}]}
            if method=="GET" and "/comments?" in endpoint:
                return {"data":comments}
            if method=="GET" and "/thread_owner?" in endpoint:
                return {"data":[]}
            return self.graph(method,endpoint,payload)
        self.gateway.graph.side_effect=graph

    def test_comment_sync_starts_new_keyword_flow_once_without_webhook(self):
        self.comment_sync_fixture([self.api_comment(),self.api_comment(comment="401",stamp=time.time()-3600),
            self.api_comment(comment="402",person="100"),self.api_comment(comment="403",hidden=True),
            self.api_comment(comment="404",text="Видео")])
        self.gateway.tick()
        self.assertIn(("comment:400","sent",""),self.rows())
        self.assertEqual(self.gateway.comments_status["queued"],1)
        self.assertEqual(self.gateway.last_ingress,0)
        self.comment(comment="400")
        self.gateway.tick()
        self.assertEqual(sum(c.args[:2]==("POST","100/messages") for c in self.gateway.graph.call_args_list),1)
        self.assertFalse(any(row[0] in {"comment:401","comment:402","comment:403","comment:404"} for row in self.rows()))

    def test_comment_sync_pause_advances_cutoff_and_does_not_backfill_paused_comments(self):
        comments=[self.api_comment(stamp=time.time()-5)]
        self.comment_sync_fixture(comments)
        settings=campaign.load_settings(self.path)
        settings["enabled"]=False
        campaign.save_settings(settings,self.path)
        self.gateway.ready=Mock(return_value=False)
        self.gateway.tick()
        self.gateway.graph.assert_not_called()
        settings["enabled"]=True
        campaign.save_settings(settings,self.path)
        self.gateway.ready=Mock(return_value=True)
        self.gateway.tick()
        self.assertEqual(self.rows(),[])
        comments[:]=[self.api_comment(comment="405",stamp=time.time())]
        self.gateway.last_comment_sync=0
        self.gateway.sync_comments(settings)
        self.assertIn(("comment:405","pending",""),self.rows())

    def test_comment_sync_preserves_watermark_until_all_comment_pages_are_read(self):
        self.comment_sync_fixture([])
        first={"data":[self.api_comment(stamp=time.time()-3600)],"paging":{
            "next":"https://untrusted.invalid/not-followed","cursors":{"after":"next-comments"}}}
        def graph(method,endpoint,payload=None):
            if "/media?" in endpoint:
                return {"data":[{"id":"900","comments_count":70}]}
            if "/comments?" in endpoint:
                return {"data":[self.api_comment()]} if "after=next-comments" in endpoint else first
            return self.graph(method,endpoint,payload)
        self.gateway.graph.side_effect=graph
        self.gateway.sync_comments(campaign.load_settings(self.path))
        with sqlite3.connect(self.gateway.database) as con:
            self.assertEqual(con.execute("SELECT watermark,cursor FROM comment_media").fetchone(),(0,"next-comments"))
        self.gateway.last_comment_sync=0
        self.gateway.sync_comments(campaign.load_settings(self.path))
        self.assertIn(("comment:400","pending",""),self.rows())
        self.assertTrue(self.gateway.graph.call_args.args[1].startswith("900/comments?"))
        with sqlite3.connect(self.gateway.database) as con:
            watermark,cursor=con.execute("SELECT watermark,cursor FROM comment_media").fetchone()
        self.assertGreater(watermark,0)
        self.assertEqual(cursor,"")

    def test_comment_sync_paginates_media_and_rotates_old_and_new_posts(self):
        self.comment_sync_fixture([])
        checked=[]
        def graph(method,endpoint,payload=None):
            if "/media?" in endpoint:
                if "after=next-media" in endpoint:
                    return {"data":[{"id":"901","comments_count":1}]}
                return {"data":[{"id":"900","comments_count":1}],"paging":{
                    "next":"https://untrusted.invalid/ignored","cursors":{"after":"next-media"}}}
            checked.append(endpoint.split("/",1)[0])
            return {"data":[]}
        self.gateway.graph.side_effect=graph
        self.gateway.sync_comments(campaign.load_settings(self.path))
        self.gateway.last_comment_sync=0
        self.gateway.sync_comments(campaign.load_settings(self.path))
        self.assertEqual(checked,["900","901"])
        with sqlite3.connect(self.gateway.database) as con:
            self.assertEqual(con.execute("SELECT count(*) FROM comment_media").fetchone()[0],2)

    def test_comment_sync_rate_limit_waits_and_malformed_cursor_never_follows_next_url(self):
        self.comment_sync_fixture([])
        self.gateway.graph.side_effect=GraphError(code=429)
        self.gateway.sync_comments(campaign.load_settings(self.path))
        self.gateway.sync_comments(campaign.load_settings(self.path))
        self.assertEqual(self.gateway.graph.call_count,1)
        self.assertEqual(self.gateway.comments_status["last_error"],"meta_429")
        self.assertEqual(self.rows(),[])
        with self.assertRaises(ValueError):
            Gateway.next_cursor({"paging":{"next":"https://untrusted.invalid/ignored"}})

    def test_comment_sync_keyword_change_does_not_replay_already_scanned_old_comments(self):
        self.comment_sync_fixture([self.api_comment(text="Видео",stamp=time.time()-5)])
        settings=campaign.load_settings(self.path)
        self.gateway.sync_comments(settings)
        settings["keywords"]=["видео"]
        self.gateway.last_comment_sync=0
        self.gateway.sync_comments(settings)
        self.assertEqual(self.rows(),[])

    def test_graph_recognizes_meta_rate_limit_code_four_even_when_http_is_403(self):
        error=HTTPError('https://graph.instagram.com/test',403,'rate limited',
                        {'Retry-After':'60'},BytesIO(b'{"error":{"code":4,"is_transient":true}}'))
        with patch('instagram_gateway.urlopen',side_effect=error):
            with self.assertRaises(GraphError) as raised:
                Gateway(self.root,self.path).graph('GET','100/messages')
        self.assertTrue(raised.exception.rate_limited)
        self.assertEqual(raised.exception.retry_after,60)
        self.assertFalse(raised.exception.uncertain)

    def test_sync_rate_limit_backoff_survives_restart(self):
        self.sync_fixture([])
        self.gateway.graph.side_effect=GraphError(code=403,api_code=4,retry_after=60)
        self.gateway.sync_messages()
        self.assertGreater(self.gateway.backoff_until('messages'),time.time())
        fresh=Gateway(self.root,self.path)
        fresh.graph=Mock()
        fresh.sync_messages()
        fresh.graph.assert_not_called()

    def test_definite_send_rate_limit_keeps_event_pending_until_backoff_then_sends_once(self):
        self.comment()
        def limited(method,endpoint,payload=None):
            if method=='POST':
                raise GraphError(code=403,api_code=4,retry_after=60)
            return self.graph(method,endpoint,payload)
        self.gateway.graph.side_effect=limited
        self.gateway.tick()
        self.assertIn(('comment:300','pending','meta_rate_limit'),self.rows())
        calls=self.gateway.graph.call_count
        self.gateway.tick()
        self.assertEqual(self.gateway.graph.call_count,calls)
        with sqlite3.connect(self.gateway.database) as con:
            con.execute('UPDATE api_backoff SET until_at=0')
        self.gateway.graph=Mock(side_effect=self.graph)
        self.gateway.tick()
        self.assertIn(('comment:300','sent',''),self.rows())
        self.assertEqual(sum(c.args[:2]==('POST','100/messages') for c in self.gateway.graph.call_args_list),1)

    def test_graph_hosts_match_token_family_and_unknown_mode_never_sends_credentials(self):
        for mode, host in (("instagram_login", "graph.instagram.com"), ("unified_login", "graph.facebook.com"),
                           ("business_system_user", "graph.facebook.com")):
            with self.subTest(mode=mode), patch("instagram_gateway.urlopen") as opened:
                credentials = dict(self.credentials, api_mode=mode)
                (self.root / "secrets" / "credentials.json").write_text(json.dumps(credentials))
                opened.return_value.__enter__.return_value.read.return_value = b'{}'
                Gateway(self.root, self.path).graph("GET", "100?fields=id")
                request = opened.call_args.args[0]
                self.assertEqual(request.full_url, "https://" + host + "/v24.0/100?fields=id")
                self.assertEqual(request.get_header("Authorization"), "Bearer test-only")
        (self.root / "secrets" / "credentials.json").write_text(json.dumps(dict(self.credentials, api_mode="unknown")))
        with patch("instagram_gateway.urlopen") as opened:
            with self.assertRaises(GraphError):
                Gateway(self.root, self.path).graph("GET", "100")
            opened.assert_not_called()

    def test_app_auth_is_restricted_to_own_business_token_debug(self):
        credentials = dict(self.credentials, api_mode="business_system_user")
        (self.root / "secrets" / "credentials.json").write_text(json.dumps(credentials))
        with patch("instagram_gateway.urlopen") as opened:
            opened.return_value.__enter__.return_value.read.return_value = b'{"data": {}}'
            Gateway(self.root, self.path).graph("GET", "debug_token?input_token=test-only", app_auth=True)
            request = opened.call_args.args[0]
            self.assertEqual(request.full_url, "https://graph.facebook.com/v24.0/debug_token?input_token=test-only")
            self.assertEqual(request.get_header("Authorization"), "Bearer 101|test-secret")
        for mode, method, endpoint in (
            ("instagram_login", "GET", "debug_token?input_token=test-only"),
            ("unified_login", "GET", "debug_token?input_token=test-only"),
            ("unknown", "GET", "debug_token?input_token=test-only"),
            ("business_system_user", "GET", "me"),
            ("business_system_user", "POST", "debug_token?input_token=test-only"),
        ):
            with self.subTest(mode=mode, method=method, endpoint=endpoint):
                (self.root / "secrets" / "credentials.json").write_text(json.dumps(dict(self.credentials, api_mode=mode)))
                with patch("instagram_gateway.urlopen") as opened:
                    with self.assertRaises(GraphError):
                        Gateway(self.root, self.path).graph(method, endpoint, app_auth=True)
                    opened.assert_not_called()

    def test_own_business_token_binds_app_principal_account_and_subscription_fields(self):
        credentials = dict(self.credentials, api_mode="business_system_user", system_user_id="500")
        (self.root / "secrets" / "credentials.json").write_text(json.dumps(credentials))
        token_info = {"is_valid": True, "type": "SYSTEM_USER", "app_id": "101", "user_id": "500",
                      "scopes": ["instagram_basic", "instagram_manage_comments", "instagram_manage_messages",
                                 "pages_read_engagement", "pages_show_list"]}
        responses = [{"data": token_info}, {"id": "100", "username": "biomozg"},
                     {"data": [{"subscribed_fields": ["comments", "messages", "messaging_postbacks"]}]}]
        connected = Gateway(self.root, self.path)
        connected.graph = Mock(side_effect=copy.deepcopy(responses))
        self.assertTrue(connected.ready())
        self.assertEqual(connected.graph.call_args_list[0].kwargs, {"app_auth": True})
        for key, value in (("is_valid", False), ("type", "USER"), ("app_id", "999"),
                           ("user_id", "999"), ("scopes", ["instagram_basic"])):
            with self.subTest(token_field=key):
                bad = copy.deepcopy(responses)
                bad[0]["data"][key] = value
                disconnected = Gateway(self.root, self.path)
                disconnected.graph = Mock(side_effect=bad)
                self.assertFalse(disconnected.ready())
                self.assertEqual(disconnected.graph.call_count, 1)
        for label, index, replacement in (
            ("other Instagram ID", 1, {"id": "999", "username": "biomozg"}),
            ("other Instagram name", 1, {"id": "100", "username": "another_account"}),
            ("other subscription app", 2, {"data": [{"id": "999", "subscribed_fields": ["comments", "messages", "messaging_postbacks"]}]}),
            ("missing private messages", 2, {"data": [{"subscribed_fields": ["comments"]}]}),
        ):
            with self.subTest(label=label):
                bad = copy.deepcopy(responses)
                bad[index] = replacement
                disconnected = Gateway(self.root, self.path)
                disconnected.graph = Mock(side_effect=bad)
                self.assertFalse(disconnected.ready())

    def test_own_business_mode_requires_an_expected_app_scoped_principal(self):
        for principal in (None, "", "not-an-id"):
            with self.subTest(principal=principal):
                credentials = dict(self.credentials, api_mode="business_system_user")
                if principal is not None:
                    credentials["system_user_id"] = principal
                (self.root / "secrets" / "credentials.json").write_text(json.dumps(credentials))
                disconnected = Gateway(self.root, self.path)
                disconnected.graph = Mock()
                self.assertFalse(disconnected.ready())
                disconnected.graph.assert_not_called()

    def test_unified_token_requires_our_business_our_assigned_instagram_and_all_subscriptions(self):
        credentials = dict(self.credentials, api_mode="unified_login", client_business_id="151")
        (self.root / "secrets" / "credentials.json").write_text(json.dumps(credentials))
        responses = [{"id": "system-user", "client_business_id": "151"},
                     {"data": [{"id": "100", "username": "biomozg"}]},
                     {"data": [{"id": "101", "subscribed_fields": ["comments", "messages", "messaging_postbacks"]}]}]
        connected = Gateway(self.root, self.path)
        connected.graph = Mock(side_effect=copy.deepcopy(responses))
        self.assertTrue(connected.ready())
        for label, index, replacement in (
            ("other business", 0, {"client_business_id": "999"}),
            ("unassigned Instagram", 1, {"data": [{"id": "999", "username": "biomozg"}]}),
            ("different username", 1, {"data": [{"id": "100", "username": "another_account"}]}),
            ("other app", 2, {"data": [{"id": "999", "subscribed_fields": ["comments", "messages", "messaging_postbacks"]}]}),
            ("missing message events", 2, {"data": [{"id": "101", "subscribed_fields": ["comments"]}]}),
        ):
            with self.subTest(label=label):
                bad = copy.deepcopy(responses)
                bad[index] = replacement
                disconnected = Gateway(self.root, self.path)
                disconnected.graph = Mock(side_effect=bad)
                self.assertFalse(disconnected.ready())
        credentials.pop("client_business_id")
        (self.root / "secrets" / "credentials.json").write_text(json.dumps(credentials))
        disconnected = Gateway(self.root, self.path)
        disconnected.graph = Mock()
        self.assertFalse(disconnected.ready())
        disconnected.graph.assert_not_called()

    def test_existing_instagram_login_token_still_checks_instagram_identity(self):
        connected = Gateway(self.root, self.path)
        connected.graph = Mock(side_effect=[{"user_id": "100", "username": "biomozg"},
            {"data": [{"id": "101", "subscribed_fields": ["comments", "messages", "messaging_postbacks"]}]}])
        self.assertTrue(connected.ready())

    def test_hmac_and_wrong_account_are_rejected(self):
        body = b'{"object":"instagram"}'
        signature = "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
        self.assertTrue(self.gateway.verify_signature(body, signature))
        self.assertFalse(self.gateway.verify_signature(body + b" ", signature))
        self.assertFalse(self.gateway.verify_signature(body, ""))
        self.gateway.ingest({"object": "instagram", "entry": [{"id": "999", "changes": [{"field": "comments", "value": {"id": "300"}}]}]})
        self.assertEqual(self.rows(), [])

    def test_comments_on_any_post_open_dm_then_consent_uses_latest_link(self):
        self.comment()
        self.gateway.tick()
        sends = [call for call in self.gateway.graph.call_args_list if call.args[0] == "POST"]
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0].args[2]["recipient"], {"comment_id": "300"})
        self.assertIn("quick_replies", sends[0].args[2]["message"])
        settings = campaign.load_settings(self.path)
        settings["target_url"] = "https://t.me/BioMozg_agent?start=new"
        campaign.save_settings(settings, self.path)
        self.message()
        self.gateway.tick()
        link_calls = [call for call in self.gateway.graph.call_args_list if call.args[0] == "POST" and "attachment" in (call.args[2] or {}).get("message", {})]
        self.assertEqual(len(link_calls), 1)
        self.assertEqual(link_calls[0].args[2]["message"]["attachment"]["payload"]["buttons"][0]["url"], settings["target_url"])
        self.comment()
        self.message()
        count = self.gateway.graph.call_count
        self.gateway.tick()
        self.assertEqual(self.gateway.graph.call_count, count)

    def test_v26_change_message_delivers_link_once_across_both_envelopes(self):
        self.comment()
        self.gateway.tick()
        self.gateway.graph.reset_mock()
        value = {"sender": {"id": "200"}, "recipient": {"id": "100"},
                 "timestamp": time.time() * 1000,
                 "message": {"mid": "v26-opt-in", "text": "Да, хочу видео",
                             "quick_reply": {"payload": campaign.OPT_IN}}}
        current = {"object": "instagram", "entry": [{"id": "100", "changes": [
            {"field": "messages", "value": value}]}]}
        self.gateway.ingest(current)
        self.assertIn(("message:v26-opt-in", "pending", ""), self.rows())
        # The same message may be delivered twice or through both supported envelopes.
        self.gateway.ingest(current)
        self.gateway.ingest({"object": "instagram", "entry": [{"id": "100", "messaging": [value]}]})
        settings = campaign.load_settings(self.path)
        settings["target_url"] = "https://t.me/BioMozg_agent?start=changed"
        campaign.save_settings(settings, self.path)
        self.gateway.tick()
        sends = [call for call in self.gateway.graph.call_args_list
                 if call.args[:2] == ("POST", "100/messages")]
        self.assertEqual(len(sends), 1)
        button = sends[0].args[2]["message"]["attachment"]["payload"]["buttons"][0]
        self.assertEqual(button["url"], settings["target_url"])
        self.assertEqual(button["title"], settings["link_label"])
        self.assertGreater(self.gateway.conversation("200")["link_sent_at"], 0)
        self.assertIn(("message:v26-opt-in", "sent", ""), self.rows())

    def test_v26_change_postback_continues_an_opened_conversation(self):
        self.comment()
        self.gateway.tick()
        self.gateway.ingest({"object": "instagram", "entry": [{"id": "100", "changes": [{
            "field": "messaging_postbacks", "value": {
                "sender": {"id": "200"}, "recipient": {"id": "100"},
                "timestamp": time.time() * 1000,
                "postback": {"mid": "v26-postback", "payload": campaign.OPT_IN},
            }}]}]})
        self.gateway.tick()
        self.assertIn(("message:v26-postback", "sent", ""), self.rows())
        self.assertGreater(self.gateway.conversation("200")["link_sent_at"], 0)

    def test_v26_other_account_and_standby_do_not_enter_the_send_queue(self):
        value = {"sender": {"id": "200"}, "recipient": {"id": "100"},
                 "timestamp": time.time() * 1000,
                 "message": {"mid": "foreign-or-standby", "text": "креатив"}}
        self.gateway.ingest({"object": "instagram", "entry": [{"id": "999", "changes": [
            {"field": "messages", "value": value}]}]})
        self.gateway.ingest({"object": "instagram", "entry": [{"id": "100", "changes": [
            {"field": "standby", "value": value}], "standby": [value]}]})
        self.gateway.tick()
        self.assertEqual(self.rows(), [])
        self.gateway.graph.assert_not_called()

    def test_no_consent_no_keyword_url_click_and_old_events_do_not_send(self):
        self.message(text="Привет", payload="")
        self.comment(comment="301", text="видео")
        self.comment(comment="302", stamp=time.time() - 8 * 86400)
        self.gateway.tick()
        self.assertEqual(self.gateway.graph.call_count, 0)
        self.message(mid="old", text="креатив", payload="", stamp=time.time() - 25 * 3600)
        self.gateway.tick()
        self.assertEqual(self.gateway.graph.call_count, 0)

    def test_pause_applies_to_queued_messages(self):
        self.comment()
        settings = campaign.load_settings(self.path)
        settings["enabled"] = False
        campaign.save_settings(settings, self.path)
        self.gateway.tick()
        self.gateway.graph.assert_not_called()

    def test_keyword_removal_while_comment_is_checked_stops_the_send(self):
        self.comment()
        def changed(method, endpoint, payload=None):
            result = self.graph(method, endpoint, payload)
            settings = campaign.load_settings(self.path)
            settings["keywords"] = ["новый урок"]
            campaign.save_settings(settings, self.path)
            return result
        self.gateway.graph.side_effect = changed
        self.gateway.tick()
        self.assertEqual(self.gateway.graph.call_count, 1)
        self.assertEqual(self.rows()[0][2], "keyword_removed")

    def test_millisecond_comment_timestamp_is_supported(self):
        self.comment(stamp=time.time() * 1000)
        self.gateway.tick()
        self.assertEqual(self.rows()[0][1], "sent")

    def test_optout_human_takeover_cooldown_and_own_comment(self):
        self.message(text="Стоп", payload="")
        self.gateway.tick()
        self.comment()
        self.gateway.tick()
        self.gateway.graph.assert_not_called()
        self.gateway.update_person("201", human_until=time.time() + 86400)
        self.comment(comment="301", person="201")
        self.gateway.update_person("202", opened_at=time.time())
        self.comment(comment="302", person="202")
        self.comment(comment="303", person="100")
        self.gateway.tick()
        self.gateway.graph.assert_not_called()

    def test_ambiguous_send_is_not_retried_and_has_no_public_reply(self):
        self.comment()
        real_graph = self.graph
        self.gateway.graph.side_effect = lambda method, endpoint, payload=None: real_graph(method, endpoint, payload) if method == "GET" else (_ for _ in ()).throw(GraphError(uncertain=True))
        self.gateway.tick()
        self.assertEqual(self.rows(), [("comment:300", "unknown", "meta_0")])
        self.comment()
        count = self.gateway.graph.call_count
        self.gateway.tick()
        self.assertEqual(self.gateway.graph.call_count, count)

    def test_restart_quarantines_interrupted_send_and_missing_meta_never_sends(self):
        self.comment()
        self.gateway.finish("comment:300", "sending")
        fresh = Gateway(self.root, self.path)
        self.assertEqual(self.rows()[0][1], "unknown")
        (self.root / "secrets" / "credentials.json").write_text("{}")
        fresh.graph = Mock()
        status = fresh.tick()
        self.assertFalse(status["connected"])
        fresh.graph.assert_not_called()


if __name__ == "__main__":
    unittest.main()
