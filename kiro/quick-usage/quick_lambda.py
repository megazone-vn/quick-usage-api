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
