#!/bin/sh
# Moves the site from suncal.github.io/tokentrim/ to https://tokentrim.app/.
# Run only after Cloudflare DNS for tokentrim.app points at GitHub Pages, as DNS only (grey cloud):
#   A @ 185.199.108.153 / .109 / .110 / .111, CNAME www -> suncal.github.io
# Then, once GitHub has issued the certificate: scripts/switch-to-custom-domain.sh https
# DRY_RUN=1 skips the DNS check and stops after the local rewrite, with nothing committed.
set -e
DOMAIN=tokentrim.app
REPO=suncal/tokentrim
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if [ "$1" = https ]; then
  gh api -X PUT "repos/$REPO/pages" -F https_enforced=true >/dev/null
  curl -fsS -o /dev/null -m 20 "https://$DOMAIN/benchmarks/"
  echo "HTTPS enforced and https://$DOMAIN/ is serving."
  exit 0
fi

if [ -z "$DRY_RUN" ]; then
  for ip in 108 109 110 111; do
    dig @1.1.1.1 +short "$DOMAIN" | grep -qx "185\.199\.$ip\.153" || {
      echo "DNS not on GitHub Pages yet: $DOMAIN lacks 185.199.$ip.153 (is the record still proxied?)"; exit 1; }
  done
  dig @1.1.1.1 +short CNAME "www.$DOMAIN" | grep -qx 'suncal\.github\.io\.' || {
    echo "DNS not on GitHub Pages yet: www.$DOMAIN must be a DNS-only CNAME to suncal.github.io"; exit 1; }
  [ -z "$(git -C "$ROOT" status --porcelain)" ] || { echo "Working tree not clean"; exit 1; }
  git -C "$ROOT" pull -q --ff-only
fi

# Anchored to the host, so github.com/suncal/tokentrim and raw.githubusercontent links stay as they are.
FILES="$(grep -rlE 'suncal\.github\.io|/tokentrim/' "$ROOT/docs" "$ROOT/content" "$ROOT/README.md" "$ROOT/CHANGELOG.md" "$ROOT/tokentrim/live.html")"
echo "$FILES" | xargs perl -pi -e "
  s{https://suncal\.github\.io/tokentrim}{https://$DOMAIN}g;
  s{suncal\.github\.io%2Ftokentrim}{$DOMAIN}g;
  s{suncal\.github\.io/tokentrim}{$DOMAIN}g;
  s{href=(\\\\?\")/tokentrim/}{href=\$1/}g;
"
perl -pi -e "s{^BASE = \"[^\"]*\"}{BASE = \"https://$DOMAIN\"}" "$ROOT/jarvis_pages.py"
echo "$DOMAIN" > "$ROOT/docs/CNAME"

[ -z "$DRY_RUN" ] || { echo "Dry run: rewrote $(echo "$FILES" | wc -l | tr -d ' ') files, nothing committed."; exit 0; }

git -C "$ROOT" add docs content README.md CHANGELOG.md tokentrim/live.html jarvis_pages.py
git -C "$ROOT" commit -qm "Move site to tokentrim.app custom domain

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git -C "$ROOT" pull -q --rebase   # the JARVIS bot commits to main too
git -C "$ROOT" push -q
gh api -X PUT "repos/$REPO/pages" -f cname="$DOMAIN" >/dev/null
echo "Done. When the certificate is ready, run: scripts/switch-to-custom-domain.sh https"
