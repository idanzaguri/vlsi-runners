#!/usr/bin/env bash
# vlsi-runners shell integration. Source it once from ~/.bashrc, e.g.:
#   [ -f ~/vlsi-runners/activate.sh ] && source ~/vlsi-runners/activate.sh
#
# Defines test_runner / regr_runner / synth_runner as self-locating commands: each runs
# the CURRENT git repo's runner from <repo>/bin/runners (falling back to <repo>/bin for
# unmigrated repos). Resolved at call time -- no `cd` override, always the repo you are
# in. Also loads tab-completion (the completion fns resolve the repo dynamically too).

_vr_runner() {                      # $1 = runner basename, rest = its args
    local rn="$1"; shift
    local root; root=$(git rev-parse --show-toplevel 2>/dev/null) \
        || { echo "vlsi-runners: not in a git repo" >&2; return 1; }
    local r="$root/bin/runners/$rn.py"; [[ -f "$r" ]] || r="$root/bin/$rn.py"
    [[ -f "$r" ]] || { echo "vlsi-runners: no $rn.py under $root/bin" >&2; return 1; }
    python3 -B "$r" "$@"
}
test_runner()  { _vr_runner test_runner  "$@"; }
regr_runner()  { _vr_runner regr_runner  "$@"; }
synth_runner() { _vr_runner synth_runner "$@"; }

# register tab-completion (sourced once; the completion functions self-locate per repo)
_vr_su="$(cd "$(dirname "${BASH_SOURCE[0]}")/shell_utils" 2>/dev/null && pwd)"
if [[ -n "$_vr_su" ]]; then
    for _vr_f in "$_vr_su"/*_autocomplete.sh; do [[ -f "$_vr_f" ]] && source "$_vr_f"; done
fi
unset _vr_su _vr_f
