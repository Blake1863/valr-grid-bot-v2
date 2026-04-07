#!/bin/bash
# Auto-commit and push script for valr-grid-bot-v2
# Run this after making code changes

set -e

cd /home/admin/.openclaw/workspace/bots/valr-grid-bot-v2

# Check for changes
if git diff --quiet && git diff --cached --quiet; then
    echo "✅ No changes to commit"
    exit 0
fi

# Stage all changes
git add -A

# Create commit with timestamp
TIMESTAMP=$(date +"%Y-%m-%d %H:%M")
git commit -m "chore: auto-commit $TIMESTAMP"

# Push to GitHub
git push origin main

echo "✅ Committed and pushed"
