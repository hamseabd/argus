#!/usr/bin/env bash
# Publish argus-dev's main to the public repository as one release commit.
#
#   scripts/release.sh v1
#
# The release commit holds origin/main's tree without docs/ (working notes)
# and has at most one parent: the previous release on the public main. No
# argus-dev commit is ever pushed, so the public history is one commit per
# release. The tree is refused if it names any private repository of the
# owner other than argus-dev itself, in a file or a path. The version must be
# new: an existing tag is never moved. The public main must be the previous
# release, so nothing pushed there by hand is overwritten. Every check fails
# closed. A main that is not canary's commit is released with a warning.
# The last line printed is the release SHA, for pins.
#
# Needs gh logged in as the owner, with a credential that sees every private
# repository, and push access to the public repository.
# ARGUS_RELEASE_REMOTE overrides the public remote, for tests.
set -euo pipefail
# One locale for every scan, so [[:alnum:]] is ASCII in git grep and grep alike.
export LC_ALL=C

readonly OWNER=hamseabd
readonly DEV_REPO=argus-dev
readonly REMOTE="${ARGUS_RELEASE_REMOTE:-https://github.com/$OWNER/argus.git}"
readonly LEFT_OUT=docs
readonly LIMIT=1000

die() {
  echo "release: $*" >&2
  exit 1
}

version="${1:-}"
[[ "$version" =~ ^v[0-9]+(\.[0-9]+){0,2}$ ]] || die "usage: scripts/release.sh v<major>[.<minor>[.<patch>]]"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

refs="$(git ls-remote "$REMOTE")" || die "could not list the refs of $REMOTE"
if awk -v ref="refs/tags/$version" '$2 == ref { found = 1 } END { exit !found }' <<<"$refs"; then
  die "$version already exists on $REMOTE; a released tag is never moved"
fi
public_main="$(awk '$2 == "refs/heads/main" { print $1 }' <<<"$refs")"
if [[ -n "$public_main" ]] &&
  ! awk -v sha="$public_main" '$1 == sha && $2 ~ /^refs\/tags\/v[0-9]/ { found = 1 } END { exit !found }' <<<"$refs"; then
  die "the public main $public_main is not a tagged release; a release would bury it, so reconcile it first"
fi

git fetch --quiet origin main
source_sha="$(git rev-parse FETCH_HEAD)"

# A version should be the commit that soaked on canary. Releasing another is
# the maintainer's call, so it is reported, not refused.
canary_refs="$(git ls-remote --tags origin canary)" || die "could not read the canary tag on origin"
canary="$(awk '$2 == "refs/tags/canary^{}" { peeled = $1 } $2 == "refs/tags/canary" { tag = $1 }
  END { print peeled ? peeled : tag }' <<<"$canary_refs")"
[[ "$canary" == "$source_sha" ]] ||
  echo "release: warning: canary is at ${canary:-no commit}, not main $source_sha; this version has not soaked" >&2

# The tree to publish: origin/main's top level less docs/, written straight
# to the object store, so the checkout, its index, and HEAD play no part.
# Each step writes a file, so a failure stops the release instead of
# vanishing inside a pipeline.
git ls-tree "$source_sha" >"$work/top"
awk -F '\t' -v out="$LEFT_OUT" '$2 != out' "$work/top" >"$work/published-top"
tree="$(git mktree <"$work/published-top")"

# Every private repository name but this one's, from GitHub at release time,
# never from this file. The listing must be complete, so the release stops
# when gh fails, is logged in as anyone but the owner, or lists a set that
# cannot be all of them: none but argus-dev (a credential scoped to it), or
# as many as the limit (cut short).
login="$(gh api user --jq .login)" || die "could not read the gh login; refusing to release unchecked"
[[ "$login" == "$OWNER" ]] ||
  die "gh is logged in as $login, not $OWNER, so it may not see every private repository; refusing to release unchecked"
names="$(gh repo list "$OWNER" --visibility private --limit "$LIMIT" --json name --jq '.[].name')" ||
  die "could not list $OWNER's private repositories with gh; refusing to release unchecked"
grep -q -x -F "$DEV_REPO" <<<"$names" ||
  die "gh did not list $DEV_REPO among $OWNER's private repositories; refusing to release unchecked"
(($(wc -l <<<"$names") < LIMIT)) ||
  die "the gh listing reached its limit of $LIMIT, so it may be cut short; refusing to release unchecked"
others="$(grep -v -x -F "$DEV_REPO" <<<"$names")" ||
  die "gh listed no private repository but $DEV_REPO, so its credential cannot see them all; refusing to release unchecked"

# One extended regex per name, in any case: '-', '_', ' ', or nothing joins
# its words, and the name must not run on into a letter or digit, so for
# my-repo, my_repo, My Repo, MyRepo, and my-repo/ match but my-repos does
# not. A separator at either end stays literal: Notes- matches notes-, never
# the word notes. GitHub names are [A-Za-z0-9._-], so '.' is the only one to
# escape. Binary files are scanned too and reported as matching, never skipped.
sed -e 's/\./\\./g' \
  -e ':join' -e 's/\([[:alnum:]]\)[-_]\([[:alnum:]]\)/\1[-_ ]?\2/' -e 't join' \
  -e 's/.*/(^|[^[:alnum:]])&([^[:alnum:]]|$)/' <<<"$others" >"$work/patterns"

git -c core.quotePath=false ls-tree -r --name-only "$tree" >"$work/paths" ||
  die "the private-name scan failed; refusing to release unchecked"
set +e
in_files="$(git grep -i -E -f "$work/patterns" "$tree" --)"
files_status=$?
in_paths="$(grep -i -E -f "$work/patterns" "$work/paths")"
paths_status=$?
set -e
((files_status <= 1 && paths_status <= 1)) ||
  die "the private-name scan failed; refusing to release unchecked"
if ((files_status == 0 || paths_status == 0)); then
  [[ -z "$in_files" ]] || sed "s/$tree://" <<<"$in_files" >&2
  [[ -z "$in_paths" ]] || sed 's/^/path: /' <<<"$in_paths" >&2
  die "the release tree names a private repository (above); fix it on argus-dev main first"
fi

message="release: $version from $DEV_REPO $(git rev-parse --short "$source_sha")"
if [[ -n "$public_main" ]]; then
  git fetch --quiet "$REMOTE" main
  release_sha="$(git commit-tree "$tree" -p "$public_main" -m "$message")"
else
  release_sha="$(git commit-tree "$tree" -m "$message")"
fi

# Atomic and never forced: if the public main moved since it was checked,
# the push is not a fast-forward and nothing lands.
git push --quiet --atomic "$REMOTE" "$release_sha:refs/heads/main" "$release_sha:refs/tags/$version"
echo "$release_sha"
