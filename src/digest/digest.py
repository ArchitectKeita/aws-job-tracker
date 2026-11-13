"""
job-tracker digest -- runs on a schedule.

  {"mode": "daily"}   email a follow-up reminder for anything still sitting at
                      "applied" for STALE_DAYS+ days (each at most once a week)
  {"mode": "weekly"}  email the week's numbers: sent, responses, response rate
"""
import os
from datetime import datetime, timedelta, timezone

import boto3

TABLE = boto3.resource("dynamodb").Table(os.environ.get("TABLE_NAME", "job-tracker-apps"))
sns = boto3.client("sns")
TOPIC_ARN = os.environ.get("TOPIC_ARN", "")
APP_URL = os.environ.get("APP_URL", "")
STALE_DAYS = int(os.environ.get("STALE_DAYS", "7"))

RESPONDED = {"screen", "interview", "offer", "rejected"}

if not TOPIC_ARN:
    print("CONFIG ERROR: TOPIC_ARN is not set. Digests will be built but NOT emailed.")


def _ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def all_apps():
    items, kwargs = [], {}
    while True:
        page = TABLE.scan(**kwargs)
        items += page.get("Items", [])
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def send(subject, body):
    if not TOPIC_ARN:
        print(f"ERROR: digest '{subject}' NOT SENT (TOPIC_ARN is not set)")
        return "NOT SENT - no topic configured"
    sns.publish(TopicArn=TOPIC_ARN, Subject=subject[:100], Message=body)
    return "sent"


def lambda_handler(event, context):
    mode = (event or {}).get("mode", "daily")
    now = (_ts(event["now"]) if event and event.get("now") else datetime.now(timezone.utc))
    apps = all_apps()
    result = daily(apps, now) if mode == "daily" else weekly(apps, now)
    print(result)
    return result


def daily(apps, now):
    cutoff = now - timedelta(days=STALE_DAYS)
    due = []
    for a in apps:
        if a.get("status") != "applied" or _ts(a["updated_at"]) > cutoff:
            continue
        last = a.get("last_reminded")
        if last and _ts(last) > cutoff:
            continue  # already nudged about this one this week
        due.append(a)

    if not due:
        return {"mode": "daily", "stale": 0, "email": "nothing due"}

    due.sort(key=lambda a: a["updated_at"])
    lines = [f"{len(due)} application{'s' if len(due) != 1 else ''} with no movement in {STALE_DAYS}+ days.",
             "A short follow-up email to the recruiter or hiring manager is worth sending.", ""]
    for a in due:
        days = (now - _ts(a["updated_at"])).days
        lines.append(f"  - {a['company']} — {a['role']}  ({days} days, applied {a.get('applied_on', '?')})")
        if a.get("link"):
            lines.append(f"    {a['link']}")
    if APP_URL:
        lines += ["", f"Update them: {APP_URL}"]

    status = send(f"Follow up: {len(due)} application{'s' if len(due) != 1 else ''} gone quiet", "\n".join(lines))
    if status == "sent":
        stamp = now.isoformat(timespec="seconds")
        for a in due:
            TABLE.update_item(Key={"app_id": a["app_id"]},
                              UpdateExpression="SET last_reminded = :t",
                              ExpressionAttributeValues={":t": stamp})
    return {"mode": "daily", "stale": len(due), "email": status}


def weekly(apps, now):
    week_ago = now - timedelta(days=7)
    sent = [a for a in apps if _ts(a["created_at"]) > week_ago]

    def responded(a):
        return any(h["status"] in RESPONDED for h in a.get("history", []))

    total = len(apps)
    responses = sum(1 for a in apps if responded(a))
    rate = round(100 * responses / total) if total else 0
    moved = [a for a in apps
             if any(_ts(h["at"]) > week_ago and h["status"] in RESPONDED for h in a.get("history", []))]
    by_status = {}
    for a in apps:
        by_status[a["status"]] = by_status.get(a["status"], 0) + 1

    lines = [
        f"This week: {len(sent)} sent · {len(moved)} moved forward",
        f"All time:  {total} application{'s' if total != 1 else ''} · {responses} response{'s' if responses != 1 else ''} · {rate}% response rate",
        "",
        "Pipeline: " + "  ".join(f"{k} {v}" for k, v in sorted(by_status.items())),
    ]
    if moved:
        lines += ["", "Moved this week:"] + [f"  - {a['company']} — {a['role']} → {a['status']}" for a in moved]
    if APP_URL:
        lines += ["", APP_URL]

    status = send(f"Job search week: {len(sent)} sent, {len(moved)} moved, {rate}% response rate", "\n".join(lines))
    return {"mode": "weekly", "sent_this_week": len(sent), "moved_this_week": len(moved),
            "total": total, "responses": responses, "rate": rate, "email": status}
