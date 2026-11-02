"""
Amazon Quick Usage Dashboard - Lambda Handler
Reads Quick logs from CloudWatch Logs (Chat, Feedback, Agent Hours, Index Usage).
Uses same Cognito auth as Kiro Usage API.
"""

import os
import json
import time
from datetime import datetime, timedelta
from collections import defaultdict

import boto3

# Configuration
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
CHAT_LOG_GROUP = os.environ.get("CHAT_LOG_GROUP", "/aws/quicksight/chat-logs")
FEEDBACK_LOG_GROUP = os.environ.get("FEEDBACK_LOG_GROUP", "/aws/quicksight/feedback-logs")
AGENT_HOURS_LOG_GROUP = os.environ.get("AGENT_HOURS_LOG_GROUP", "/aws/quicksight/agent-hours-logs")
INDEX_USAGE_LOG_GROUP = os.environ.get("INDEX_USAGE_LOG_GROUP", "/aws/quicksight/index-usage-logs")

logs_client = boto3.client("logs", region_name=AWS_REGION)
qs_client = boto3.client("quicksight", region_name=AWS_REGION)
sso_admin_client = boto3.client("sso-admin", region_name=AWS_REGION)
identitystore_client = boto3.client("identitystore", region_name=AWS_REGION)

# Identity Store id (IAM Identity Center) for resolving Creator UUIDs -> email.
# Resolved lazily; overridable via env for air-gapped / fixed setups.
_IDENTITY_STORE_ID = os.environ.get("IDENTITY_STORE_ID", "")
# Cache of identity-store user_id -> email across a single invocation.
_IDENTITY_EMAIL_CACHE = {}

# Account id is needed for all QuickSight asset calls. Resolved lazily from STS
# so the Lambda does not need it as config.
_ACCOUNT_ID = os.environ.get("QS_ACCOUNT_ID", "")


def _account_id():
    """Resolve the AWS account id (cached) for QuickSight asset calls."""
    global _ACCOUNT_ID
    if not _ACCOUNT_ID:
        try:
            _ACCOUNT_ID = boto3.client("sts", region_name=AWS_REGION).get_caller_identity()["Account"]
        except Exception:
            _ACCOUNT_ID = ""
    return _ACCOUNT_ID


def _identity_store_id():
    """Resolve the IAM Identity Center identity-store id (cached)."""
    global _IDENTITY_STORE_ID
    if not _IDENTITY_STORE_ID:
        try:
            inst = sso_admin_client.list_instances().get("Instances", [])
            if inst:
                _IDENTITY_STORE_ID = inst[0].get("IdentityStoreId", "")
        except Exception:
            _IDENTITY_STORE_ID = ""
    return _IDENTITY_STORE_ID


def _extract_identity_uuid(creator):
    """Pull the identity-store user UUID out of a QuickSight Creator value.

    Creator looks like:
      arn:aws:quicksight::ACCT:user-internal/idc/ssoins-.../apl-.../IDENTITY_STORE_ID/<uuid>
    or the literal 'Auto_Created', or a plain email (spaces). Returns the UUID
    only when the ARN carries one, else None.
    """
    if not creator or creator == "Auto_Created":
        return None
    s = str(creator)
    if "@" in s:  # already an email, not a UUID ARN
        return None
    tail = s.rsplit("/", 1)[-1].strip()
    # UUID-ish: 8-4-4-4-12 hex (identity-store ids are this shape)
    import re

    if re.match(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", tail):
        return tail
    return None


def _resolve_creator_email(creator):
    """Resolve a QuickSight Creator value to an email, best-effort.

    - Plain email -> returned as-is.
    - identity-store ARN -> describe_user to get the primary email (cached).
    - 'Auto_Created' / unknown -> None.
    """
    if not creator:
        return None
    s = str(creator).strip()
    if "@" in s:
        # Either a bare email or an ARN tail like .../user/default/<email>.
        tail = s.rsplit("/", 1)[-1]
        return tail if "@" in tail else s
    uuid = _extract_identity_uuid(s)
    if not uuid:
        return None
    if uuid in _IDENTITY_EMAIL_CACHE:
        return _IDENTITY_EMAIL_CACHE[uuid]
    email = None
    store = _identity_store_id()
    if store:
        try:
            u = identitystore_client.describe_user(IdentityStoreId=store, UserId=uuid)
            emails = u.get("Emails") or []
            primary = next((e.get("Value") for e in emails if e.get("Primary")), None)
            email = primary or (emails[0].get("Value") if emails else None) or u.get("UserName")
        except Exception:
            email = None
    _IDENTITY_EMAIL_CACHE[uuid] = email
    return email


def lambda_handler(event, context):
    """Main Lambda handler for API Gateway proxy integration."""
    path = event.get("path", "/")
    method = event.get("httpMethod", "GET")
    params = event.get("queryStringParameters") or {}

    # Route requests
    if path in ("/quick/health", "/health"):
        return respond(200, {"status": "ok", "service": "quick-usage-api", "timestamp": datetime.utcnow().isoformat()})
    elif path == "/quick/all":
        return respond(200, all_quick_data(params))
    elif path == "/quick/chat":
        return respond(200, chat_logs(params))
    elif path == "/quick/feedback":
        return respond(200, feedback_logs(params))
    elif path == "/quick/agent-hours":
        return respond(200, agent_hours_logs(params))
    elif path == "/quick/index-usage":
        return respond(200, index_usage_logs(params))
    elif path == "/quick/users":
        return respond(200, user_summary(params))
    elif path == "/quick/conversations":
        return respond(200, conversations(params))
    elif path == "/quick/agents":
        return respond(200, agents_summary(params))
    elif path == "/quick/spaces":
        return respond(200, spaces_summary(params))
    elif path == "/quick/report":
        return respond(200, report_summary(params))
    elif path == "/quick/assets":
        return respond(200, assets_summary(params))
    else:
        return respond(404, {"error": "Not found", "path": path})


def respond(status_code, body):
    """Create API Gateway response."""
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET,OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
        },
        "body": json.dumps(body, default=str, ensure_ascii=False),
    }


# ─── HELPERS ──────────────────────────────────────────────────────────────────


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None


def _get_time_range(params):
    """Get time range from params, default last 7 days."""
    from_dt = _parse_date(params.get("from"))
    to_dt = _parse_date(params.get("to"))

    if not to_dt:
        to_dt = datetime.utcnow()
    if not from_dt:
        from_dt = to_dt - timedelta(days=7)

    # Set to end of day for to_dt
    to_dt = to_dt.replace(hour=23, minute=59, second=59)

    return int(from_dt.timestamp() * 1000), int(to_dt.timestamp() * 1000)


def _fmt_timestamp(value):
    """Format a CloudWatch Logs timestamp to 'YYYY-MM-DD HH:MM:SS'.

    max(@timestamp) in Logs Insights comes back as an ISO-ish string
    ('2026-10-23 10:15:00.000'); older paths may hand epoch ms. Accept both.
    """
    if value is None:
        return None
    s = str(value).strip()
    # Try epoch milliseconds first.
    try:
        num = float(s)
        if num > 1e11:  # clearly epoch ms
            return datetime.utcfromtimestamp(num / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        pass
    # Try ISO-ish string: drop trailing millis / 'Z', normalise separator.
    iso = s.replace("T", " ").replace("Z", "").split(".")[0]
    try:
        return datetime.strptime(iso, "%Y-%m-%d %H:%M:%S").strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return iso or None


def _created_in_range(created_value, start_ms, end_ms):
    """True if an asset's created timestamp falls within [start_ms, end_ms].

    QuickSight asset APIs return created time either as a datetime (boto3) or an
    ISO/epoch string (json round-trip). Parse best-effort; if it cannot be
    parsed, keep the asset (do not silently drop it) and let the caller see it.
    """
    if created_value is None:
        return True
    ts = None
    if isinstance(created_value, (int, float)):
        # epoch seconds vs ms
        ts = float(created_value) * (1000 if created_value < 1e12 else 1)
    elif isinstance(created_value, datetime):
        ts = created_value.timestamp() * 1000
    else:
        s = str(created_value).strip()
        try:
            ts = datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000
        except Exception:
            try:
                ts = float(s) * (1000 if float(s) < 1e12 else 1)
            except Exception:
                return True  # unparseable -> keep
    return start_ms <= ts <= end_ms


def _user_filter(params):
    """Return CloudWatch Logs Insights filter clause for user."""
    user = params.get("user", "")
    if user:
        return f"| filter user_arn like '{user}'\n    "
    return ""


def _query_logs(log_group, query, start_time, end_time, limit=1000):
    """Run CloudWatch Logs Insights query."""
    try:
        response = logs_client.start_query(
            logGroupName=log_group,
            startTime=start_time,
            endTime=end_time,
            queryString=query,
            limit=limit,
        )
        query_id = response["queryId"]

        # Wait for query to complete
        for _ in range(30):
            result = logs_client.get_query_results(queryId=query_id)
            if result["status"] == "Complete":
                return result["results"]
            time.sleep(0.5)

        return []
    except logs_client.exceptions.ResourceNotFoundException:
        return []
    except Exception as e:
        return [{"error": str(e)}]


def _results_to_dicts(results):
    """Convert CloudWatch Logs Insights results to list of dicts."""
    records = []
    if not results:
        return records
    for row in results:
        if isinstance(row, dict) and "error" in row:
            return [row]
        record = {}
        for field in row:
            if isinstance(field, dict) and field.get("field") != "@ptr":
                record[field["field"]] = field.get("value", "")
        if record:
            records.append(record)
    return records


# ─── API HANDLERS ─────────────────────────────────────────────────────────────


def all_quick_data(params):
    """Return all Quick data in one response."""
    start_time, end_time = _get_time_range(params)

    # Chat summary
    chat_query = """
    fields @timestamp, user_arn, status_code, conversation_id, user_message, system_text_message, message_scope, agent_id
    | stats count() as total_messages,
            count_distinct(conversation_id) as total_conversations,
            count_distinct(user_arn) as total_users
    """
    chat_summary_results = _query_logs(CHAT_LOG_GROUP, chat_query, start_time, end_time)

    # Chat by user
    chat_by_user_query = """
    fields user_arn, status_code, conversation_id
    | stats count() as messages, count_distinct(conversation_id) as conversations by user_arn
    | sort messages desc
    """
    chat_by_user_results = _query_logs(CHAT_LOG_GROUP, chat_by_user_query, start_time, end_time)

    # Chat by day
    chat_by_day_query = """
    fields @timestamp, conversation_id, user_arn
    | stats count() as messages, count_distinct(conversation_id) as conversations, count_distinct(user_arn) as users by bin(1d) as day
    | sort day asc
    """
    chat_by_day_results = _query_logs(CHAT_LOG_GROUP, chat_by_day_query, start_time, end_time)

    # Feedback summary
    feedback_query = """
    fields feedback_type, feedback_reason
    | stats count() as total by feedback_type
    """
    feedback_results = _query_logs(FEEDBACK_LOG_GROUP, feedback_query, start_time, end_time)

    # Agent hours
    agent_hours_query = """
    fields reporting_service, usage_group, usage_hours
    | stats sum(usage_hours) as total_hours by reporting_service, usage_group
    | sort total_hours desc
    """
    agent_hours_results = _query_logs(AGENT_HOURS_LOG_GROUP, agent_hours_query, start_time, end_time)

    # Index usage
    index_query = """
    fields source_type, source_name, consumed_source_size, consumed_source_doc_count, consumed_index_size
    | sort @timestamp desc
    | limit 50
    """
    index_results = _query_logs(INDEX_USAGE_LOG_GROUP, index_query, start_time, end_time)

    return {
        "date_range": {
            "from": datetime.utcfromtimestamp(start_time / 1000).strftime("%Y-%m-%d"),
            "to": datetime.utcfromtimestamp(end_time / 1000).strftime("%Y-%m-%d"),
        },
        "chat_overview": _results_to_dicts(chat_summary_results),
        "chat_by_user": _results_to_dicts(chat_by_user_results),
        "chat_by_day": _results_to_dicts(chat_by_day_results),
        "feedback": _results_to_dicts(feedback_results),
        "agent_hours": _results_to_dicts(agent_hours_results),
        "index_usage": _results_to_dicts(index_results),
    }


def chat_logs(params):
    """Get chat logs with details."""
    start_time, end_time = _get_time_range(params)
    limit = int(params.get("limit", "100"))
    user_filter = _user_filter(params)

    query = f"""
    fields @timestamp, user_arn, status_code, conversation_id, user_message, system_text_message, message_scope, agent_id, latency, surface_type
    {user_filter}| sort @timestamp desc
    """
    results = _query_logs(CHAT_LOG_GROUP, query, start_time, end_time, limit=limit)
    return {"logs": _results_to_dicts(results), "count": len(results)}


def feedback_logs(params):
    """Get feedback logs."""
    start_time, end_time = _get_time_range(params)
    limit = int(params.get("limit", "100"))

    query = f"""
    fields @timestamp, user_arn, feedback_type, feedback_reason, feedback_details, conversation_id
    {_user_filter(params)}| sort @timestamp desc
    """
    results = _query_logs(FEEDBACK_LOG_GROUP, query, start_time, end_time, limit=limit)

    # Summary
    summary_query = """
    fields feedback_type
    | stats count() as total by feedback_type
    """
    summary = _query_logs(FEEDBACK_LOG_GROUP, summary_query, start_time, end_time)

    return {
        "logs": _results_to_dicts(results),
        "count": len(results),
        "summary": _results_to_dicts(summary),
    }


def agent_hours_logs(params):
    """Get agent/research hours usage."""
    start_time, end_time = _get_time_range(params)

    query = f"""
    fields @timestamp, user_arn, subscription_type, reporting_service, usage_group, usage_hours, service_resource_arn
    {_user_filter(params)}| sort @timestamp desc
    """
    results = _query_logs(AGENT_HOURS_LOG_GROUP, query, start_time, end_time)

    # Summary by service
    summary_query = """
    fields reporting_service, usage_group, usage_hours
    | stats sum(usage_hours) as total_hours, count() as sessions by reporting_service, usage_group
    | sort total_hours desc
    """
    summary = _query_logs(AGENT_HOURS_LOG_GROUP, summary_query, start_time, end_time)

    return {
        "logs": _results_to_dicts(results),
        "count": len(results),
        "summary": _results_to_dicts(summary),
    }


def index_usage_logs(params):
    """Get index storage usage."""
    start_time, end_time = _get_time_range(params)

    query = """
    fields @timestamp, source_type, source_name, source_arn, consumed_index_size, consumed_source_size, consumed_source_doc_count
    | sort @timestamp desc
    | limit 100
    """
    results = _query_logs(INDEX_USAGE_LOG_GROUP, query, start_time, end_time)
    return {"logs": _results_to_dicts(results), "count": len(results)}


def user_summary(params):
    """Get per-user usage summary across all log types."""
    start_time, end_time = _get_time_range(params)

    # Chat usage per user
    chat_query = """
    fields user_arn, conversation_id, status_code
    | stats count() as total_messages, count_distinct(conversation_id) as conversations by user_arn
    | sort total_messages desc
    """
    chat_results = _query_logs(CHAT_LOG_GROUP, chat_query, start_time, end_time)

    # Feedback per user
    feedback_query = """
    fields user_arn, feedback_type
    | stats count() as total_feedback,
            sum(case when feedback_type = 'Useful' then 1 else 0 end) as useful,
            sum(case when feedback_type = 'Not Useful' then 1 else 0 end) as not_useful
            by user_arn
    | sort total_feedback desc
    """
    feedback_results = _query_logs(FEEDBACK_LOG_GROUP, feedback_query, start_time, end_time)

    # Agent hours per user
    hours_query = """
    fields user_arn, usage_hours, reporting_service
    | stats sum(usage_hours) as total_hours by user_arn
    | sort total_hours desc
    """
    hours_results = _query_logs(AGENT_HOURS_LOG_GROUP, hours_query, start_time, end_time)

    return {
        "chat_usage": _results_to_dicts(chat_results),
        "feedback_usage": _results_to_dicts(feedback_results),
        "agent_hours_usage": _results_to_dicts(hours_results),
    }


def conversations(params):
    """Get conversation details."""
    start_time, end_time = _get_time_range(params)
    conversation_id = params.get("id")

    if conversation_id:
        query = f"""
        fields @timestamp, user_arn, user_message, system_text_message, status_code, message_scope
        | filter conversation_id = '{conversation_id}'
        | sort @timestamp asc
        """
    else:
        query = """
        fields @timestamp, user_arn, conversation_id, user_message, status_code
        | stats count() as messages, min(@timestamp) as started, max(@timestamp) as last_message by conversation_id, user_arn
        | sort last_message desc
        | limit 50
        """

    results = _query_logs(CHAT_LOG_GROUP, query, start_time, end_time)
    return {"conversations": _results_to_dicts(results), "count": len(results)}


# ─── NEW: AGGREGATED REPORTING HANDLERS ───────────────────────────────────────
#
# These endpoints build the file/agent-level reporting table.
# IMPORTANT: CloudWatch Logs only records ACTIVITY (who used what), NOT asset
# configuration. Fields that are asset metadata (Agent Name, Owner, Welcome
# Message, Instruction, Created Date, and "Created" counts for Spaces/Flows/
# Agents) do NOT exist in any Quick log group — they must be pulled from the
# QuickSight/Quick Suite admin API separately and are returned as null here.


def _email_from_arn(user_arn):
    """Extract the email/user portion from a Quick user ARN.

    e.g. arn:aws:quicksight:us-east-1:ACCT:user/default/linhndk@mzcvietnam...
    -> linhndk@mzcvietnam...
    """
    if not user_arn:
        return ""
    return user_arn.rsplit("/", 1)[-1]


def _owner_from_creator(creator_arn):
    """Best-effort human-readable owner from an agent Creator ARN."""
    if not creator_arn:
        return None
    tail = creator_arn.rsplit("/", 1)[-1]
    return tail or creator_arn


def _list_agents_metadata():
    """Return {agent_id: {name, owner, created_date, updated_date}} from the
    QuickSight admin API (list-agents). One paginated call covers all agents.

    These fields are NOT in CloudWatch Logs; this is the enrichment source.
    Returns {} (and callers degrade to nulls) if the API is unavailable.
    """
    acct = _account_id()
    if not acct:
        return {}
    meta = {}
    try:
        token = None
        while True:
            kwargs = {"AwsAccountId": acct, "MaxResults": 100}
            if token:
                kwargs["NextToken"] = token
            resp = qs_client.list_agents(**kwargs)
            for a in resp.get("AgentSummaries", []):
                meta[a.get("AgentId")] = {
                    "agent_name": a.get("Name"),
                    "description": a.get("Description"),
                    "created_date": a.get("CreatedAt"),
                    "updated_date": a.get("UpdatedAt"),
                    "arn": a.get("Arn"),
                }
            token = resp.get("NextToken")
            if not token:
                break
    except Exception:
        return {}
    return meta


def _describe_agent_detail(agent_id):
    """Fetch owner / welcome / instruction for a single agent via describe-agent.

    Heavier than list-agents (one call per agent) — used only when the caller
    asks for detail=true.
    """
    acct = _account_id()
    if not acct or not agent_id or agent_id in ("-", "SYSTEM"):
        return {}
    try:
        agent = qs_client.describe_agent(AwsAccountId=acct, AgentId=agent_id).get("Agent", {})
    except Exception:
        return {}
    cpi = agent.get("CustomPromptInterface", {}) or {}
    starters = [s for s in (agent.get("StarterPrompts") or []) if s]
    creator = agent.get("Creator")
    # Resolve the Creator (identity-store UUID ARN) to an email when possible;
    # fall back to the raw tail if it cannot be resolved (e.g. 'Auto_Created').
    owner = _resolve_creator_email(creator) or _owner_from_creator(creator)
    return {
        "owner": owner,
        "owner_id": _owner_from_creator(creator),
        "welcome_message": starters[0] if starters else None,
        "starter_prompts": starters or None,
        "instruction": cpi.get("CustomInstructions"),
        "instruction_summary": cpi.get("promptSummary"),
        "agent_status": agent.get("AgentStatus"),
        "agent_lifecycle": agent.get("AgentLifecycle"),
    }


def _created_counts_by_email():
    """Count assets CREATED per user email from the QuickSight admin API.

    Returns ({email_lower: {spaces_created, flows_created, chat_agents_created}},
             totals, unattributed) where:
      - spaces: owner is `createdBy` = email directly (list-spaces).
      - agents/flows: owner is `Creator`, resolved to an email via
        describe-agent/describe-flow + identitystore:DescribeUser
        (_resolve_creator_email). 'Auto_Created'/unresolvable -> unattributed.
      - totals: account-wide created counts (independent of attribution).
      - unattributed: per-type count that could not be tied to an email.
    """
    acct = _account_id()
    counts = defaultdict(lambda: {"spaces_created": 0, "flows_created": 0, "chat_agents_created": 0})
    totals = {"spaces_created_total": 0, "flows_created_total": 0, "agents_created_total": 0}
    unattributed = {"spaces": 0, "flows": 0, "agents": 0}
    if not acct:
        return counts, totals, unattributed

    def _bump(email, key, kind):
        if email:
            counts[email.strip().lower()][key] += 1
        else:
            unattributed[kind] += 1

    # Spaces — createdBy is an email directly
    try:
        token = None
        while True:
            kw = {"AwsAccountId": acct, "MaxResults": 100}
            if token:
                kw["NextToken"] = token
            r = qs_client.list_spaces(**kw)
            for s in r.get("SpaceSummaries", []):
                totals["spaces_created_total"] += 1
                _bump(s.get("createdBy"), "spaces_created", "spaces")
            token = r.get("NextToken")
            if not token:
                break
    except Exception:
        pass

    # Agents — Creator (identity-store UUID ARN) resolved to email
    try:
        token = None
        while True:
            kw = {"AwsAccountId": acct, "MaxResults": 100}
            if token:
                kw["NextToken"] = token
            r = qs_client.list_agents(**kw)
            for a in r.get("AgentSummaries", []):
                totals["agents_created_total"] += 1
                aid = a.get("AgentId")
                if not aid:
                    unattributed["agents"] += 1
                    continue
                try:
                    ag = qs_client.describe_agent(AwsAccountId=acct, AgentId=aid).get("Agent", {})
                    _bump(_resolve_creator_email(ag.get("Creator")), "chat_agents_created", "agents")
                except Exception:
                    unattributed["agents"] += 1
            token = r.get("NextToken")
            if not token:
                break
    except Exception:
        pass

    # Flows — Creator resolved to email
    try:
        token = None
        while True:
            kw = {"AwsAccountId": acct, "MaxResults": 100}
            if token:
                kw["NextToken"] = token
            r = qs_client.list_flows(**kw)
            for f in r.get("FlowSummaryList", []):
                totals["flows_created_total"] += 1
                fid = f.get("FlowId")
                if not fid:
                    unattributed["flows"] += 1
                    continue
                # describe-flow requires PublishState. Owner is CreatedBy (a
                # QuickSight user ARN ending in the email). Unpublished flows
                # return CreatedBy=None on PUBLISHED but carry it on DRAFT, so
                # try PUBLISHED then fall back to DRAFT.
                creator = None
                for ps in ("PUBLISHED", "DRAFT"):
                    try:
                        fl = qs_client.describe_flow(
                            AwsAccountId=acct, FlowId=fid, PublishState=ps
                        ).get("Flow", {})
                    except Exception:
                        continue
                    creator = fl.get("CreatedBy") or fl.get("Creator")
                    if creator:
                        break
                _bump(_resolve_creator_email(creator), "flows_created", "flows")
            token = r.get("NextToken")
            if not token:
                break
    except Exception:
        pass

    return counts, totals, unattributed


def agents_summary(params):
    """Per-agent activity rollup from chat-logs (grouped by agent_id), enriched
    with asset metadata from the QuickSight admin API.

    From logs: number_of_queries, number_of_conversations, number_of_users.
    Enriched (list-agents): agent_name, owner/created_date (created_date yes,
    owner needs detail), updated_date.
    Enriched (describe-agent, only when detail=true): owner, welcome_message,
    instruction, instruction_summary, status, lifecycle.

    Query params:
      enrich=false -> skip the QuickSight metadata merge (logs only).
      detail=true  -> additionally call describe-agent per agent for
                      owner/welcome/instruction (slower).
      by_user=true -> add a per-user breakdown under each agent (which users
                      used it, with their query/conversation counts).
      all_time=true -> count Number of Users/Queries/Conversations cumulatively
                       from the agent's creation (Table 3 "All-time"); widens
                       the log query window to CloudWatch's max retention.
    """
    start_time, end_time = _get_time_range(params)
    enrich = str(params.get("enrich", "true")).lower() != "false"
    detail = str(params.get("detail", "false")).lower() == "true"
    by_user = str(params.get("by_user", "false")).lower() == "true"
    all_time = str(params.get("all_time", "false")).lower() == "true"

    # All-time (Table 3): counts are cumulative since the agent was created.
    # CloudWatch Logs Insights can only scan what is retained, so widen the
    # window to ~2 years back (well beyond the oldest asset) up to now.
    if all_time:
        end_time = int(datetime.utcnow().timestamp() * 1000)
        start_time = int((datetime.utcnow() - timedelta(days=730)).timestamp() * 1000)

    query = """
    fields agent_id, conversation_id, user_arn
    | filter agent_id like /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
    | stats count() as number_of_queries,
            count_distinct(conversation_id) as number_of_conversations,
            count_distinct(user_arn) as number_of_users
            by agent_id
    | sort number_of_queries desc
    """
    results = _results_to_dicts(_query_logs(CHAT_LOG_GROUP, query, start_time, end_time))

    meta = _list_agents_metadata() if enrich else {}

    # Optional per-user breakdown: one extra query grouped by (agent_id, user).
    users_by_agent = {}
    if by_user:
        bu_query = """
        fields agent_id, conversation_id, user_arn
        | filter agent_id like /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
        | stats count() as queries,
                count_distinct(conversation_id) as conversations
                by agent_id, user_arn
        | sort queries desc
        """
        for r in _results_to_dicts(_query_logs(CHAT_LOG_GROUP, bu_query, start_time, end_time)):
            if "error" in r:
                break
            aid = r.get("agent_id")
            users_by_agent.setdefault(aid, []).append(
                {
                    "user_email": _email_from_arn(r.get("user_arn", "")),
                    "user_arn": r.get("user_arn"),
                    "queries": int(r.get("queries", 0) or 0),
                    "conversations": int(r.get("conversations", 0) or 0),
                }
            )

    agents = []
    for row in results:
        if "error" in row:
            return {"error": row["error"]}
        aid = row.get("agent_id")
        m = meta.get(aid, {})
        # exists: an agent_id seen in the logs but ABSENT from list-agents has
        # been DELETED from Quick (its activity survives in CloudWatch Logs but
        # its metadata is gone). exists=True -> still present; False -> deleted;
        # None -> unknown (enrich disabled, so list-agents was never fetched).
        if not enrich:
            exists = None
        else:
            exists = aid in meta
        entry = {
            "agent_id": aid,
            "number_of_queries": int(row.get("number_of_queries", 0) or 0),
            "number_of_conversations": int(row.get("number_of_conversations", 0) or 0),
            "number_of_users": int(row.get("number_of_users", 0) or 0),
            # Lifecycle flags: an agent with activity in logs but no metadata
            # was deleted from Quick.
            "exists": exists,
            "is_deleted": (exists is False) if exists is not None else None,
            # From QuickSight list-agents (null if deleted / enrich disabled):
            "agent_name": m.get("agent_name"),
            "description": m.get("description"),
            "created_date": m.get("created_date"),
            "updated_date": m.get("updated_date"),
            # From describe-agent (only when detail=true):
            "owner": None,
            "welcome_message": None,
            "instruction": None,
            "instruction_summary": None,
        }
        # Only describe agents that still exist (describe-agent on a deleted
        # agent just fails); skip the wasted call when we know it is gone.
        if detail and exists is not False:
            entry.update(_describe_agent_detail(aid))
        if by_user:
            entry["users"] = users_by_agent.get(aid, [])
        agents.append(entry)

    existing_count = sum(1 for a in agents if a.get("exists") is True)
    deleted_count = sum(1 for a in agents if a.get("is_deleted") is True)

    return {
        "report_date": datetime.utcnow().strftime("%Y-%m-%d"),
        "agents": agents,
        "count": len(agents),
        "existing_count": existing_count if enrich else None,
        "deleted_count": deleted_count if enrich else None,
        "enriched": enrich,
        "detail": detail,
        "by_user": by_user,
        "all_time": all_time,
        "note": "agent_name/created_date/updated_date from QuickSight list-agents; "
        "owner/welcome/instruction require detail=true (describe-agent). "
        "all_time=true counts queries/conversations/users cumulatively since "
        "agent creation (wide log window). exists=false / is_deleted=true marks "
        "an agent that has activity in logs but was DELETED from Quick "
        "(metadata null). exists=null when enrich=false.",
    }


def spaces_summary(params):
    """Space & KB usage rollup from index-usage-logs (source_type SPACE/KB).

    index-usage-logs carries source_type (SPACE|KB), source_name, and consumed
    size/doc counts — the only place 'Spaces Used' is observable in logs.
    """
    start_time, end_time = _get_time_range(params)

    query = """
    fields source_type, source_name, source_arn, user_arn,
           consumed_index_size, consumed_source_size, consumed_source_doc_count
    | stats latest(consumed_index_size) as index_size,
            latest(consumed_source_size) as source_size,
            latest(consumed_source_doc_count) as doc_count,
            count_distinct(user_arn) as number_of_users
            by source_type, source_name, source_arn
    | sort index_size desc
    | limit 200
    """
    results = _results_to_dicts(_query_logs(INDEX_USAGE_LOG_GROUP, query, start_time, end_time))
    if results and "error" in results[0]:
        return {"error": results[0]["error"]}

    spaces = [r for r in results if r.get("source_type") == "SPACE"]
    knowledge_bases = [r for r in results if r.get("source_type") == "KB"]

    return {
        "report_date": datetime.utcnow().strftime("%Y-%m-%d"),
        "spaces": spaces,
        "spaces_used": len(spaces),
        "knowledge_bases": knowledge_bases,
        "kb_used": len(knowledge_bases),
        "note": "'Created' counts are not in logs; this reflects indexed/used "
        "sources only. Enrich created counts via QuickSight admin API.",
    }


def report_summary(params):
    """Per-user consolidated reporting table matching the requested columns.

    Columns resolvable from logs:
      report_date         -> query date
      user_email          -> from user_arn
      user_type           -> chat/feedback logs
      total_queries       -> chat-logs count() (alias: chat_queries)
      total_conversations -> distinct conversation_id (alias: conversations)
      last_activity_date  -> max(@timestamp) of the user's chat messages
      chat_agents_used    -> distinct agent_id (real agents only)
      flows_used          -> distinct flow_id (chat) + FLOW reporting_service
      research_used_hours -> agent-hours RESEARCH usage_hours
      flow_used_hours     -> agent-hours FLOW usage_hours
      total_usage_hours   -> agent-hours sum

    Created-per-user columns (spaces_created / flows_created /
    chat_agents_created) are NOT in CloudWatch Logs. When enrich=true (default)
    they are filled from the QuickSight admin API by matching each asset's
    owner (space createdBy / agent+flow Creator) to the user; pass enrich=false
    to skip those API calls and leave them null.
    """
    start_time, end_time = _get_time_range(params)
    enrich = str(params.get("enrich", "true")).lower() != "false"

    # Chat activity per user (last_activity = most recent message timestamp)
    chat_query = """
    fields user_arn, user_type, conversation_id, agent_id, flow_id
    | stats count() as chat_queries,
            count_distinct(conversation_id) as conversations,
            count_distinct(agent_id) as distinct_agent_ids,
            count_distinct(flow_id) as flows_used_chat,
            max(@timestamp) as last_activity_ms
            by user_arn, user_type
    """
    chat = _results_to_dicts(_query_logs(CHAT_LOG_GROUP, chat_query, start_time, end_time))

    # Agent-hours per user, split by reporting_service
    hours_query = """
    fields user_arn, reporting_service, usage_hours
    | stats sum(usage_hours) as total_hours,
            sum(usage_hours * (reporting_service = 'RESEARCH')) as research_hours,
            sum(usage_hours * (reporting_service = 'FLOW')) as flow_hours
            by user_arn
    """
    hours = _results_to_dicts(_query_logs(AGENT_HOURS_LOG_GROUP, hours_query, start_time, end_time))

    for rows in (chat, hours):
        if rows and "error" in rows[0]:
            return {"error": rows[0]["error"]}

    # Merge by user_arn
    by_user = {}

    def _slot(arn):
        return by_user.setdefault(
            arn,
            {
                "user_email": _email_from_arn(arn),
                "user_arn": arn,
                "user_type": None,
                # Table 1 columns (total_* are the primary names; chat_queries/
                # conversations kept as aliases for backward compatibility):
                "total_queries": 0,
                "total_conversations": 0,
                "last_activity_date": None,
                "chat_queries": 0,
                "conversations": 0,
                # Table 2 "Used" columns:
                "chat_agents_used": 0,
                "flows_used": 0,
                "research_used_hours": 0.0,
                "flow_used_hours": 0.0,
                "total_usage_hours": 0.0,
                # Table 2 "Created" columns — all attributable per user_email:
                # spaces via createdBy (email), flows/agents via Creator UUID
                # resolved through IAM Identity Center (describe-user).
                "spaces_created": 0 if enrich else None,
                "flows_created": 0 if enrich else None,
                "chat_agents_created": 0 if enrich else None,
            },
        )

    for r in chat:
        arn = r.get("user_arn", "")
        if not arn:
            continue
        s = _slot(arn)
        s["user_type"] = r.get("user_type") or s["user_type"]
        q = int(r.get("chat_queries", 0) or 0)
        c = int(r.get("conversations", 0) or 0)
        s["total_queries"] = q
        s["total_conversations"] = c
        s["chat_queries"] = q  # alias
        s["conversations"] = c  # alias
        # last_activity_date: Logs Insights returns max(@timestamp) as an ISO
        # string ("2026-10-23 10:15:00.000"), not epoch ms. Handle both.
        last_val = r.get("last_activity_ms")
        if last_val:
            s["last_activity_date"] = _fmt_timestamp(last_val)
        # distinct_agent_ids includes '-'/'SYSTEM'; subtract a conservative
        # estimate is unsafe, so expose raw distinct agent ids used.
        s["chat_agents_used"] = int(r.get("distinct_agent_ids", 0) or 0)
        s["flows_used"] = int(r.get("flows_used_chat", 0) or 0)

    for r in hours:
        arn = r.get("user_arn", "")
        if not arn:
            continue
        s = _slot(arn)
        s["total_usage_hours"] = round(float(r.get("total_hours", 0) or 0), 6)
        s["research_used_hours"] = round(float(r.get("research_hours", 0) or 0), 6)
        s["flow_used_hours"] = round(float(r.get("flow_hours", 0) or 0), 6)

    # Table 2 "Created" per user — spaces by email, flows/agents by Creator
    # UUID resolved to email via IAM Identity Center.
    asset_totals = {}
    unattributed = {}
    if enrich:
        created, asset_totals, unattributed = _created_counts_by_email()
        for s in by_user.values():
            email = (s.get("user_email") or "").strip().lower()
            c = created.get(email)
            if c:
                s["spaces_created"] = c["spaces_created"]
                s["flows_created"] = c["flows_created"]
                s["chat_agents_created"] = c["chat_agents_created"]

    rows = sorted(by_user.values(), key=lambda x: x["total_queries"], reverse=True)

    return {
        "report_date": datetime.utcnow().strftime("%Y-%m-%d"),
        "date_range": {
            "from": datetime.utcfromtimestamp(start_time / 1000).strftime("%Y-%m-%d"),
            "to": datetime.utcfromtimestamp(end_time / 1000).strftime("%Y-%m-%d"),
        },
        "report": rows,
        "count": len(rows),
        "enriched": enrich,
        # Account-wide created totals (full counts regardless of attribution).
        "spaces_created_total": asset_totals.get("spaces_created_total"),
        "flows_created_total": asset_totals.get("flows_created_total"),
        "agents_created_total": asset_totals.get("agents_created_total"),
        # Assets whose Creator could not be resolved to an email
        # ('Auto_Created' or deleted identity-store user) — not in any row.
        "unattributed_created": unattributed or None,
        "note": "total_queries/total_conversations/last_activity_date and all "
        "'Used' columns come from CloudWatch Logs. spaces_created/flows_created/"
        "chat_agents_created are per-user: spaces via list-spaces.createdBy "
        "(email), flows/agents via Creator UUID resolved through IAM Identity "
        "Center describe-user. 'Auto_Created' or unresolvable creators go to "
        "unattributed_created (not any user). *_created_total = account-wide. "
        "Research has no asset list API. enrich=false skips all QuickSight calls.",
    }


def assets_summary(params):
    """Asset 'Created' counts + metadata from the QuickSight admin API.

    This is the companion to the log-derived 'Used' endpoints: it answers
    'how many spaces/flows/agents were CREATED' with their created date and
    owner — none of which exist in CloudWatch Logs.

      spaces  : list-spaces  -> name, created_at, owner (createdBy), resources
      flows   : list-flows   -> name, created_time, run_count, user_count
      agents  : list-agents  -> name, created_at
      research: NOT an asset  -> no list API; counted from logs elsewhere.

    Time range: ?from=YYYY-MM-DD&to=YYYY-MM-DD keeps only assets whose
    created_at falls in the range (so '*_created' = created in that window).
    Pass ?all_time=true to count every asset regardless of created date.
    '*_total' always carries the full count so you never lose the overall number.
    """
    acct = _account_id()
    if not acct:
        return {"error": "Could not resolve AWS account id for QuickSight calls."}

    start_time, end_time = _get_time_range(params)
    all_time = str(params.get("all_time", "false")).lower() == "true"

    def _keep(created):
        return True if all_time else _created_in_range(created, start_time, end_time)

    spaces, flows, agents = [], [], []
    spaces_total = flows_total = agents_total = 0
    errors = {}

    # Spaces (owner + created date available directly)
    try:
        token = None
        while True:
            kw = {"AwsAccountId": acct, "MaxResults": 100}
            if token:
                kw["NextToken"] = token
            r = qs_client.list_spaces(**kw)
            for s in r.get("SpaceSummaries", []):
                spaces_total += 1
                if not _keep(s.get("createdAt")):
                    continue
                spaces.append(
                    {
                        "space_id": s.get("spaceId"),
                        "name": s.get("name"),
                        "description": s.get("description"),
                        "created_at": s.get("createdAt"),
                        "updated_at": s.get("updatedAt"),
                        "owner": s.get("createdBy"),
                        "resources_count": s.get("resourcesCount"),
                    }
                )
            token = r.get("NextToken")
            if not token:
                break
    except Exception as e:
        errors["spaces"] = repr(e)

    # Flows (run_count + created time; owner needs describe-flow, omitted here)
    try:
        token = None
        while True:
            kw = {"AwsAccountId": acct, "MaxResults": 100}
            if token:
                kw["NextToken"] = token
            r = qs_client.list_flows(**kw)
            for f in r.get("FlowSummaryList", []):
                flows_total += 1
                if not _keep(f.get("CreatedTime")):
                    continue
                flows.append(
                    {
                        "flow_id": f.get("FlowId"),
                        "name": f.get("Name"),
                        "description": f.get("Description"),
                        "created_at": f.get("CreatedTime"),
                        "updated_at": f.get("LastUpdatedTime"),
                        "publish_state": f.get("PublishState"),
                        "run_count": f.get("RunCount"),
                        "user_count": f.get("UserCount"),
                    }
                )
            token = r.get("NextToken")
            if not token:
                break
    except Exception as e:
        errors["flows"] = repr(e)

    # Agents (name + created date; owner/instruction via describe-agent elsewhere)
    try:
        token = None
        while True:
            kw = {"AwsAccountId": acct, "MaxResults": 100}
            if token:
                kw["NextToken"] = token
            r = qs_client.list_agents(**kw)
            for a in r.get("AgentSummaries", []):
                agents_total += 1
                if not _keep(a.get("CreatedAt")):
                    continue
                agents.append(
                    {
                        "agent_id": a.get("AgentId"),
                        "name": a.get("Name"),
                        "description": a.get("Description"),
                        "created_at": a.get("CreatedAt"),
                        "updated_at": a.get("UpdatedAt"),
                    }
                )
            token = r.get("NextToken")
            if not token:
                break
    except Exception as e:
        errors["agents"] = repr(e)

    result = {
        "report_date": datetime.utcnow().strftime("%Y-%m-%d"),
        "date_range": {
            "from": datetime.utcfromtimestamp(start_time / 1000).strftime("%Y-%m-%d"),
            "to": datetime.utcfromtimestamp(end_time / 1000).strftime("%Y-%m-%d"),
            "all_time": all_time,
        },
        # '*_created' = created within the date range (or all, if all_time=true)
        "spaces_created": len(spaces),
        "flows_created": len(flows),
        "agents_created": len(agents),
        # '*_total' = full count regardless of date range
        "spaces_total": spaces_total,
        "flows_total": flows_total,
        "agents_total": agents_total,
        "spaces": spaces,
        "flows": flows,
        "agents": agents,
        "note": "'*_created' counts assets whose created_at is within from/to "
        "(pass all_time=true to count every asset). '*_total' is the full count. "
        "'research' has no asset list API; count distinct research_id from "
        "feedback-logs / RESEARCH hours from agent-hours-logs instead. "
        "Flow/agent owner needs describe-flow/describe-agent.",
    }
    if errors:
        result["errors"] = errors
    return result
