# Run one agent command and keep at most limit+1 bytes per stream, draining
# both pipes so a flood cannot stall it. Argument: byte limit.
# The command arrives in COMMAND and runs from a file, so it is never
# interpolated and no command line in the container contains it: a
# `pkill -f` in the command can match only the processes it starts.
set -u
limit="$1"
directory=$(mktemp -d /tmp/output.XXXXXXXX)
trap 'rm -rf "$directory"' EXIT
printf '%s' "$COMMAND" > "$directory/command"
unset COMMAND
mkfifo "$directory/out.pipe" "$directory/err.pipe"
capture() {
    { head -c "$((limit + 1))"; cat >/dev/null; } < "$1" > "$2"
}
capture "$directory/out.pipe" "$directory/out" & out_pid=$!
capture "$directory/err.pipe" "$directory/err" & err_pid=$!
# Capture Bash's signal diagnostics alongside the command's stderr.
{ /bin/bash --noprofile --norc "$directory/command"; } > "$directory/out.pipe" 2> "$directory/err.pipe"
status=$?
wait "$out_pid" "$err_pid"
printf '%s\n' "$status"
wc -c < "$directory/out"
wc -c < "$directory/err"
head -c "$limit" "$directory/out" | base64 -w0
printf '\n'
head -c "$limit" "$directory/err" | base64 -w0
printf '\n'
