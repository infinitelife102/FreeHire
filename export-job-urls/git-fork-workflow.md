# Git workflow for `feat/export-job-urls`

Use this when your clone's **`origin`** is your fork (e.g. `infinitelife102/FreeHire`)
and you track the canonical repository as **`upstream`** (`strelov1/freehire`).

## One-time setup

Add the upstream remote (skip if `git remote -v` already lists `upstream`):

```bash
git remote add upstream git@github.com:strelov1/freehire.git
```

That is equivalent to putting this in `.git/config`:

```ini
[remote "upstream"]
	url = git@github.com:strelov1/freehire.git
	fetch = +refs/heads/*:refs/remotes/upstream/*
```

Verify:

```bash
git remote -v
```

You should see both `origin` (your fork) and `upstream` (canonical repo).

## Sync local `main` with upstream

Fetch the latest from the canonical repo:

```bash
git fetch upstream
```

Update your local `main`:

```bash
git checkout main
git merge upstream/main
```

Equivalent while on `main`: `git pull upstream main`.

Optional but recommended — keep your fork's `main` on GitHub in sync too:

```bash
git push origin main
```

## Rebase your feature branch onto updated `main`

```bash
git checkout feat/export-job-urls
git rebase main
```

Resolve any conflicts, then `git rebase --continue` until finished.

## Push your feature branch to your fork

After a rebase, history changed — a normal push is rejected. Use
`--force-with-lease` (safer than `--force`: it refuses if someone else pushed
since your last fetch).

```bash
git fetch origin
git push --force-with-lease origin feat/export-job-urls
```

You do **not** need `git rebase origin` — that is not a valid upstream ref.
Git expects a full ref such as `origin/feat/export-job-urls`. After rebasing
onto `main`, go straight to `push --force-with-lease` unless the remote branch
has commits you still need; in that rare case:

```bash
git fetch origin
git rebase origin/feat/export-job-urls
git push --force-with-lease origin feat/export-job-urls
```

## Quick reference

| Goal | Commands |
| --- | --- |
| Fetch canonical latest | `git fetch upstream` |
| Update local `main` | `git checkout main` → `git merge upstream/main` |
| Update fork's `main` | `git push origin main` |
| Refresh feature branch | `git checkout feat/export-job-urls` → `git rebase main` |
| Publish rebased branch | `git push --force-with-lease origin feat/export-job-urls` |
