"""邮箱旁路反查（独立批处理，与主服务/主线流水线解耦）。

用法：
    .venv\\Scripts\\python.exe run_email_enrich.py --dry-run        # 先看会反查到什么、写回什么
    .venv\\Scripts\\python.exe run_email_enrich.py --limit 20       # 真正写回前 20 条
    .venv\\Scripts\\python.exe run_email_enrich.py --min-conf 70    # 只采纳 Hunter score >= 70 的邮箱

依赖：环境变量 HUNTER_API_KEY 或 data/config.json 里的 "hunter_api_key"。
"""
import argparse
import sys

from server import config, store
from server.email_enrich import find_email, is_missing


def main() -> int:
    ap = argparse.ArgumentParser(description="邮箱旁路反查（Hunter.io）")
    ap.add_argument("--limit", type=int, default=50, help="最多处理多少条")
    ap.add_argument("--min-conf", type=int, default=None, help="Hunter score 阈值")
    ap.add_argument("--dry-run", action="store_true", help="只演练不写库")
    args = ap.parse_args()

    min_conf = args.min_conf if args.min_conf is not None else config.EMAIL_ENRICH_MIN_CONF

    if not config.APOLLO_API_KEY and not config.HUNTER_API_KEY:
        print("未配置邮箱反查 key：请设置 APOLLO_API_KEY 或 HUNTER_API_KEY，")
        print("或在 data/config.json 里加 \"apollo_api_key\" 或 \"hunter_api_key\"。")
        return 2

    recs = store.list_records(conclusion="通过")
    todo = [r for r in recs if is_missing(r.get("email")) and (r.get("agent") or "").strip()]
    todo.sort(key=lambda r: r.get("id") or 0)
    print(f"通过记录 {len(recs)} 条；其中缺邮箱且有经纪人 {len(todo)} 条。")
    if not todo:
        return 0
    print(f"本次处理最多 {args.limit} 条（score >= {min_conf} 才写回）"
          + ("【dry-run，不写库】" if args.dry_run else "") + "\n")

    n_hit = n_miss = n_skip = 0
    for r in todo[: args.limit]:
        email, score, status = find_email(r.get("agent"))
        label = (r.get("agent") or "")[:34]
        if status == "hit" and score >= min_conf:
            n_hit += 1
            if not args.dry_run:
                store.update_record(r["id"], email=email)
            print(f"  ✓ #{r['id']:>4} {label:<36} -> {email}  (score={score})")
        elif status == "miss":
            n_miss += 1
            print(f"  ✗ #{r['id']:>4} {label:<36} 未找到可用邮箱")
        else:
            n_skip += 1
            print(f"  · #{r['id']:>4} {label:<36} 跳过（{status}）")

    print(f"\n完成：命中 {n_hit} / 未找到 {n_miss} / 跳过 {n_skip}"
          + ("【dry-run，未写库】" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
