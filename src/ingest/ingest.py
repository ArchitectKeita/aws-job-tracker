"""
job-tracker ingest -- POST /ingest, called by the Gmail script.

Takes one email (subject, sender, body), decides what it means for the job
search, and updates the tracker:

  confirmation  ("thank you for applying")  -> new application, status applied
  rejection     ("unfortunately...")        -> matching application -> rejected
  next step     ("schedule a call")         -> matching application -> screen
  anything else                             -> ignored

Reading the email is done by a small model on Amazon Bedrock (Nova Micro).
If that call fails for any reason, simple pattern-matching takes over, so an
email is never dropped just because the model was unavailable.

Idempotent: the app_id is derived from the Gmail message id, so the same
email sent twice never creates two applications.
"""
import hashlib
import hmac
import json
import os
import re
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

TABLE = boto3.resource("dynamodb").Table(os.environ.get("TABLE_NAME", "job-tracker-apps"))
API_KEY = os.environ.get("API_KEY", "")
MODEL_ID = os.environ.get("MODEL_ID", "amazon.nova-micro-v1:0")
_bedrock = None

ACTIVE = {"applied", "screen", "interview", "offer"}

if not API_KEY:
    print("CONFIG ERROR: API_KEY is not set. Every ingest request will be refused.")


def bedrock():
    # One attempt, short timeouts. If the model is throttled or slow, the email
    # falls straight through to pattern-matching instead of waiting ~10 seconds
    # on retries (which is what new-account daily token limits caused).
    global _bedrock
    if _bedrock is None:
        from botocore.config import Config
        _bedrock = boto3.client("bedrock-runtime", config=Config(
            retries={"max_attempts": 1, "mode": "standard"}, connect_timeout=3, read_timeout=8))
    return _bedrock


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def resp(code, body):
    return {"statusCode": code, "headers": {"Content-Type": "application/json"},
            "body": json.dumps(body, default=str)}


# --- reading the email -----------------------------------------------------------

PROMPT = """You read emails from a job seeker's inbox. Reply with ONLY a JSON object:
{"kind": "confirmation" | "rejection" | "next_step" | "other",
 "company": "<employer name, or empty>",
 "role": "<job title, or empty>"}

confirmation = the employer received an application.
rejection    = the employer is not moving forward.
next_step    = the employer wants to schedule a call, screen, assessment, or interview.
other        = anything else (newsletters, job alerts, recommendations, marketing).
Job alert emails that list many jobs are "other". Use the employer's name, not the
recruiting platform (not Workday, Greenhouse, Lever, LinkedIn, Indeed)."""


def read_with_model(subject, sender, body):
    text = f"From: {sender}\nSubject: {subject}\n\n{body[:3000]}"
    out = bedrock().converse(
        modelId=MODEL_ID,
        system=[{"text": PROMPT}],
        messages=[{"role": "user", "content": [{"text": text}]}],
        inferenceConfig={"maxTokens": 200, "temperature": 0},
    )
    raw = out["output"]["message"]["content"][0]["text"]
    m = re.search(r"\{.*\}", raw, re.S)
    data = json.loads(m.group(0)) if m else {}
    kind = data.get("kind")
    if kind not in {"confirmation", "rejection", "next_step", "other"}:
        raise ValueError(f"model gave no usable kind: {raw[:120]}")
    return {"kind": kind, "company": (data.get("company") or "").strip()[:120],
            "role": (data.get("role") or "").strip()[:120], "reader": "model"}


# A company name = up to 6 capitalized words. No periods, so it stops at the end of a sentence.
COMPANY = r"(?P<company>[A-Z][\w&'-]*(?: (?:[A-Z&][\w&'-]*|of|and|the)){0,5})"
PLATFORMS = r"(?:workday|greenhouse|lever|icims|smartrecruiters|linkedin|indeed|taleo|jobvite|ashby)"


def company_from_sender(sender):
    name = re.sub(r"\s*<.*?>\s*", "", sender or "").strip().strip('"')
    if not name or re.search(PLATFORMS, name, re.I) or "@" in name:
        return ""
    name = re.sub(r"\b(careers?|jobs|recruiting|recruitment|talent( acquisition)?|hiring|team|hr|no.?reply)\b",
                  "", name, flags=re.I)
    return re.sub(r"\s{2,}", " ", name).strip(" -|,")


def read_with_patterns(subject, sender, body):
    text = f"{subject}\n{body[:3000]}"
    low = text.lower()
    if re.search(r"unfortunately|not (be )?moving forward|other candidates|decided to (pursue|move forward with)|"
                 r"no longer under consideration|not selected", low):
        kind = "rejection"
    elif re.search(r"schedule (a|an|your) (call|interview|chat|time)|phone screen|next steps? in (the|our) process|"
                   r"invite you to (an )?interview|availability for", low):
        kind = "next_step"
    elif re.search(r"thank(s| you) for (applying|your application|your interest)|application (was )?received|"
                   r"received your application|we('ve| have) received|application was sent to|"
                   r"confirming your .{0,40}application|your application (to|for) ", low):
        kind = "confirmation"
    else:
        kind = "other"

    role = company = ""
    m = re.search(r"(?:applying|application|applied) (?:for|to) (?:the )?(?:position of |role of )?"
                  r"(?P<role>[A-Z][^\n.,!]{2,80}?)(?: position| role)? (?:at|with) " + COMPANY,
                  text)
    if m:
        role, company = m.group("role").strip(), m.group("company").strip(" .")
    if not company:
        m = re.search(r"(?:applying|application|application was sent) (?:to|at|with) " + COMPANY, text)
        if m:
            company = m.group("company").strip()
    if not company:
        m = re.search(r"\byour (?P<company>[A-Z][\w&'-]*(?: [A-Z][\w&'-]*){0,4}) (?:job )?application", text)
        if m:
            company = m.group("company").strip()
    company = company or company_from_sender(sender)
    return {"kind": kind, "company": company[:120], "role": role[:120], "reader": "patterns"}


def read_email(subject, sender, body):
    try:
        return read_with_model(subject, sender, body)
    except Exception as e:  # any model failure -> fall back, never drop the email
        print(f"model unavailable, using patterns: {type(e).__name__}: {str(e)[:160]}")
        return read_with_patterns(subject, sender, body)


# --- updating the tracker ----------------------------------------------------------

def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def find_match(company):
    target = norm(company)
    if not target:
        return None
    items, kwargs = [], {}
    while True:
        page = TABLE.scan(**kwargs)
        items += page.get("Items", [])
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    hits = [a for a in items if a.get("status") in ACTIVE
            and (norm(a.get("company")) == target or target in norm(a.get("company")) or norm(a.get("company")) in target)]
    hits.sort(key=lambda a: a.get("updated_at", ""), reverse=True)
    return hits[0] if hits else None


def set_status(app, status, reason):
    ts = now()
    out = TABLE.update_item(
        Key={"app_id": app["app_id"]},
        UpdateExpression="SET #s = :s, updated_at = :u, history = list_append(if_not_exists(history, :e), :h)",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": status, ":u": ts, ":e": [],
                                   ":h": [{"status": status, "at": ts, "via": reason}]},
        ReturnValues="ALL_NEW",
    )
    return out["Attributes"]


def lambda_handler(event, context):
    got = (event.get("headers") or {}).get("x-api-key", "")
    if not (API_KEY and hmac.compare_digest(got, API_KEY)):
        return resp(401, {"error": "missing or wrong access key"})
    try:
        mail = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return resp(400, {"error": "body must be JSON"})
    msg_id = str(mail.get("message_id") or "")
    if not msg_id:
        return resp(400, {"error": "message_id is required"})

    subject, sender, body = mail.get("subject", ""), mail.get("from", ""), mail.get("body", "")
    r = read_email(subject, sender, body)
    base = {"message_id": msg_id, "kind": r["kind"], "reader": r["reader"],
            "company": r["company"], "role": r["role"]}

    if r["kind"] == "other":
        return resp(200, {**base, "action": "ignored"})

    if r["kind"] in ("rejection", "next_step"):
        match = find_match(r["company"])
        new_status = "rejected" if r["kind"] == "rejection" else "screen"
        if match:
            if r["kind"] == "next_step" and match["status"] in ("screen", "interview", "offer"):
                return resp(200, {**base, "action": "already further along", "app_id": match["app_id"]})
            updated = set_status(match, new_status, f"email: {subject[:80]}")
            return resp(200, {**base, "action": f"moved to {new_status}", "app_id": updated["app_id"]})
        # no match: fall through and record it, so the news isn't lost

    if r["kind"] == "confirmation":
        match = find_match(r["company"])
        if match and match.get("status") == "applied" and not match.get("email_id"):
            TABLE.update_item(Key={"app_id": match["app_id"]},
                              UpdateExpression="SET email_id = :m",
                              ExpressionAttributeValues={":m": msg_id})
            return resp(200, {**base, "action": "already tracked, linked to existing entry",
                              "app_id": match["app_id"]})

    status = {"confirmation": "applied", "rejection": "rejected", "next_step": "screen"}[r["kind"]]
    ts = now()
    sent = (mail.get("date") or ts)[:10]
    item = {
        "app_id": "m" + hashlib.sha256(msg_id.encode()).hexdigest()[:9],
        "company": r["company"] or "(unknown — check email)",
        "role": r["role"] or "(see email)",
        "status": status,
        "source": "Email (auto)",
        "applied_on": sent,
        "notes": f"Auto-logged from: {subject[:160]}",
        "created_at": ts,
        "updated_at": ts,
        "history": [{"status": status, "at": ts, "via": "email"}],
        "email_id": msg_id,
    }
    try:
        TABLE.put_item(Item=item, ConditionExpression="attribute_not_exists(app_id)")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return resp(200, {**base, "action": "duplicate, already logged", "app_id": item["app_id"]})
        raise
    return resp(201, {**base, "action": f"logged as {status}", "app_id": item["app_id"]})
