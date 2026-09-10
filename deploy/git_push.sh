#!/bin/bash
# 用 git credential 提供 token, 一次性完成 push, 避免反复手打 token 出错
# 用法: ./git_push.sh [commit message]
#   token 放 /mnt/gpustack/.gh_token (单行, ghp_xxx); 该文件已 gitignore.
set -e
cd /mnt/gpustack
MSG="${1:-二开修复: 详见 git diff}"

# 从文件读 token (避免 shell 参数乱码)
TOKEN=$(cat /mnt/gpustack/.gh_token 2>/dev/null | tr -d ' \n')

if [ -z "$TOKEN" ]; then
    echo "ERROR: no token in /mnt/gpustack/.gh_token"
    echo "  put a GitHub PAT there (single line, ghp_xxx), then retry."
    exit 1
fi

# 用 GIT_ASKPASS 提供 token (不走 insteadOf, 直连 github.com;
# 二开修复: 原版 set-url 里 \$TOKE 变量名不存在且引号未闭合, 一执行
# 就把 remote URL 改坏. 现在不再改 remote, 只通过 askpass 注入凭据.)
cat > /tmp/askpass-gh.sh <<'EOF'
#!/bin/bash
echo "$GH_TOKEN"
EOF
chmod +x /tmp/askpass-gh.sh

echo "=== stage + commit (如有未提交改动) ==="
if ! git diff --quiet HEAD 2>/dev/null || [ -n "$(git ls-files --others --exclude-standard)" ]; then
    git add -A
    git commit -m "$MSG" || true
else
    echo "nothing to commit"
fi

echo "=== push ==="
GIT_ASKPASS=/tmp/askpass-gh.sh GH_TOKEN="$TOKEN" \
    git -c "credential.helper=" push -u origin main 2>&1
rc=$?
echo "=== push exit=$rc ==="
exit $rc
