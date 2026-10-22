#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPOSITORY_NAME="${1:-entra-obo-github-poc}"
cd "$PROJECT_ROOT"

if ! command -v gh >/dev/null 2>&1; then
  echo "GitHub CLI (gh) is required. Install it with: brew install gh" >&2
  exit 1
fi
if ! gh auth status >/dev/null 2>&1; then
  echo "Authenticate GitHub CLI first with: gh auth login" >&2
  exit 1
fi

GH_LOGIN="$(gh api user --jq .login)"
GH_USER_ID="$(gh api user --jq .id)"
FULL_REPOSITORY="${GH_LOGIN}/${REPOSITORY_NAME}"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git init --initial-branch=main
fi
if git remote get-url origin >/dev/null 2>&1; then
  echo "This checkout already has an origin remote. Inspect it before publishing: $(git remote get-url origin)" >&2
  exit 1
fi
if ! git check-ignore -q .env; then
  echo ".env is not ignored by git; refusing to publish." >&2
  exit 1
fi

git config user.name "$GH_LOGIN"
git config user.email "${GH_USER_ID}+${GH_LOGIN}@users.noreply.github.com"
git add -A

while IFS= read -r tracked_path; do
  case "$tracked_path" in
    .env|.env.*)
      if [[ "$tracked_path" != ".env.example" ]]; then
        echo "Refusing to publish environment file: $tracked_path" >&2
        exit 1
      fi
      ;;
    secrets/*|*.pem|*.sqlite3|*.db)
      echo "Refusing to publish sensitive/local file: $tracked_path" >&2
      exit 1
      ;;
  esac
done < <(git diff --cached --name-only)

if gh repo view "$FULL_REPOSITORY" >/dev/null 2>&1; then
  echo "Repository ${FULL_REPOSITORY} already exists. Choose another name or review it manually." >&2
  exit 1
fi

if ! git rev-parse --verify HEAD >/dev/null 2>&1 || ! git diff --cached --quiet; then
  git commit -m "Document and publish Entra OBO GitHub POC"
fi

gh repo create "$FULL_REPOSITORY" --private --source="$PROJECT_ROOT" --remote=origin --push
echo "Published private repository: https://github.com/${FULL_REPOSITORY}"
