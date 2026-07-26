#!/bin/bash

_metric_runner_completion() {
    local cur prev
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"

    local repo_top
    repo_top=$(git rev-parse --show-toplevel 2>/dev/null) || return 0

    local opts="--block --top --vs --clean --flatten -h"

    if [[ "$cur" == -* ]]; then
        COMPREPLY=( $(compgen -W "$opts" -- "$cur") )
        return 0
    fi

    # --block / --vs both take a design block name (design/<block>).
    if [[ "$prev" == "--block" || "$prev" == "--vs" ]]; then
        local design_dir="$repo_top/design"
        if [[ -d "$design_dir" ]]; then
            local blocks
            blocks=$(find "$design_dir" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' 2>/dev/null)
            COMPREPLY=( $(compgen -W "$blocks" -- "$cur") )
        fi
        return 0
    fi

    # --top completes module source file names under the chosen block, like synth_runner.
    if [[ "$prev" == "--top" ]]; then
        local block="" i
        for (( i=0; i < ${#COMP_WORDS[@]}; i++ )); do
            if [[ "${COMP_WORDS[i]}" == "--block" && $((i+1)) -lt ${#COMP_WORDS[@]} ]]; then
                block="${COMP_WORDS[i+1]}"
                break
            fi
        done
        [[ -z "$block" ]] && return 0
        local modules
        modules=$(find "$repo_top/design/${block}/src" -type f -printf '%f\n' 2>/dev/null)
        COMPREPLY=( $(compgen -W "$modules" -- "$cur") )
        return 0
    fi

    COMPREPLY=( $(compgen -W "$opts" -- "$cur") )
    return 0
}

complete -F _metric_runner_completion metric_runner
