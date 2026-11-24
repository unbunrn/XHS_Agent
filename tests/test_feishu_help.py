from unittest import TestCase
from tests.helpers import new_env, cleanup, approve
from bot.services.approvals import handle_inbound_message

class HelpTests(TestCase):
    def test_aliases_dedupe_and_no_business_jobs(self):
        e = new_env('help-menu')
        try:
            before = e.db.scalar('SELECT COUNT(*) FROM jobs')
            for i, text in enumerate(['帮助', '菜单', ' HELP ']):
                mid = f'help-{i}'
                result = approve(e, '', message_id=mid, text=text)
                self.assertEqual(result['outcome'], 'help')
                self.assertTrue(approve(e, '', message_id=mid, text=text)['duplicate'])
            self.assertEqual(e.db.scalar("SELECT COUNT(*) FROM notifications WHERE dedupe_key LIKE 'help:%'"), 3)
            self.assertEqual(e.db.scalar('SELECT COUNT(*) FROM jobs'), before)
            self.assertEqual(e.db.scalar('SELECT COUNT(*) FROM approvals'), 0)
        finally:
            cleanup(e)

    def test_owner_private_only_and_unknown_hint(self):
        e = new_env('help-auth')
        try:
            for mid, sender, chat in [('outsider','other','p2p'),('group','owner-open-id','group')]:
                r = handle_inbound_message(e.db,e.config,e.clock,message_id=mid,
                    sender_id=sender,chat_id='test',chat_type=chat,text='帮助')
                self.assertFalse(r['ok'])
            self.assertEqual(e.db.scalar('SELECT COUNT(*) FROM notifications'), 0)
            approve(e, '', text='这是什么命令')
            row=e.db.fetchone('SELECT payload_json FROM notifications LIMIT 1')
            self.assertIn('帮助',row['payload_json'])
        finally:
            cleanup(e)
