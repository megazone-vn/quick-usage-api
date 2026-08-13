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

```bash
zip quick_package.zip quick_lambda.py
aws lambda update-function-code --function-name quick-dashboard-api --zip-file fileb://quick_package.zip
rm quick_package.zip
```

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

### Query Parameters

| Param | Format | Mô tả |
|-------|--------|--------|
| `from` | `YYYY-MM-DD` | Ngày bắt đầu (default: 7 ngày trước) |
| `to` | `YYYY-MM-DD` | Ngày kết thúc (default: hôm nay) |
| `user` | email | Filter theo user (partial match) |
| `limit` | number | Số records tối đa (default: 100) |

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
| IAM Role | `quick-dashboard-lambda-role` | CloudWatch Logs access |

## Chi phí

Dưới 50,000 users + dưới 1M requests/tháng: **$0/tháng** (free tier).
