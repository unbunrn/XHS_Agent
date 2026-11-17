import unittest
from tests.helpers import new_env
from bot.services.research import sync_sources, _topic_source_name
class TopicSourceSyncTests(unittest.TestCase):
 def test_registered_source_survives_and_explicit_disable_is_preserved(self):
  e=new_env('topic-source-sync'); code='XT-TESTAA'
  with e.db.tx() as c:
   c.execute("INSERT INTO topic_requests(code,message_id,sender_id,raw_text,command,topic,instruction,day_key,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",(code,'m','owner','t','topic','t','','2026-11-09','now','now'))
   for k,name,url in [('valid',_topic_source_name(code),'deepseek-search://topic/'+code),('orphan','Topic Search orphan','deepseek-search://topic/XT-ORPHAN')]:
    c.execute("INSERT INTO sources(name,url,kind,trust_tier,enabled,created_at) VALUES (?,?,'deepseek_search',2,1,'now')",(name,url))
  sync_sources(e.db,e.config)
  self.assertEqual(e.db.scalar('select enabled from sources where name=?',(_topic_source_name(code),)),1)
  self.assertEqual(e.db.scalar("select enabled from sources where name='Topic Search orphan'"),0)
  e.db.execute('update sources set enabled=0 where name=?',(_topic_source_name(code),))
  sync_sources(e.db,e.config)
  self.assertEqual(e.db.scalar('select enabled from sources where name=?',(_topic_source_name(code),)),0)
