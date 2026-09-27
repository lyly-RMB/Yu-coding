# S08 权限冒烟：对目标群实拉一页飞书历史消息
# 需要环境变量：
#   PROJECT_LENS_FEISHU_APP_ID / PROJECT_LENS_FEISHU_APP_SECRET
#   PROJECT_LENS_SMOKE_CHAT_ID  （目标群 chat_id，如 oc_xxx）
# 可选：PROJECT_LENS_FEISHU_API_BASE_URL（默认 https://open.feishu.cn）

param(
    [string]$ChatId = $env:PROJECT_LENS_SMOKE_CHAT_ID
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not $env:PROJECT_LENS_FEISHU_APP_ID -or -not $env:PROJECT_LENS_FEISHU_APP_SECRET) {
    Write-Error "缺少 PROJECT_LENS_FEISHU_APP_ID / PROJECT_LENS_FEISHU_APP_SECRET"
}
if (-not $ChatId) {
    Write-Error "缺少 PROJECT_LENS_SMOKE_CHAT_ID（或 -ChatId）"
}

$code = @"
from project_lens.integrations.feishu.http_adapter import FeishuTenantTokenProvider
from project_lens.integrations.feishu.messages_client import FeishuMessagesClient
from project_lens.config import settings

token = FeishuTenantTokenProvider(
    app_id=settings.feishu_app_id,
    app_secret=settings.feishu_app_secret,
    base_url=settings.feishu_api_base_url,
)
client = FeishuMessagesClient(
    token_provider=token,
    base_url=settings.feishu_api_base_url,
)
page = client.list_chat_history(chat_id=r'''$ChatId''', page_size=5)
print('available=', page.available)
print('unavailable_reason=', page.unavailable_reason)
print('count=', len(page.items))
print('has_more=', page.has_more)
for item in page.items:
    print(item.message_id, item.occurred_at.isoformat(), item.text[:80].replace(chr(10), ' '))
if not page.available:
    raise SystemExit(2)
if not page.items:
    print('WARN: available but empty page (window/permissions may still be ok)')
"@

python -c $code
if ($LASTEXITCODE -ne 0) {
    Write-Error "S08 权限冒烟失败（exit=$LASTEXITCODE）。检查机器人在群 + im:message.group_msg 等读历史权限。"
}
Write-Host "S08 权限冒烟通过：已实拉至少一页（或空页但 available=true）。"
