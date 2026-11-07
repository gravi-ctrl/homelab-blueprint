# ~/.zsh/functions.zsh
#
# Custom functions, moved out of ~/.zshrc unchanged.
# Remember: if a name here is also defined as an alias anywhere, run
# `unalias <name>` first or zsh will give a parse error.

# Adds a timestamp to terminal. Usage: stamp <command>
stamp() {
  "$@" | ts
}

# File or logs to clipboard
clipin() {
  if [[ "$1" == -h || "$1" == --help || -t 0 ]]; then
    local rc=1
    [[ "$1" == -h || "$1" == --help ]] && rc=0
    cat <<'EOF'
clipin: copy stdin to your local clipboard (OSC 52, works over SSH)

Usage:
  clipin < file.txt
  some-command 2>&1 | clipin

Options:
  -h, --help   show this help

Notes:
  With no piped input it shows this help instead of waiting.
  Large inputs may be truncated or ignored by the terminal.
  Inside tmux, set-clipboard must be on.
EOF
    return $rc
  fi
  printf '\033]52;c;%s\a' "$(base64 -w0)"
}

# Readable Docker Status
dps() {
  {
    printf 'NAMES\tSTATUS\tIMAGE\tPORTS\n'
    docker ps -a --format '{{.Names}}\t{{.Status}}\t{{.Image}}\t{{.Ports}}' \
      | sort -k1,1 \
      | awk -F'\t' -v OFS='\t' '{ sub(/^[^\/]*[.:][^\/]*\//, "", $3); print }' \
      | sed -E 's/, \[::\]:[0-9-]+->[0-9-]+\/(tcp|udp)//g; s/0\.0\.0\.0://g'
  } | column -t -s $'\t'
}

# VeraCrypt mount and unmount
vc-mount() { sudo veracrypt --text --pim=0 --keyfiles="" --protect-hidden=no "$1" "$2" }
vc-umount() { sudo veracrypt --text --dismount "$1" }

# Universal Archive Extractor
extract() {
    emulate -L zsh
    setopt extended_glob glob_dots

    (( $# )) || { print -u2 "Usage: extract <archive> [archive...]"; return 1 }

    # Longest first, so tar.gz is matched before gz
    local -a exts=(tar.gz tar.bz2 tar.xz tar.zst tar.lz4 tar.z tgz tbz2 txz tzst tar
                   zip jar apk war rar 7z gz bz2 xz zst lz4 z)
    local f rc=0 base lc e ext name outdir dest tmp st
    local -a top

    for f in "$@"; do
        if [[ ! -f $f ]]; then
            print -u2 "extract: '$f' is not a file"; rc=1; continue
        fi

        # Work out the extension (case-insensitive) and the output name
        base=${f:t} lc=${(L)f:t} ext=""
        for e in $exts; do
            [[ $lc == *.$e ]] && { ext=$e; break }
        done
        if [[ -z $ext ]]; then
            print -u2 "extract: '$f' has an unsupported extension"; rc=1; continue
        fi
        name=${base:0:$(( ${#base} - ${#ext} - 1 ))}
        [[ -n $name ]] || name=extracted

        # Output goes next to the archive (set outdir=. on the next line to use the current dir)
        outdir=${f:h}
        [[ $outdir == . ]] && dest=$name || dest=$outdir/$name

        if [[ -e $dest ]]; then
            print -u2 "extract: '$dest' already exists, skipping '$f'"; rc=1; continue
        fi

        # Extract into a scratch dir first, so a failure never leaves junk behind
        tmp=$outdir/.extract.$$.$RANDOM
        mkdir -- "$tmp" || { rc=1; continue }

        case $ext in
            tar.*|tgz|tbz2|txz|tzst|tar) tar -xf "$f" -C "$tmp" ;;
            zip|jar|apk|war)             unzip -q -- "$f" -d "$tmp" ;;
            rar)                         unrar x -inul -- "$f" "$tmp/" ;;
            7z)                          7z x -bso0 -bsp0 -o"$tmp" -- "$f" ;;
            gz)                          gunzip  -c -- "$f" > "$tmp/$name" ;;
            bz2)                         bunzip2 -c -- "$f" > "$tmp/$name" ;;
            xz)                          xz  -dc -- "$f" > "$tmp/$name" ;;
            zst)                         zstd -dqc -- "$f" > "$tmp/$name" ;;
            lz4)                         lz4 -dc -- "$f" > "$tmp/$name" ;;
            z)                           uncompress -c -- "$f" > "$tmp/$name" ;;
        esac
        st=$?

        if (( st != 0 )); then
            rm -rf -- "$tmp"
            print -u2 "extract: failed to extract '$f'"; rc=1; continue
        fi

        # One top-level folder inside? Use it directly instead of name/name/
        top=( "$tmp"/*(N) )
        if (( ${#top} == 1 )) && [[ -d ${top[1]} && ! -L ${top[1]} ]]; then
            mv -- "${top[1]}" "$dest" && rmdir -- "$tmp"
        else
            mv -- "$tmp" "$dest"
        fi
        print "Extracted '$f' -> '$dest/'"
    done
    return $rc
}
