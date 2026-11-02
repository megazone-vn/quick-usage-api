# Quick Usage Dashboard API

API dashboard để theo dõi usage của Amazon Quick (chat, feedback, agent hours, index). Deploy trên AWS Lambda + API Gateway + Cognito JWT auth.

## Current Deployment (MegazoneCloud account)

| Thông tin | Giá trị |
|-----------|---------|
| API URL | `https://r9y9wybd2a.execute-api.us-east-1.amazonaws.com/prod` |
| User Pool ID | `us-east-1_FHSe8wBcP` |
| Client ID | `7gnlp24064tsqgpbiqdsk0dkv0` |
| Region | `us-east-1` |
| Test user | `test@kiro-dashboard.com` / `Test@1234` |

> ⚠️ Giá trị trên chỉ dùng cho account hiện tại. Deploy ở account khác sẽ sinh ra URL và Client ID mới.

---

## Deploy

### Bước 1: Deploy CloudFormation

```bash
aws cloudformation deploy \
  --template-file template.yaml \
  --stack-name quick-dashboard-api \
  --capabilities CAPABILITY_NAMED_IAM
```

### Bước 2: Upload Lambda code

> ⚠️ **boto3 mới bắt buộc cho endpoint enrich.** Các API agent của Quick Suite
> (`list_agents`, `describe_agent`, `list_spaces`, ...) chỉ có trong boto3 đủ mới.
> boto3 **bundled sẵn trong Lambda runtime quá cũ** -> sẽ báo
> `'QuickSight' object has no attribute 'list_agents'` và `/quick/agents` trả
> `agent_name=null`. Phải **đóng gói boto3 mới kèm code** (không dùng bản bundled):

```bash
# Package kèm boto3 mới (bắt buộc cho /quick/agents enrich)
mkdir build && pip install boto3 --target build && cp quick_lambda.py build/
cd build && zip -r ../quick_package.zip . && cd ..
aws lambda update-function-code --function-name quick-dashboard-api --zip-file fileb://quick_package.zip
rm -rf build quick_package.zip
```

> Nếu KHÔNG cần enrich (chỉ endpoint đọc log), bản tối giản vẫn chạy:
> `zip quick_package.zip quick_lambda.py` rồi update-function-code như cũ.

> **Env var:** đặt `QS_ACCOUNT_ID=<account-id>` cho Lambda để bỏ phụ thuộc STS
> khi resolve account cho các call QuickSight (không bắt buộc nhưng khuyến nghị).

### Bước 3: Lấy thông tin stack

```bash
aws cloudformation describe-stacks --stack-name quick-dashboard-api \
  --query "Stacks[0].Outputs[*].[OutputKey,OutputValue]" --output table
```

### Bước 4: Tạo user

```bash
aws cognito-idp admin-create-user \
  --user-pool-id <USER_POOL_ID> \
  --username user@company.com \
  --user-attributes Name=email,Value=user@company.com Name=email_verified,Value=true \
  --temporary-password "TempPass@123" \
  --message-action SUPPRESS

aws cognito-idp admin-set-user-password \
  --user-pool-id <USER_POOL_ID> \
  --username user@company.com \
  --password "YourPassword@123" \
  --permanent
```

### Bước 5: Bật Quick CloudWatch Logs (nếu chưa bật)

```bash
aws logs put-delivery-source --name quicksight-chat-logs \
  --resource-arn "arn:aws:quicksight:us-east-1:<ACCOUNT_ID>:account/<ACCOUNT_ID>" \
  --log-type CHAT_LOGS

aws logs put-delivery-source --name quicksight-feedback-logs \
  --resource-arn "arn:aws:quicksight:us-east-1:<ACCOUNT_ID>:account/<ACCOUNT_ID>" \
  --log-type FEEDBACK_LOGS

aws logs put-delivery-source --name quicksight-agent-hours-logs \
  --resource-arn "arn:aws:quicksight:us-east-1:<ACCOUNT_ID>:account/<ACCOUNT_ID>" \
  --log-type AGENT_HOURS_LOGS

aws logs put-delivery-source --name quicksight-index-usage-logs \
  --resource-arn "arn:aws:quicksight:us-east-1:<ACCOUNT_ID>:account/<ACCOUNT_ID>" \
  --log-type INDEX_USAGE_LOGS
```

Sau đó tạo log groups + delivery destinations + deliveries. Xem [AWS docs](https://docs.aws.amazon.com/quick/latest/userguide/monitoring-quicksuite-chat-feedback-cloudwatch.html).

---

## Cách gọi API

### Lấy token (curl thuần)

```bash
curl -s -X POST "https://cognito-idp.us-east-1.amazonaws.com/" \
  -H "Content-Type: application/x-amz-json-1.1" \
  -H "X-Amz-Target: AWSCognitoIdentityProviderService.InitiateAuth" \
  -d '{"AuthFlow":"USER_PASSWORD_AUTH","ClientId":"<CLIENT_ID>","AuthParameters":{"USERNAME":"<email>","PASSWORD":"<password>"}}'
```

Response chứa `AuthenticationResult.IdToken` — dùng **IdToken** (KHÔNG phải AccessToken).

> **Lưu ý quan trọng:**
> - Phải dùng prefix `Bearer ` trước token trong header `Authorization`.
> - Khi URL có nhiều query param (dấu `&`), bọc URL trong dấu nháy để shell không hiểu nhầm: `"...?user=x&from=y"`.
> - Path phải đầy đủ `/prod/quick/<endpoint>`. Gọi thiếu (vd `/prod/quick`) sẽ báo `Missing Authentication Token`.

### Curl mẫu tất cả endpoints

```bash
# ═══ ALL DATA ═══
curl -s -H "Authorization: Bearer <TOKEN>" "https://<API_URL>/prod/quick/all"

# ═══ CHAT ═══
curl -s -H "Authorization: Bearer <TOKEN>" "https://<API_URL>/prod/quick/chat"

# ═══ FEEDBACK ═══
curl -s -H "Authorization: Bearer <TOKEN>" "https://<API_URL>/prod/quick/feedback"

# ═══ AGENT HOURS ═══
curl -s -H "Authorization: Bearer <TOKEN>" "https://<API_URL>/prod/quick/agent-hours"

# ═══ INDEX USAGE ═══
curl -s -H "Authorization: Bearer <TOKEN>" "https://<API_URL>/prod/quick/index-usage"

# ═══ USERS ═══
curl -s -H "Authorization: Bearer <TOKEN>" "https://<API_URL>/prod/quick/users"

# ═══ CONVERSATIONS ═══
curl -s -H "Authorization: Bearer <TOKEN>" "https://<API_URL>/prod/quick/conversations"
curl -s -H "Authorization: Bearer <TOKEN>" "https://<API_URL>/prod/quick/conversations?id=<CONVERSATION_ID>"

# ═══ AGENTS (rollup theo agent_id) ═══
curl -s -H "Authorization: Bearer <TOKEN>" "https://<API_URL>/prod/quick/agents"
# + owner / welcome / instruction (describe-agent, chậm hơn):
curl -s -H "Authorization: Bearer <TOKEN>" "https://<API_URL>/prod/quick/agents?detail=true"
# + breakdown theo TỪNG USER dùng agent đó:
curl -s -H "Authorization: Bearer <TOKEN>" "https://<API_URL>/prod/quick/agents?by_user=true"
# gộp cả hai (đủ info agent theo từng user):
curl -s -H "Authorization: Bearer <TOKEN>" "https://<API_URL>/prod/quick/agents?detail=true&by_user=true"
# lọc theo KHOẢNG THỜI GIAN (hoạt động chat của agent trong khoảng from..to):
curl -s -H "Authorization: Bearer <TOKEN>" "https://<API_URL>/prod/quick/agents?detail=true&by_user=true&from=2026-10-01&to=2026-10-23"
# ALL-TIME (Bảng 3): queries/conversations/users tích lũy TỪ NGÀY TẠO agent:
curl -s -H "Authorization: Bearer <TOKEN>" "https://<API_URL>/prod/quick/agents?all_time=true&detail=true&by_user=true"

# ═══ SPACES & KB (từ index-usage) ═══
curl -s -H "Authorization: Bearer <TOKEN>" "https://<API_URL>/prod/quick/spaces"

# ═══ ASSETS (số TẠO + ngày tạo + owner, từ QuickSight API) ═══
curl -s -H "Authorization: Bearer <TOKEN>" "https://<API_URL>/prod/quick/assets"
# -> tra ve: spaces_created/flows_created/agents_created (TRONG khoang from/to)
#    + spaces_total/flows_total/agents_total (TONG toan bo) + list tung asset
#    (spaces co owner; flows co run_count; agents co created date)
# Loc theo khoang thoi gian (asset tao trong khoang): dung from/to nhu cac endpoint khac
curl -s -H "Authorization: Bearer <TOKEN>" "https://<API_URL>/prod/quick/assets?from=2026-10-01&to=2026-10-23"
# Dem TAT CA asset bat ke ngay tao:
curl -s -H "Authorization: Bearer <TOKEN>" "https://<API_URL>/prod/quick/assets?all_time=true"

# ═══ REPORT (bảng tổng hợp per-user — Bảng 1 + Bảng 2) ═══
curl -s -H "Authorization: Bearer <TOKEN>" "https://<API_URL>/prod/quick/report"
curl -s -H "Authorization: Bearer <TOKEN>" "https://<API_URL>/prod/quick/report?from=2026-09-23&to=2026-10-23"
# 3 phien ban 7/14/30 ngay:
curl -s -H "Authorization: Bearer <TOKEN>" "https://<API_URL>/prod/quick/report?from=2026-10-16&to=2026-10-23"  # 7 ngay
curl -s -H "Authorization: Bearer <TOKEN>" "https://<API_URL>/prod/quick/report?from=2026-10-09&to=2026-10-23"  # 14 ngay
curl -s -H "Authorization: Bearer <TOKEN>" "https://<API_URL>/prod/quick/report?from=2026-09-23&to=2026-10-23"  # 30 ngay

# ═══ FILTER USER ═══
curl -s -H "Authorization: Bearer <TOKEN>" "https://<API_URL>/prod/quick/chat?user=nhinl@megazone.com"
curl -s -H "Authorization: Bearer <TOKEN>" "https://<API_URL>/prod/quick/agent-hours?user=nhinl@megazone.com"

# ═══ FILTER THỜI GIAN ═══
curl -s -H "Authorization: Bearer <TOKEN>" "https://<API_URL>/prod/quick/all?from=2026-06-10&to=2026-06-12"

# ═══ KẾT HỢP USER + THỜI GIAN ═══
curl -s -H "Authorization: Bearer <TOKEN>" "https://<API_URL>/prod/quick/chat?user=nhinl@megazone.com&from=2026-06-10&to=2026-06-12"
```

---

## Endpoints

| Path | Mô tả |
|------|--------|
| `/quick/all` | Toàn bộ data (chat overview, per user, per day, feedback, agent hours, index) |
| `/quick/chat` | Chi tiết chat logs (user_message + system_text_message) |
| `/quick/feedback` | Feedback logs (useful/not useful) |
| `/quick/agent-hours` | Agent hours usage (BRIO, RESEARCH, AUTOMATION) |
| `/quick/index-usage` | Index storage usage |
| `/quick/users` | Usage summary per user |
| `/quick/conversations` | Danh sách conversations |
| `/quick/conversations?id=<id>` | Chi tiết 1 conversation |
| `/quick/agents` | Rollup theo agent_id (queries / conversations / users mỗi agent) + cờ `exists`/`is_deleted` |
| `/quick/agents?detail=true` | + owner (EMAIL) / welcome / instruction (describe-agent) |
| `/quick/agents?by_user=true` | + breakdown theo TỪNG USER dùng agent đó (queries/conversations) |
| `/quick/agents?all_time=true` | queries/conversations/users tích lũy từ ngày tạo agent (Bảng 3) |
| `/quick/spaces` | Rollup Space & Knowledge Base từ index-usage (size, doc count, users) |
| `/quick/report` | Bảng per-user: total_queries, last_activity_date, Created/Used (Bảng 1+2) |
| `/quick/assets` | Số tạo + ngày tạo + owner của spaces/flows/agents (QuickSight API) |


### Query Parameters

| Param | Format | Mô tả |
|-------|--------|--------|
| `from` | `YYYY-MM-DD` | Ngày bắt đầu (default: 7 ngày trước). Áp cho MỌI endpoint: endpoint log lọc theo thời điểm hoạt động, `/quick/assets` lọc theo `created_at` của asset |
| `to` | `YYYY-MM-DD` | Ngày kết thúc (default: hôm nay) |
| `all_time` | `true`/`false` | `/quick/assets`: đếm mọi asset bất kể ngày tạo. `/quick/agents`: tính queries/conversations/users tích lũy từ ngày tạo agent |
| `user` | email | Filter theo user (partial match) |
| `limit` | number | Số records tối đa (default: 100) |
| `detail` | `true`/`false` | (chỉ `/quick/agents`) gọi describe-agent lấy owner(email)/welcome/instruction |
| `by_user` | `true`/`false` | (chỉ `/quick/agents`) thêm breakdown theo từng user dùng agent |
| `enrich` | `true`/`false` | (`/quick/agents` & `/quick/report`) `false` = chỉ log, bỏ qua QuickSight API (nhanh, các cột Created/metadata = null) |

---

## Frontend (JavaScript)

```javascript
// Login
const res = await fetch("https://cognito-idp.us-east-1.amazonaws.com/", {
  method: "POST",
  headers: {
    "Content-Type": "application/x-amz-json-1.1",
    "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth"
  },
  body: JSON.stringify({
    AuthFlow: "USER_PASSWORD_AUTH",
    ClientId: "<CLIENT_ID>",
    AuthParameters: { USERNAME: "email", PASSWORD: "password" }
  })
});
const { AuthenticationResult } = await res.json();
const token = AuthenticationResult.IdToken;

// Gọi API
const data = await fetch("https://<API_URL>/prod/quick/all", {
  headers: { "Authorization": `Bearer ${token}` }
}).then(r => r.json());
```

---

## Xóa tài nguyên

```bash
aws cloudformation delete-stack --stack-name quick-dashboard-api --region us-east-1
```

---

## Tài nguyên AWS

| Tài nguyên | Tên | Mục đích |
|------------|-----|----------|
| Cognito User Pool | `quick-dashboard-users` | Auth |
| Cognito App Client | `quick-dashboard-web` | Frontend login |
| API Gateway | `quick-dashboard-api` | REST endpoint |
| Lambda | `quick-dashboard-api` | Đọc CloudWatch Logs |
| IAM Role | `quick-dashboard-lambda-role` | CloudWatch Logs + QuickSight asset read + Identity Store read |

> **IAM:** ngoài CloudWatch Logs và QuickSight (`ListAgents`/`DescribeAgent`/`ListSpaces`/`ListFlows`/`DescribeFlow`), role cần thêm `sso:ListInstances` + `identitystore:DescribeUser` để resolve Creator UUID của agent/flow sang email (xem `identity-store-policy.json`). Nếu thiếu, `/quick/report` sẽ trả `chat_agents_created` không gán được và `/quick/agents` owner vẫn là UUID.

## Chi phí

Dưới 50,000 users + dưới 1M requests/tháng: **$0/tháng** (free tier).
