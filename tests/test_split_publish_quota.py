import sqlite3,unittest
from types import SimpleNamespace
from bot.services.publish import _attempts_today
from bot.timeutil import FrozenClock,to_iso
class SplitQuotaTests(unittest.TestCase):
 def test_manual_and_automatic_are_independent_across_revisions(self):
  c=sqlite3.connect(':memory:')
  c.executescript('create table approvals(id integer,status text);create table drafts(id integer,event_id integer);create table topic_requests(event_id integer);create table publish_attempts(approval_id integer,draft_id integer,state text,created_at text);')
  clock=FrozenClock();cfg=SimpleNamespace(timezone_name='Asia/Shanghai');now=to_iso(clock.now())
  c.execute('insert into topic_requests values (10)')
  for i in range(1,7):
   c.execute('insert into drafts values (?,?)',(i,10 if i<=5 else 20));c.execute("insert into approvals values (?,'published')",(i,));c.execute("insert into publish_attempts values (?,?,'published',?)",(i,i,now))
  self.assertEqual(_attempts_today(None,cfg,clock,conn=c,manual=True),5)
  self.assertEqual(_attempts_today(None,cfg,clock,conn=c,manual=False),1)
  c.execute("update publish_attempts set state='unknown' where approval_id=1")
  self.assertEqual(_attempts_today(None,cfg,clock,conn=c,manual=True),5)
  c.execute("update approvals set status='reconciled_not_published' where id=1")
  c.execute("update publish_attempts set state='failed' where approval_id=1")
  self.assertEqual(_attempts_today(None,cfg,clock,conn=c,manual=True),4)
