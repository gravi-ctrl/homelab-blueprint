# ~/.zsh/welcome.zsh
#
# Login health line + the `welcome` briefing.
# Defines functions and variables only: sourcing this file prints nothing.
# Source it near the top of ~/.zshrc, before anything that calls welcome_status.

zmodload zsh/datetime 2>/dev/null
zmodload -F zsh/stat b:zstat 2>/dev/null

# =============================================================================
# EDIT THESE: what the briefing lists.  Format: "key|description"
# An empty key prints the description as a dim divider line.
# Column widths are computed from the longest entries, so write full labels.
# =============================================================================
typeset -ga WELCOME_COMMANDS=(
  "welcome|Status + briefing"
  "clipin|File to clipboard"
  "oracle|AI assistant"
  "dps|Docker table"
  "cert|Cert manager"
  "vc-mount|Mount vault"
  "vc-umount|Unmount vault"
  "extract|Unzip / untar"
  "guestwifi|WiFi toggle"
  "stamp|Timestamp output"
)

typeset -ga WELCOME_SHORTCUTS=(
  "Ctrl+R|FZF history"
  "Ctrl+T|FZF files"
  "Alt+C|FZF jump"
  "→ / End|Accept suggestion"
  "Ctrl+Space|Execute suggestion"
  "↑ / ↓|Prefix history"
  "Ctrl+Bksp|Delete word"
  "|── nano ──"
  "Alt+N|Line numbers"
  "Alt+M|Mouse"
)

# =============================================================================
# HEALTH CHECK SETTINGS (all optional)
# =============================================================================
typeset -ga WELCOME_DISKS=(/)               # mount points to watch (add /data etc.)
typeset -gi WELCOME_DISK_WARN=85            # warn at this % used
typeset -g  WELCOME_BACKUP_GLOB=""          # e.g. "/data/backups/*.age"; empty = skip
typeset -gi WELCOME_BACKUP_MAX_DAYS=8       # weekly backup + a day of slack
typeset -ga WELCOME_IGNORE_CONTAINERS=()    # containers that are fine being stopped

# -----------------------------------------------------------------------------
# Health checks. Fill _WH_ISSUES (problems) and _WH_INFO (good news).
# Every check is skipped if its tool is missing, and docker is time-limited so a
# hung daemon can't stall your login.
# -----------------------------------------------------------------------------
_welcome_health() {
  emulate -L zsh
  typeset -ga _WH_ISSUES=() _WH_INFO=()

  # Containers: unhealthy, restart-looping, or exited with a non-zero code.
  # Clean exits (code 0) are treated as intentional, e.g. one-shot jobs.
  if (( $+commands[docker] )); then
    local -a dk=(docker)
    (( $+commands[timeout] )) && dk=(timeout 3 docker)
    local out line name st up=0
    if out=$("${dk[@]}" ps -a --format $'{{.Names}}\t{{.Status}}' 2>/dev/null); then
      for line in "${(@f)out}"; do
        [[ -n $line ]] || continue
        name=${line%%$'\t'*}
        st=${line#*$'\t'}
        (( ${WELCOME_IGNORE_CONTAINERS[(Ie)$name]} )) && continue
        case $st in
          *unhealthy*)            _WH_ISSUES+=("container $name is unhealthy") ;;
          Restarting*)            _WH_ISSUES+=("container $name keeps restarting") ;;
          Up*)                    (( up++ )) ;;
          Exited\ \(0\)*)         ;;
          Exited*|Dead*|Created*) _WH_ISSUES+=("container $name is down (${st%% ago*})") ;;
        esac
      done
      _WH_INFO+=("$up container$( (( up == 1 )) || print -n s ) up")
    else
      _WH_ISSUES+=("docker is not responding")
    fi
  fi

  # Failed systemd units
  if (( $+commands[systemctl] )); then
    local -a failed fnames
    local f
    failed=( "${(@f)$(systemctl list-units --state=failed --plain --no-legend --no-pager 2>/dev/null)}" )
    for f in $failed; do
      [[ -n $f ]] || continue
      [[ $f == (●|\*)\ * ]] && f=${f#* }
      fnames+=("${f%% *}")
    done
    (( ${#fnames} )) && _WH_ISSUES+=("${#fnames} failed systemd unit(s): ${(j:, :)fnames[1,3]}")
  fi

  # Disk usage
  local mp pct
  for mp in $WELCOME_DISKS; do
    [[ -d $mp ]] || continue
    pct=$(df -P -- "$mp" 2>/dev/null | awk 'NR==2 {gsub("%","",$5); print $5}')
    [[ $pct == <-> ]] || continue
    if (( pct >= WELCOME_DISK_WARN )); then
      _WH_ISSUES+=("disk $mp is ${pct}% full")
    else
      _WH_INFO+=("$mp ${pct}%")
    fi
  done

  # Backup freshness (newest file matching WELCOME_BACKUP_GLOB)
  if [[ -n $WELCOME_BACKUP_GLOB ]]; then
    local -a newest=( ${~WELCOME_BACKUP_GLOB}(N.om[1]) )
    if (( ${#newest} )); then
      local -a mt
      zstat -A mt +mtime -- "${newest[1]}"
      local age=$(( (EPOCHSECONDS - mt[1]) / 86400 ))
      if (( age >= WELCOME_BACKUP_MAX_DAYS )); then
        _WH_ISSUES+=("last backup is $age days old")
      else
        _WH_INFO+=("backup ${age}d old")
      fi
    else
      _WH_ISSUES+=("no backup found matching $WELCOME_BACKUP_GLOB")
    fi
  fi
}

_welcome_health_print() {
  emulate -L zsh
  _welcome_health
  if (( ${#_WH_ISSUES} == 0 )); then
    printf '\e[1;32m✓ System online.\e[0m \e[90m%s\e[0m\n' "${(j: · :)_WH_INFO}"
  else
    printf '\e[1;31m✗ %d issue(s) need attention:\e[0m\n' ${#_WH_ISSUES}
    local i
    for i in "${_WH_ISSUES[@]}"; do
      printf '  \e[31m•\e[0m %s\n' "$i"
    done
    (( ${#_WH_INFO} )) && printf '  \e[90m%s\e[0m\n' "${(j: · :)_WH_INFO}"
  fi
}

# Shown on SSH login (call it from .zshrc)
welcome_status() {
  emulate -L zsh
  printf '\n'
  _welcome_health_print
  printf '\e[90m💡 Tip: Lost? Type \e[1;33mwelcome\e[0;90m to view commands.\e[0m\n\n'
}

# -----------------------------------------------------------------------------
# Layout helpers
# -----------------------------------------------------------------------------

# Longest key / description of a list -> reply=(keywidth descwidth)
_welcome_widths() {
  emulate -L zsh
  local e k d kw=0 dw=0 sw=0
  for e in "$@"; do
    k=${e%%|*}
    d=${e#*|}
    if [[ -z $k ]]; then
      (( ${#d} > sw )) && sw=${#d}
      continue
    fi
    (( ${#k} > kw )) && kw=${#k}
    (( ${#d} > dw )) && dw=${#d}
  done
  (( sw > kw + 3 + dw )) && dw=$(( sw - kw - 3 ))
  reply=($kw $dw)
}

# color kw dw entries... -> reply=(one padded, coloured cell per entry)
_welcome_cells() {
  emulate -L zsh
  local color=$1 kw=$2 dw=$3
  shift 3
  local e k d cell w=$(( kw + 3 + dw ))
  local -a out=()
  for e in "$@"; do
    k=${e%%|*}
    d=${e#*|}
    if [[ -z $k ]]; then
      printf -v cell "\e[90m%-${w}s\e[0m" "$d"
    else
      printf -v cell "${color}%-${kw}s\e[0m \e[90m→\e[0m %-${dw}s" "$k" "$d"
    fi
    out+=("$cell")
  done
  reply=("${out[@]}")
}

# color kw dw ncols entries... -> reply=(rows), entries split evenly over ncols
_welcome_section_lines() {
  emulate -L zsh
  local color=$1 kw=$2 dw=$3 nc=$4
  shift 4
  _welcome_cells "$color" $kw $dw "$@"
  local -a cells=("${reply[@]}") out=()
  local n=${#cells} w=$(( kw + 3 + dw ))
  local rows=$(( (n + nc - 1) / nc )) r k idx line blank
  printf -v blank "%-${w}s" ""
  for (( r = 1; r <= rows; r++ )); do
    line=""
    for (( k = 0; k < nc; k++ )); do
      idx=$(( k * rows + r ))
      (( k )) && line+="   "
      if (( idx <= n )); then line+="${cells[idx]}"; else line+="$blank"; fi
    done
    out+=("$line")
  done
  reply=("${out[@]}")
}

# -----------------------------------------------------------------------------
# welcome: health status + the briefing, laid out for the current terminal width
#   wide   : commands in 2 columns, shortcuts beside them
#   medium : commands in 2 columns, shortcuts below
#   narrow : everything in one column (phones)
# -----------------------------------------------------------------------------
welcome() {
  emulate -L zsh
  local G='\e[32m' Y='\e[33m'
  local cols=${COLUMNS:-80} indent=2 gap=3

  _welcome_widths "${WELCOME_COMMANDS[@]}";  local kwc=${reply[1]} dwc=${reply[2]}
  _welcome_widths "${WELCOME_SHORTCUTS[@]}"; local kws=${reply[1]} dws=${reply[2]}
  local wc=$(( kwc + 3 + dwc )) ws=$(( kws + 3 + dws ))

  local ncC=1
  (( cols >= indent + 2 * wc + gap )) && ncC=2
  local wcsec=$(( ncC * wc + (ncC - 1) * gap ))
  local side=0
  (( ncC == 2 && cols >= indent + wcsec + gap + ws )) && side=1

  local total
  if (( side )); then
    total=$(( indent + wcsec + gap + ws ))
  else
    total=$(( indent + (wcsec > ws ? wcsec : ws) ))
  fi
  local div=$'\e[90m'"${(l:$total::─:)}"$'\e[0m'

  local -a rc rs
  _welcome_section_lines "$G" $kwc $dwc $ncC "${WELCOME_COMMANDS[@]}";  rc=("${reply[@]}")
  _welcome_section_lines "$Y" $kws $dws 1    "${WELCOME_SHORTCUTS[@]}"; rs=("${reply[@]}")

  printf '\n\e[1;35m🚀 System Briefing for\e[0m \e[1;31m%s\e[0m\n' "$USER"
  _welcome_health_print
  print -r -- "$div"

  local i
  if (( side )); then
    local blank n=${#rc}
    (( ${#rs} > n )) && n=${#rs}
    printf -v blank "%-${wcsec}s" ""
    printf "  \e[90m%-${wcsec}s%${gap}s%s\e[0m\n" "Commands" "" "Shortcuts"
    print -r -- "$div"
    for (( i = 1; i <= n; i++ )); do
      printf '  %s   %s\n' "${rc[i]:-$blank}" "${rs[i]}"
    done
  else
    printf '  \e[90m%s\e[0m\n' "Commands"
    for i in "${rc[@]}"; do printf '  %s\n' "$i"; done
    printf '\n  \e[90m%s\e[0m\n' "Shortcuts"
    for i in "${rs[@]}"; do printf '  %s\n' "$i"; done
  fi
  print -r -- "$div"
  printf '\n'
}
