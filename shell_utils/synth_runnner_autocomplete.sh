#!/bin/bash

_synth_runner_completion() {
    local cur prev words cword
    # Initialize completion variables.
    # If _init_completion is available (from bash-completion), you can use:
    # _init_completion || return
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"

    local repo_top
    repo_top=$(git rev-parse --show-toplevel 2>/dev/null) || return 0
    
    # Define the available options.
    local opts="--block --top --clean --pedant --rundir --files --synth --verilator --xilinx --spartan6 --flatten --noattr_netlist -h"

    # If the current word starts with a dash, complete options.
    if [[ "$cur" == -* ]]; then
        COMPREPLY=( $(compgen -W "$opts" -- "$cur") )
        return 0
    fi
    
    # If the previous word was "--block", complete block names.
    if [[ "$prev" == "--block" ]]; then
        # Locate the git repository top.
        local verif_dir="$repo_top/design"
        # If the "verif" directory exists, list its subdirectories.
        if [[ -d "$verif_dir" ]]; then
            local blocks
            # This command lists the names of directories under verif.
            blocks=$(find "$verif_dir" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' 2>/dev/null)
            COMPREPLY=( $(compgen -W "$blocks" -- "$cur") )
        fi
        return 0
    fi

    # If the previous word was "--test", complete test names.
    if [[ "$prev" == "--top" ]]; then
        # First, find the block value given in the command line.
        local block=""
        local i
        for (( i=0; i < ${#COMP_WORDS[@]}; i++ )); do
            if [[ "${COMP_WORDS[i]}" == "--block" && $((i+1)) -lt ${#COMP_WORDS[@]} ]]; then
                block="${COMP_WORDS[i+1]}"
                break
            fi
        done

        # If no block is specified, there's nothing to complete.
        [[ -z "$block" ]] && return 0
        
	local modules
        modules=$(find "$repo_top/design/${block}/src" -type f -printf '%f\n' 2>/dev/null)
	COMPREPLY=( $(compgen -W "$modules" -- "$cur") )

        return 0
    fi

    # Fallback: complete available options.
    COMPREPLY=( $(compgen -W "$opts" -- "$cur") )
    return 0
}


complete -F _synth_runner_completion synth_runner

