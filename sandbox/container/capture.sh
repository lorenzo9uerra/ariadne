# Run one agent command and keep at most limit+1 bytes per stream, draining
# both pipes so a flood cannot stall it. Arguments: command, byte limit.
# The command is one positional argument and is never interpolated.
set -u
limit="$2"
directory=$(mktemp -d /tmp/output.XXXXXXXX)
trap 'rm -rf "$directory"' EXIT
mkfifo "$directory/out.pipe" "$directory/err.pipe"
capture() {
    { head -c "$((limit + 1))"; cat >/dev/null; } < "$1" > "$2"
}
capture "$directory/out.pipe" "$directory/out" & out_pid=$!
capture "$directory/err.pipe" "$directory/err" & err_pid=$!
# Capture Bash's signal diagnostics alongside the command's stderr.
{ /bin/bash --noprofile --norc -c "$1"; } > "$directory/out.pipe" 2> "$directory/err.pipe"
status=$?
wait "$out_pid" "$err_pid"
printf '%s\n' "$status"
wc -c < "$directory/out"
wc -c < "$directory/err"
head -c "$limit" "$directory/out" | base64 -w0
printf '\n'
head -c "$limit" "$directory/err" | base64 -w0
printf '\n'
