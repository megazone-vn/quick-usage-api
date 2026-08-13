# Git Branching & Merge Request Guidelines

## Branch Naming Convention

Khi tạo branch mới, bắt buộc tuân theo quy tắc đặt tên sau:

- **Feature mới:** `feature/<tên-tính-năng>`
  - Ví dụ: `feature/user-authentication`, `feature/payment-integration`
- **Sửa lỗi:** `fixbug/<mô-tả-lỗi>`
  - Ví dụ: `fixbug/login-redirect-error`, `fixbug/null-pointer-exception`

Quy tắc chung:

- Tên branch viết thường, dùng dấu `-` thay cho khoảng trắng
- Không push trực tiếp lên `main` hoặc `develop`
- Mỗi branch chỉ phục vụ một mục đích duy nhất (một feature hoặc một bug fix)

## Merge Request Workflow

**Bắt buộc:** Mọi thay đổi phải được merge vào `develop` trước khi lên `main`.

Quy trình:

1. Tạo branch từ `develop` theo naming convention ở trên
2. Thực hiện code và commit
3. Push branch lên remote
4. Tạo Merge Request (MR) vào branch `develop`
5. Sau khi MR được review và approve, merge vào `develop`
6. Chỉ khi `develop` đã ổn định, mới tạo MR từ `develop` vào `main`

## Lưu ý khi dùng Kiro

- Khi được yêu cầu tạo feature hoặc fix bug, luôn tạo branch mới theo convention
- Không bao giờ commit/push trực tiếp lên `main` hay `develop`
- Sau khi hoàn thành, tạo Merge Request vào `develop` bằng CLI (ví dụ: `gh pr create --base develop`)
- Nếu chưa có branch `develop`, hỏi user trước khi tạo
