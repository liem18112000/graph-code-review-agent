#!/usr/bin/env bash
# Install Defects4J for the step-5 benchmark arm (docs/architecture.md §10.1).
#
# NOT run automatically: init.sh clones the subject projects and pulls down
# several GB. Run it once, deliberately, when you are ready to benchmark.
#
#   bash setup-defects4j.sh [install-dir]      # default ~/defects4j
#
# Afterwards, add the printed line to your shell profile, then:
#   python bench.py prepare --bugs Lang:1,Math:5 --out tasks/
set -euo pipefail

DIR="${1:-$HOME/defects4j}"

need() { command -v "$1" >/dev/null || { echo "missing: $1 -- $2" >&2; exit 1; }; }
need git  "install git"
need perl "install perl (Strawberry Perl on Windows, or use WSL)"
need java "install a JDK (Defects4J needs Java 8 for some projects; 11/17 work for most)"

echo "java: $(java -version 2>&1 | head -1)"
echo "perl: $(perl -e 'print $^V')"
echo

if [ -d "$DIR/.git" ]; then
  echo "== updating $DIR"
  git -C "$DIR" pull --ff-only
else
  echo "== cloning into $DIR"
  git clone https://github.com/rjust/defects4j "$DIR"
fi

cd "$DIR"

echo "== perl deps"
if command -v cpanm >/dev/null; then
  cpanm --installdeps .
else
  echo "cpanm not found; trying cpan (slower, may prompt)" >&2
  cpan App::cpanminus && cpanm --installdeps .
fi

echo "== init.sh (downloads subject projects -- this is the slow, multi-GB part)"
./init.sh

cat <<EOF

Done. Add to your shell profile:

    export PATH="$DIR/framework/bin:\$PATH"

Verify:

    defects4j info -p Lang

Then build the benchmark corpus. Start small -- one project, a handful of bugs:

    python bench.py prepare --bugs Lang:1,Lang:2,Lang:3,Math:5 --out tasks/

Each task carries the inverse patch, the ground truth, and the triggering
tests. Run both arms over tasks/, write <bug>.json per task into out/<arm>/,
then compare:

    python bench.py score --tasks-dir tasks/ --findings-dir out/graph/
    python bench.py score --tasks-dir tasks/ --findings-dir out/baseline/
EOF
