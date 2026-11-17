import sqlite3,unittest
from types import SimpleNamespace
from bot.services.publish import _attempts_today
from bot.timeutil import FrozenClock,to_iso
class QuotaRecoveryTests(unittest.TestCase):
 def test_only_explicitly_reconciled_failure_releases_slot(self):
  c=sqlite3.connect(':memory:');c.execute('create table approvals(id integer,status text)');c.execute('create table publish_attempts(approval_id integer,state text,created_at text)')
  clock=FrozenClock();cfg=SimpleNamespace(timezone_name='Asia/Shanghai')
  states=[('failed','reconciled_not_published'),('failed','failed'),('unknown','publish_unknown'),('publishing','publishing'),('published','published')]
  for i,(attempt,approval) in enumerate(states):
   c.execute('insert into approvals values (?,?)',(i,approval));c.execute('insert into publish_attempts values (?,?,?)',(i,attempt,to_iso(clock.now())))
  self.assertEqual(_attempts_today(None,cfg,clock,conn=c),4)
  c.execute("update publish_attempts set state='unknown' where approval_id=0")
  self.assertEqual(_attempts_today(None,cfg,clock,conn=c),5)
