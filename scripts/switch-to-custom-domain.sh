#!/bin/sh
# Run only after Namecheap DNS for tokentrim.com points at GitHub Pages
# (A @ 185.199.108-111.153, CNAME www -> suncal.github.io). Moves the site
# from suncal.github.io/tokentrim/ to https://tokentrim.com/.
set -e
cd "$(dirname "$0")/.."
dig +short tokentrim.com | grep -q '^185\.199\.' || { echo "DNS not on GitHub Pages yet"; exit 1; }
grep -rl 'suncal.github.io/tokentrim' docs | xargs sed -i '' 's|https://suncal.github.io/tokentrim/|https://tokentrim.com/|g; s|suncal.github.io/tokentrim/|tokentrim.com/|g'
echo tokentrim.com > docs/CNAME
gh api -X PUT repos/suncal/tokentrim/pages -f cname=tokentrim.com >/dev/null
git add docs scripts && git commit -qm "Move site to tokentrim.com custom domain" && git push -q
echo "Done. Enable 'Enforce HTTPS' in repo Settings > Pages once the cert issues."
