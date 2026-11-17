"""Ad-hoc local smoke check.  Not part of the product CLI."""

# =============================================================================
# 中文导读：开发时手动跑一遍全链路的冒烟脚本
# =============================================================================
# 它不是产品命令，只是给开发者用的“手动验收”：
# 采集 -> 研究队列 -> 写稿 -> 渲染 -> 通知 -> 批准 -> 模拟发布 -> 重复批准。
# 运行前请确认配置是 offline/mock，否则可能真的去调用外部服务。
# 每一步都 print，方便肉眼确认哪一环出了问题。
# =============================================================================

from pathlib import Path

from PIL import Image

from bot.adapters.llm import build_llm
from bot.adapters.notify import build_notifier
from bot.budget import BudgetGuard
from bot.config import load_config
from bot.db import Database
from bot.httpclient import HttpClient
from bot.security import SSRFGuard
from bot.services.approvals import handle_inbound_message
from bot.services.drafts import create_draft, draft_view
from bot.services.notifications import deliver_pending
from bot.services.publish import publish_approval
from bot.services.research import ensure_research_queue, ingest_source, sync_sources
from bot.timeutil import FrozenClock


def main() -> None:
    # 用脚本所在位置推项目根目录，保证从任何工作目录运行都能找到配置。
    root = Path(__file__).resolve().parent.parent
    config = load_config(root=root)
    db = Database(config.db_path)
    db.init()
    # 冻结时钟：时间不会自己走，结果可复现。
    clock = FrozenClock()
    print("sync", sync_sources(db, config))
    # 冒烟测试里也用真正的 SSRFGuard，确保网络层不是“测试专用简化版”。
    http = HttpClient(guard=SSRFGuard(), user_agent="BotOperator/0.1-dev")
    for source in db.fetchall("SELECT * FROM sources WHERE enabled=1 ORDER BY id"):
        # 逐个来源采集。offline 配置下这里是本地 fixture，不会联网。
        print("ingest", ingest_source(db, config, http, clock, source))
    # 研究队列会挑出值得写的选题；这里只打印标题确认数量。
    print("queue", [item["title"] for item in ensure_research_queue(db, config, clock)])
    # 写稿仍然走真实的 create_draft 和预算守卫，避免测试路径与生产分叉。
    llm = build_llm(config, clock, BudgetGuard(db, config, clock))
    created = create_draft(db, config, clock, llm)
    draft_id = created["draft"]["id"]
    code = created["approval_code"]
    view = draft_view(db, draft_id)
    print("draft", draft_id, view["status"], code)
    print(view["body"])
    for image in view["images"]:
        # 用 Pillow 的 verify() 真正解码一次，确认生成的是完整 PNG，
        # 而不是只有扩展名对、内容损坏的文件。
        with Image.open(image["path"]) as opened:
            opened.verify()
        print("image", image["path"], image["sha256"][:12], "OK")

    notifier = build_notifier(config, clock)
    # 投递队列里的审批通知。offline 下只是写本地 JSON。
    print("notify", deliver_pending(db, config, clock, notifier))
    # 用 owner 身份批准；message_id 固定，方便重复运行时观察幂等行为。
    approval = handle_inbound_message(
        db,
        config,
        clock,
        message_id="smoke-approve-1",
        sender_id="demo-owner",
        chat_id="demo-owner",
        chat_type="p2p",
        text=f"批准 {code}",
        llm=llm,
    )
    print("approval", approval)
    # 直接调用发布闸门。offline/mock 下应得到 simulated。
    result = publish_approval(db, config, clock, created["approval_id"])
    print("publish", result)
    attempt = db.fetchone("SELECT * FROM publish_attempts WHERE approval_id=?", (created["approval_id"],))
    print("attempt", dict(attempt) if attempt else None)
    # 换一个 message_id 再批准一次：审批已经处理过，应该被拒绝/忽略，
    # 发布尝试数量仍然只有一条。
    duplicate = handle_inbound_message(
        db,
        config,
        clock,
        message_id="smoke-approve-2",
        sender_id="demo-owner",
        chat_id="demo-owner",
        chat_type="p2p",
        text=f"批准 {code}",
        llm=llm,
    )
    print("duplicate", duplicate)
    print("attempts", db.scalar("SELECT COUNT(*) FROM publish_attempts"))


if __name__ == "__main__":
    main()
