# Releasing Smitline

How a version of Smitline is released, step by step, for maintainers and for coding agents working for one. Each step gives the command to run and when to stop.

## Who can release

**Only the maintainers can release: the repository's admins.** GitHub enforces this, not this document:

- **A release is a version tag.** Pushing a tag `v*` is what publishes anything (see [What a release publishes](#what-a-release-publishes)).
- **Only admins can push a `v*` tag.** The repository's "release tags" ruleset blocks creating, moving or deleting `v*` tags for everyone else.
- **Contributors' code never runs with publishing rights.** Contributors propose changes from forks. Their pull requests run the tests with a read-only token and no secrets, and no workflow runs fork code with more (`pull_request_target` is not used).

**Coding agents:** before you bump the version, tag, publish or create a release, check both of these. If either fails, stop and say why.

1. **The person you work for asked for this release.** A request to fix or build something is not a request to release it.
2. **The account you act through is an admin of the repository.** This must print `true`:

   ```bash
   gh api repos/kaelorlabs/smitline --jq .permissions.admin
   ```

**Contributors:** don't change version numbers in a pull request. Add what your change does to the "Unreleased" section of [CHANGELOG.md](../CHANGELOG.md) instead; the maintainers set the version when they release.

**Keeping it that way (maintainers):**

- Give write or admin access only to maintainers.
- Check now and then that nobody else has access, and that the "release tags" ruleset is still active:

  ```bash
  gh api repos/kaelorlabs/smitline/collaborators --jq '.[] | "\(.login): \(.role_name)"'
  gh api repos/kaelorlabs/smitline/rulesets --jq '.[] | "\(.name): \(.target), \(.enforcement)"'
  ```

## What a release publishes

A `v*` tag runs [`.github/workflows/images.yml`](../.github/workflows/images.yml) on the tagged commit:

1. **`test`:** the same tests as every pull request.
2. **`publish`:** builds and pushes both images, for `linux/amd64` and `linux/arm64`:
   - `ghcr.io/kaelorlabs/smitline`
   - `ghcr.io/kaelorlabs/smitline-meeting`

   Each is tagged with the version (`0.3.13`), the minor version (`0.3`), `latest`, and the commit (`sha-…`).
3. **`mcp-registry`:** lists the version in the official MCP Registry from `server.json` ([MCP directories](mcp-directories.md)).

Nothing is published to npm or PyPI. The GitHub release page is made by hand, in step 6.

## Versions

- **Where the version lives:** `package.json` holds it. Seventeen files carry it in all: package manifests, `server.json` and its image tag, the SDKs, the MCPB bundle, `tools.json`, READMEs and `SETUP.md`. [`scripts/release-version.mjs`](../scripts/release-version.mjs) lists them.
- **Never edit those files by hand:** the script changes them all at once. `npm test` fails when they disagree, so a half-done bump never reaches a tag.
- **Numbering:** each release so far raised the last number (0.3.12, then 0.3.13). A bigger step is the maintainers' call.

## Steps

The examples release `0.3.14`; use the real version throughout.

### 1. Check that `main` is ready

```bash
git fetch origin
git switch -c release-0.3.14 origin/main
gh run list --branch main --limit 3
```

Stop if:

- the latest runs on `main` failed;
- the "Unreleased" section of `CHANGELOG.md` is empty, or misses a change that was merged.

Every merged change that users would notice should have a line there.

### 2. Bump the version

```bash
node scripts/release-version.mjs 0.3.14
```

This moves all seventeen files and turns `## Unreleased` into `## 0.3.14 — <today>` (add `--date YYYY-MM-DD` for another day).

It checks everything first and changes nothing if:

- the files already disagree;
- the new version is not after the current one;
- the "Unreleased" section is missing or empty.

### 3. Test

```bash
npm ci
npm test
(cd packages/sdk-python && python3 -m unittest discover -s tests)
node packages/mcpb/build.mjs --check
```

- `build.mjs --check` fails if `packages/mcpb/server/tools.json` no longer matches the MCP server.
- When the release changes anything under `meeting-runtime/` or `joinly/`, also run the runtime suite in the meeting image ([AGENTS.md](../AGENTS.md#tests)).

Stop on any failure. One test, `local MCP sessions … DELETE`, can fail now and then with `EPIPE` for reasons unrelated to any change. Run it again once; if it fails again, stop.

### 4. Open the release pull request

Commit only the version bump, with the message `Release 0.3.14`.

```bash
git commit -am "Release 0.3.14"
git push -u origin release-0.3.14
gh pr create --base main --title "Release 0.3.14: <the release's main change>" --body "<body>"
```

The body says, in this order:

1. that this is the version bump;
2. the pull requests the release brings, each with one line on what it changes;
3. the tests from step 3;
4. "After merge: tag `v0.3.14` on the merge commit."

Merge it once its checks pass, as a merge commit, like every pull request here.

### 5. Tag the merge commit

Find the merge commit, check it, and tag it:

```bash
merge=$(gh pr view <number> --json mergeCommit --jq .mergeCommit.oid)
git fetch origin
git show --no-patch --format='%H %s' "$merge"
git tag v0.3.14 "$merge"
git push origin v0.3.14
```

- The `git show` line must print "Merge pull request #<number> …"; stop if it doesn't.
- The tag must be on that commit, so the images contain exactly what was reviewed.
- Only an admin's push of the tag succeeds.

Then watch the run until `test`, `publish` and `mcp-registry` have all passed:

```bash
gh run list --workflow images.yml --limit 3
gh run watch <run id>
```

### 6. Create the GitHub release page

The page is titled "Smitline 0.3.14", and its notes are the version's section of the changelog:

```bash
awk '/^## 0\.3\.14 /{found=1; next} /^## /{found=0} found' CHANGELOG.md > release-notes.md
gh release create v0.3.14 --title "Smitline 0.3.14" --notes-file release-notes.md
rm release-notes.md
```

### 7. After the release

- **Self-hosted installs** get the new version from the `latest` image, or by pinning `0.3.14` ([SETUP.md](../SETUP.md)).
- **Hosted services** built on Smitline follow their own steps to take up the new tag.

## If something goes wrong

- **Before the tag:** fix it on the release branch or on `main`. Nothing has been published.
- **The run on the tag failed, or a release has a bug:** don't move or delete the tag. Images may already be published under it. Fix it on `main` and release the next version.
- **Tagged the wrong commit:** the same applies: leave the tag and release the next version from the right commit.
