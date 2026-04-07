# Git Push Guide — VALR Grid Bot v2

## Quick Reference

```bash
cd /home/admin/.openclaw/workspace/bots/valr-grid-bot-v2

# Standard push (SSH)
git push origin main

# If SSH agent not running
eval $(ssh-agent -s) && ssh-add ~/.ssh/id_ed25519 && git push origin main

# Verbose push (for debugging)
git push --verbose origin main
```

---

## SSH Authentication

### Key Location
- **Private key:** `~/.ssh/id_ed25519`
- **Public key:** `~/.ssh/id_ed25519.pub`
- **Key comment:** `blake@valr-grid-bot`

### Test SSH Connection
```bash
ssh -T git@github.com
# Expected: "Hi Blake1863! You've successfully authenticated, but GitHub does not provide shell access."
```

### Add Key to Agent (if needed)
```bash
eval $(ssh-agent -s)
ssh-add ~/.ssh/id_ed25519
```

---

## Common Issues & Solutions

### 1. Push Hangs/Timeouts

**Cause:** Multiple concurrent `git push` processes competing for resources.

**Solution:**
```bash
# Kill any stale git/ssh processes first
pkill -f "git.*push" || true
pkill -f "pack-objects" || true
pkill -f "git-receive-pack" || true

# Wait a moment
sleep 2

# Then push
git push origin main
```

**Prevention:** Never run multiple `git push` commands simultaneously. Wait for each push to complete before starting another.

---

### 2. Large File Warnings

**Warning:**
```
remote: warning: File bots/valr-grid-bot/target/debug/deps/xxx is 52.30 MB;
this is larger than GitHub's recommended maximum file size of 50.00 MB
```

**Cause:** Build artifacts (`target/` directory) are being tracked in git.

**Solution:** Ensure `target/` is in `.gitignore`:

```bash
# Check .gitignore
cat .gitignore | grep target

# If missing, add it:
echo "target/" >> .gitignore
git add .gitignore
git commit -m "chore: add target/ to .gitignore"
git push origin main
```

**Remove already-tracked build artifacts:**
```bash
# Remove target/ from git history (keeps local files)
git rm -r --cached target/
git commit -m "chore: remove build artifacts from git"
git push origin main
```

---

### 3. "Agent has no identities"

**Cause:** SSH agent not running or key not loaded.

**Solution:**
```bash
# Start agent and add key
eval $(ssh-agent -s)
ssh-add ~/.ssh/id_ed25519

# Verify key is loaded
ssh-add -l
# Expected: "256 SHA256:xxx blake@valr-grid-bot (ED25519)"
```

---

### 4. Push Aborted Mid-Transfer

**Cause:** Network timeout, resource exhaustion, or SIGKILL from system.

**Solution:**
```bash
# Increase Git buffer for large pushes
git config --global http.postBuffer 524288000  # 500MB

# Use SSH with keepalive
GIT_SSH_COMMAND="ssh -o ServerAliveInterval=30 -o ServerAliveCountMax=3" git push origin main

# Or push with progress monitoring
git push --progress origin main 2>&1 | tee /tmp/git-push.log
```

---

## Best Practices

### Before Pushing
1. **Check for stale processes:**
   ```bash
   ps aux | grep -E "git.*push|pack-objects" | grep -v grep
   ```

2. **Verify working tree is clean:**
   ```bash
   git status
   ```

3. **Ensure only intended files are staged:**
   ```bash
   git diff --cached --name-only
   ```

### During Push
- **Wait for completion** — don't start another push until the first finishes
- **Monitor progress** — use `--progress` flag for visibility
- **Use background jobs for large pushes:**
  ```bash
  git push origin main &
  # Monitor with: ps aux | grep git
  ```

### After Push
1. **Verify on GitHub:** Check https://github.com/Blake1863/valr-grid-bot-v2/commits/main
2. **Clean up background jobs:** `wait` or check with `jobs`

---

## Repository Info

- **Remote:** `git@github.com:Blake1863/valr-grid-bot-v2.git`
- **Branch:** `main`
- **Auto-commit:** Enabled via cron (`*/5 * * * *`)
- **Auto-commit script:** `scripts/auto-commit.sh`

---

## Troubleshooting Checklist

- [ ] SSH key loaded: `ssh-add -l`
- [ ] Can connect to GitHub: `ssh -T git@github.com`
- [ ] No stale git processes: `ps aux | grep git`
- [ ] Working tree clean: `git status`
- [ ] Remote configured: `git remote -v`
- [ ] On correct branch: `git branch`

---

## Emergency Reset

If git repo gets corrupted:

```bash
# Fetch fresh from remote
git fetch origin

# Reset local to match remote
git reset --hard origin/main

# Clean untracked files (careful!)
git clean -fd
```

---

*Last updated: 2026-04-07*
