"""One explicitly requested, unpublished live drafting check. No background loop."""
from __future__ import annotations
import argparse, dataclasses, json, pathlib, sys
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true", help="Make one paid DeepSeek draft request")
    args = ap.parse_args()
    if not args.run:
        print("Use --run to request one real draft. Never publishes or sends notifications.")
        return 0
    from bot.config import load_config
    from bot.db import Database
    from bot.timeutil import SystemClock
    from bot.budget import BudgetGuard
    from bot.adapters.llm import DeepSeekLLM
    from bot.content import validate_draft_payload
    from bot.render import render_draft
    out = ROOT / "var/live-verification"
    retrieved = json.loads((out / "official-fetch.json").read_text(encoding="utf-8"))
    source_text = retrieved["data"]["content"]
    if retrieved["url"] != "https://api-docs.deepseek.com/guides/search/" or len(source_text) < 200:
        raise ValueError("Expected a genuine cached official Fetch result")
    config = load_config(root=ROOT)
    config.data["llm"].update(provider="deepseek", model="deepseek-flash",
                            base_url="https://api.deepseek.com", max_retries=0,
                            timeout_seconds=60, thinking="disabled")
    config.data["publisher"]["live_enabled"] = False
    config.data["budget"]["daily_usd"] = 0.10
    config.data["writer"]["body_max_chars"] = 900
    db = Database(out / "draft-verification.sqlite3")
    db.init()
    clock = SystemClock()
    budget = BudgetGuard(db, config, clock)
    llm = DeepSeekLLM(config, clock, budget)
    evidence = {"id": "official-search-guide", "quote": source_text[:12000],
                "source_name": "DeepSeek 官方文档", "url": retrieved["url"],
                "published_at": None, "date_precision": "unknown",
                "fetched_at": retrieved["fetched_at"], "vendor_claim": False}
    payload = {
        "event": {"title": "用现有 DeepSeek Key 给运营助手接入搜索与网页取材", "date_unknown": True},
        "evidence": [evidence],
        "constraints": {"title_max_chars": 20, "body_max_chars": 900},
        "instruction": "这是功能解读样稿，不是今日新闻。title字段必须准确使用：给AI助手补上搜索能力。只写证据支持的能力和边界，不引用性能、排名或用户数，不编造实测。面向普通用户解释价值，不要粘贴接口地址、代码和请求头；正文四节合计约300个汉字。先搜索发现候选网页，再取正文让模型根据证据整理，指出有来源不等于内容就准确。分析要标为建议，未知发布时间要说明，不要声称文档没有价格或速率限制。所有事实带证据标识。"
    }
    result = llm.complete_json(purpose="draft", payload=payload, max_output_tokens=2400)
    (out / "raw-draft.json").write_text(json.dumps(result.data, ensure_ascii=False, indent=2), encoding="utf-8")
    content = validate_draft_payload(result.data, evidence_by_id={evidence["id"]: evidence},
             config=config, date_unknown=True, generator=result.provider, model=result.model)
    images = render_draft(content, out_dir=out / "images", config=config, draft_key="deepseek-live")
    (out / "draft.json").write_text(json.dumps(content.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "draft.md").write_text("# " + content.title + "\n\n未发布的真实模型样稿；来源页面发布时间未知，不能称为今日热点。\n\n" + content.render_body(), encoding="utf-8")
    report = {"status": "draft_only", "published": False, "notified": False,
              "model": result.model, "usage": dataclasses.asdict(result.usage),
              "latency_ms": result.latency_ms, "images": [x.as_dict() for x in images],
              "source": retrieved["url"], "source_date": "unknown",
              "note": "Search/Fetch API collected real public content separately; this verifies actual writer+validator+renderer, not the continuously running collector or a platform publication."}
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"title": content.title, "image_count": len(images), "usage": report["usage"], "draft": str(out / "draft.md"), "published": False}, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("Live verification stopped: " + type(exc).__name__ + ". No publication attempted.", file=sys.stderr)
        raise SystemExit(1)
